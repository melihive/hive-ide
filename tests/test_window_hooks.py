"""Window hooks paint first and cannot race a CLI layout mutation."""
import shlex
import subprocess
import sys

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
