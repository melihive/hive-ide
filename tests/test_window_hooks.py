"""Window hooks paint first and cannot race a CLI layout mutation."""
import shlex
import subprocess
import sys
import os
import time
from pathlib import Path

from hive_ide.frame import Frame
from hive_ide.relayout import IdeRelayout
from hive_ide.store import StateStore


def test_window_hook_paints_before_adopting_layout(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state", tmp_path)
    frame = Frame(store, socket="fixture")
    calls = []
    def tmux(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(frame, "tmux", tmux)
    observer = tmp_path / "observe.py"
    observer.write_text("import sys; print(sys.argv[1])\n")
    monkeypatch.setattr(frame, "_module", lambda module, args: shlex.join([
        sys.executable, str(observer), module,
    ]))
    frame.bind_keys()
    hooks = [args[-1] for args in calls if args[0] == "set-hook"
             and "session-window-changed" in args and "wake" in args[-1]]
    assert len(hooks) == 1
    tokens = shlex.split(hooks[0])
    assert tokens[:2] == ["run-shell", "-b"]
    observed = subprocess.run(tokens[2], shell=True, capture_output=True, text=True, check=True)
    assert observed.stdout.splitlines() == ["wake", "relayout"]


def test_layout_hook_holds_the_same_workspace_lock_as_cli(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state", tmp_path)
    held = []
    def at_layout_gate(path, **kwargs):
        held.append(store.mutation_lock_held())
        return True  # Stop here: the assertion observes the real lock, no tmux.
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", staticmethod(at_layout_gate))
    assert IdeRelayout.main([
        "relayout", "--state-home", str(store.home), "--workspace-key", store.workspace_key,
        "--tmux-socket", "fixture", "--mode", "adopt",
    ]) == 0
    assert held == [True]
    assert store.mutation_lock_held() is False


def test_resize_hooks_debounce_before_cli_lock_and_only_newest_applies(tmp_path):
    store = StateStore(tmp_path / "state", tmp_path)
    applied = tmp_path / "applied"
    script = """
import sys
from pathlib import Path
from hive_ide.relayout import IdeRelayout
state, workspace, ready, applied, label, height = sys.argv[1:]
original = IdeRelayout._coalesced_by_newer_snap
def debounce(path, **kwargs):
    result = original(path, **kwargs)
    Path(ready).write_text(str(result))
    return result
def apply(path):
    with open(applied, 'a') as stream:
        stream.write(label + '\\n')
    return True
IdeRelayout._coalesced_by_newer_snap = staticmethod(debounce)
IdeRelayout._breaker_tripped = staticmethod(apply)
raise SystemExit(IdeRelayout.main([
    'relayout', '--state-home', state, '--workspace-key', workspace,
    '--tmux-socket', 'fixture', '--mode', 'snap',
    '--client-width', '180', '--client-height', height,
]))
"""
    children = []
    def start(label, height):
        ready = tmp_path / label
        child = subprocess.Popen([
            sys.executable, "-c", script, str(store.home), store.workspace_key,
            str(ready), str(applied), label, str(height),
        ], env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])))
        children.append(child)
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert child.poll() is None
            time.sleep(0.01)
        assert ready.exists(), "debounce was blocked behind the CLI's lock"
        assert ready.read_text() == "False"
        assert not applied.exists(), "hook mutated panes before the CLI released its lock"
        return child
    try:
        with store.mutation_lock():
            start("old", 40)
            start("new", 42)
        for child in children:
            assert child.wait(timeout=5) == 0
        assert applied.read_text().splitlines() == ["new"]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


def test_client_hooks_elect_before_relayout(tmp_path, monkeypatch):
    """Both client hooks elect FIRST, then relayout.

    As two independent hooks, relayout could read the departing client's 100x30
    before the election demoted it, resize the panes to that, and leave a layout
    nothing afterwards corrects. On detach the order matters for a second reason:
    tmux preserves a zoom across an automatic resize, so a departing narrow
    controller would leave the promoted wide survivor still zoomed for mobile.
    """
    store = StateStore(tmp_path / "state", tmp_path)
    frame = Frame(store, socket="fixture")
    calls = []

    def tmux(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(frame, "tmux", tmux)
    observer = tmp_path / "observe.py"
    observer.write_text("import sys; print(sys.argv[1])\n")
    monkeypatch.setattr(
        frame,
        "_module",
        lambda module, args: shlex.join([sys.executable, str(observer), module]),
    )

    frame.bind_keys()

    for hook in ("client-attached", "client-detached"):
        installed = [
            args[-1]
            for args in calls
            if args[0] == "set-hook" and hook in args and "run-shell" in str(args[-1])
        ]
        assert len(installed) == 1, f"{hook}: expected one hook, got {installed}"
        # `client-attached` stamps the attention client before the run-shell, so
        # the command is the tail rather than the whole value.
        tokens = shlex.split(installed[0].split("run-shell -b ", 1)[-1])
        observed = subprocess.run(
            tokens[0], shell=True, capture_output=True, text=True, check=True
        )
        assert observed.stdout.splitlines() == ["clients", "relayout"], (
            f"{hook} ran {observed.stdout.splitlines()}; the election must come "
            "first or relayout decides from the wrong client"
        )


def test_the_election_holds_the_workspace_lock(tmp_path, monkeypatch):
    """Serialized, not merely retried.

    Three stale retries could each lose their race and still exit leaving two
    controllers, with no election left to repair them — competing geometry votes,
    which is the pane-reflow freeze. Elections take the same cross-process lock
    state mutations take, so they cannot interleave at all.
    """
    store = StateStore(tmp_path / "state", tmp_path)
    frame = Frame(store, socket="fixture")
    held = []

    monkeypatch.setattr(
        Frame,
        "attached_clients",
        lambda self: (held.append(store.mutation_lock_held()), [])[1],
    )

    assert frame.elect_controller() is None
    assert held, "the election never read the client list"
    assert all(held), "the client list was read without the workspace lock held"
