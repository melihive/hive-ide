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
