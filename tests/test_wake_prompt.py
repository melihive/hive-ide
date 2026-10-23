"""`wake`: resume a SLEEPING session's conversation with a one-shot prompt.

Sleeping is the session's own `sleep.state == "sleeping"`, the state `sleep`
writes and the sidebar marks 💤. The prompt rides the driver's resume argv through
the ordinary launch paths (`build` with no window, `respawn_agent` with one) —
never typed — and is consumed by that launch. tmux is faked: these tests read the
exact command each pane would have been given, and nothing real is launched.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys

import pytest

from hive_ide.cli import main
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.store import StateStore


PROMPT = "Relay → probe (you): reply from vivo in #ops. Run ./hive relay read"
REFERENCE = "0b1c2d3e-4f50-4617-8293-a4b5c6d7e8f9"


class FakeTmux:
    """Answers just enough of tmux for `build`/`respawn_agent`, recording every
    pane command it is handed."""

    def __init__(self, *, window: bool = False, fail_split: bool = False):
        self.server_path: str | None = "/server/bin:/usr/bin"
        self.pane_output = ""
        self.exists = window
        self.windows: dict[str, str] = {}
        self.window = window
        self.fail_split = fail_split
        self.calls: list[list[str]] = []

    def launches(self) -> list[str]:
        return [
            call[-1]
            for call in self.calls
            if call and call[0] in ("split-window", "respawn-pane")
        ]

    def __call__(self, _frame, args, **_kwargs):
        args = list(args)
        self.calls.append(args)

        def reply(stdout: str = "", code: int = 0):
            return subprocess.CompletedProcess(["tmux", *args], code, stdout, "")

        verb = args[0]
        if verb == "has-session":
            return reply(code=0 if self.exists else 1)
        if verb == "list-windows":
            return reply("".join(f"{sid}\t{wid}\n" for sid, wid in self.windows.items()))
        if verb == "list-panes" and "#{@hive_ide_pane}\t#{pane_id}" in args:
            return reply("sidebar\t%1\nagent\t%2\nplan\t%3\n")
        if verb in ("new-session", "new-window"):
            self.exists = True
            return reply("@9\n")
        if verb == "split-window" and self.fail_split:
            return subprocess.CompletedProcess(args, 1, "", "no space for new pane")
        if verb == "display-message":
            return reply(code=1)
        if verb == "show-environment":
            if self.server_path is None:
                return subprocess.CompletedProcess(args, 1, "", "unknown variable: PATH")
            return reply(f"PATH={self.server_path}\n")
        if verb == "capture-pane":
            return reply(self.pane_output)
        return reply()

    def launch_path(self) -> str:
        agent = [c for c in self.calls if c[0] in ("split-window", "respawn-pane")
                 and "HIVE_IDE_PANE_ROLE=agent" in c]
        assert len(agent) == 1, agent
        call = agent[0]
        paths = [call[i + 1] for i, a in enumerate(call) if a == "-e" and call[i + 1].startswith("PATH=")]
        assert len(paths) == 1, call
        return paths[0].removeprefix("PATH=")


def _base(tmp_path) -> list[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return [
        "--state-home", str(tmp_path / "state"),
        "--workspace-key", str(workspace),
    ]


def _session(tmp_path, driver_id: str, *, reference: str | None = REFERENCE) -> tuple[StateStore, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="S",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver=bundled_drivers()[driver_id].resolve(
            name="S", working_dir=str(workspace), conversation_reference=reference
        ),
    )
    return store, record


@pytest.fixture
def tmux(monkeypatch):
    fake = FakeTmux()
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: fake(self, args, **kw))
    # Not under test, and each reaches outside the test: the guard probes the
    # driver's real transcript store, the source refresh spawns an interpreter.
    monkeypatch.setattr("hive_ide.frame.ConversationGuard.check", lambda *a, **k: None)
    monkeypatch.setattr(Frame, "_refresh_source_if_needed", lambda *a, **k: None)
    monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: False)
    # The launch check polls for up to 2 s; one look is enough against a fake.
    monkeypatch.setattr(Frame, "LAUNCH_SETTLE_SECONDS", 0.0)
    return fake


def _sleep(tmp_path, record) -> None:
    assert main([*_base(tmp_path), "sleep", f"--session-id={record['id']}",
                 "--tmux-socket=test-wake"]) == 0


def _wake(tmp_path, capsys, session_id: str, prompt: str = PROMPT) -> tuple[int, dict]:
    capsys.readouterr()
    code = main([*_base(tmp_path), "wake", f"--session={session_id}",
                 f"--prompt={prompt}", "--tmux-socket=test-wake"])
    return code, json.loads(capsys.readouterr().out)


def _resume_argv(driver_id: str, record: dict) -> list[str]:
    argv = list(record["driver"]["launch_argv"])
    if driver_id == "claude":
        assert argv[:3] == ["claude", "--resume", REFERENCE]
    else:
        assert argv[:2] == ["codex", "resume"]
        assert "--no-daemon" in argv
        assert REFERENCE in argv
    return argv


@pytest.mark.parametrize("driver_id", ["claude", "codex"])
@pytest.mark.parametrize("window", [False, True], ids=["no-window", "sleeping-window"])
def test_a_sleeping_session_wakes_resuming_with_the_prompt(
    tmp_path, capsys, tmux, driver_id, window
):
    store, record = _session(tmp_path, driver_id)
    _sleep(tmp_path, record)
    if window:
        tmux.exists = True
        tmux.windows[record["id"]] = "@4"
    tmux.calls.clear()

    code, out = _wake(tmp_path, capsys, record["id"])

    assert (code, out) == (0, {"ok": True, "woke": True, "session": record["id"]})
    expected = shlex.join([*_resume_argv(driver_id, record), PROMPT])
    agent = [command for command in tmux.launches() if expected in command]
    assert len(agent) == 1, tmux.launches()
    verb = "respawn-pane" if window else "split-window"
    assert any(call[0] == verb and call[-1] == agent[0] for call in tmux.calls)
    assert not any(call[0] == "send-keys" for call in tmux.calls)   # never typed
    fresh = store.find_session(record["id"])
    assert "sleep" not in fresh
    assert "launch_prompt" not in fresh
    assert store.read("status", record["id"]) is None


@pytest.mark.parametrize("driver_id", ["claude", "codex"])
def test_the_prompt_is_one_shot(tmp_path, capsys, tmux, driver_id):
    store, record = _session(tmp_path, driver_id)
    _sleep(tmp_path, record)
    assert _wake(tmp_path, capsys, record["id"])[0] == 0
    fresh = store.find_session(record["id"])
    # A later relaunch of the same record (a rebuild, a `chat` respawn) resumes
    # without repeating the prompt.
    assert PROMPT not in Frame._agent_command(fresh)
    assert shlex.join(_resume_argv(driver_id, fresh)) in Frame._agent_command(fresh)


def test_a_session_that_is_not_sleeping_is_refused_and_nothing_launches(
    tmp_path, capsys, tmux
):
    store, record = _session(tmp_path, "claude")
    before = store.find_session(record["id"])
    tmux.calls.clear()
    code, out = _wake(tmp_path, capsys, record["id"])
    assert (code, out) == (2, {"ok": False, "woke": False, "reason": "not_sleeping"})
    assert tmux.calls == []
    assert store.find_session(record["id"]) == before


@pytest.mark.parametrize(
    "prompt",
    ["", "  padded  ", "--yolo", "esc \x1b[2J", "x" * 4001],
    ids=["empty", "padded", "option", "control", "long"],
)
def test_a_bad_prompt_is_refused_before_anything_changes(tmp_path, capsys, tmux, prompt):
    store, record = _session(tmp_path, "codex")
    _sleep(tmp_path, record)
    before = store.find_session(record["id"])
    status = store.read("status", record["id"])
    tmux.calls.clear()
    code, out = _wake(tmp_path, capsys, record["id"], prompt)
    assert code == 2
    assert out["reason"] == "bad_prompt" and out["ok"] is False and out["woke"] is False
    assert tmux.calls == []
    assert store.find_session(record["id"]) == before
    assert store.read("status", record["id"]) == status


def test_an_unknown_session_is_refused(tmp_path, capsys, tmux):
    code, out = _wake(tmp_path, capsys, "no-such-session")
    assert (code, out["reason"]) == (2, "unknown_session")


def test_a_driver_that_takes_no_prompt_is_refused(tmp_path, capsys, tmux):
    store, record = _session(tmp_path, "antigravity")
    _sleep(tmp_path, record)
    tmux.calls.clear()
    code, out = _wake(tmp_path, capsys, record["id"])
    assert (code, out["reason"]) == (2, "driver_unsupported")
    assert tmux.calls == []


def test_a_session_with_no_conversation_is_not_started_fresh(tmp_path, capsys, tmux):
    store, record = _session(tmp_path, "claude", reference=None)
    _sleep(tmp_path, record)
    tmux.calls.clear()
    code, out = _wake(tmp_path, capsys, record["id"])
    assert (code, out["reason"]) == (1, "no_conversation")
    assert tmux.launches() == []
    assert Frame._is_sleeping(store.find_session(record["id"]))


def test_a_failed_launch_puts_the_session_back_to_sleep(tmp_path, capsys, tmux):
    store, record = _session(tmp_path, "claude")
    _sleep(tmp_path, record)
    tmux.fail_split = True
    code, out = _wake(tmp_path, capsys, record["id"])
    assert (code, out["reason"], out["woke"]) == (1, "launch_failed", False)
    fresh = store.find_session(record["id"])
    assert Frame._is_sleeping(fresh)
    assert "launch_prompt" not in fresh
    assert store.read("status", record["id"])["state"] == "sleeping"


def test_building_a_sleeping_window_keeps_an_undelivered_launch_prompt(tmp_path, tmux):
    store, record = _session(tmp_path, "codex")
    record["sleep"] = {"state": "sleeping", "slept_at": "2026-10-23T00:00:00Z"}
    record["launch_prompt"] = PROMPT
    store.write("sessions", record["id"], record)
    Frame(store, socket="test-wake").build(record)
    assert PROMPT not in "".join(tmux.launches())          # the sleeping shell
    assert store.find_session(record["id"])["launch_prompt"] == PROMPT


def test_wake_is_a_workspace_mutation_and_a_capability(capsys):
    from hive_ide.cli import WORKSPACE_MUTATIONS

    assert "wake" in WORKSPACE_MUTATIONS
    assert main(["capabilities"]) == 0
    assert "wake-prompt" in json.loads(capsys.readouterr().out)["features"]


@pytest.mark.parametrize("window", [False, True], ids=["no-window", "sleeping-window"])
def test_a_driver_launch_carries_the_tmux_server_path(tmp_path, capsys, tmux, window):
    # A daemon's client PATH lacks ~/.local/bin, where `claude` lives; the pane
    # must get the server's PATH instead, or it exits 127.
    store, record = _session(tmp_path, "claude")
    _sleep(tmp_path, record)
    if window:
        tmux.exists = True
        tmux.windows[record["id"]] = "@4"
    tmux.calls.clear()
    assert _wake(tmp_path, capsys, record["id"])[0] == 0
    assert tmux.launch_path().startswith("/server/bin:/usr/bin")


def test_without_a_server_path_the_client_path_gets_the_user_bin_dirs(
    tmp_path, capsys, tmux, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".npmrc").write_text("prefix=~/.npm-global\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("PIPX_BIN_DIR", "/pipx/bin")
    monkeypatch.delenv("NPM_CONFIG_PREFIX", raising=False)
    monkeypatch.delenv("PNPM_HOME", raising=False)
    tmux.server_path = None
    store, record = _session(tmp_path, "claude")
    _sleep(tmp_path, record)
    tmux.calls.clear()
    assert _wake(tmp_path, capsys, record["id"])[0] == 0
    assert tmux.launch_path().split(":") == [
        str(home / ".local" / "bin"), "/pipx/bin", str(home / ".npm-global" / "bin"),
        "/usr/bin", "/bin",
    ]


@pytest.mark.parametrize("window", [False, True], ids=["no-window", "sleeping-window"])
def test_a_driver_that_exits_at_launch_is_launch_failed_and_sleeps_again(
    tmp_path, capsys, tmux, window
):
    store, record = _session(tmp_path, "claude")
    _sleep(tmp_path, record)
    if window:
        tmux.exists = True
        tmux.windows[record["id"]] = "@4"
    tmux.pane_output = (
        "sh: claude: command not found\n\nhive-ide: driver command failed with exit 127\n$ "
    )
    code, out = _wake(tmp_path, capsys, record["id"])
    assert (code, out["reason"], out["woke"]) == (1, "launch_failed", False)
    assert "exit 127" in out["detail"]
    fresh = store.find_session(record["id"])
    assert Frame._is_sleeping(fresh)
    assert "launch_prompt" not in fresh
    assert store.read("status", record["id"])["state"] == "sleeping"


def test_the_launch_check_is_bounded(tmp_path, tmux, monkeypatch):
    store, record = _session(tmp_path, "claude")
    monkeypatch.setattr(Frame, "LAUNCH_SETTLE_SECONDS", 0.3)
    monkeypatch.setattr(Frame, "LAUNCH_POLL_SECONDS", 0.05)
    frame = Frame(store, socket="test-wake")
    import time
    started = time.monotonic()
    frame._confirm_agent_launch("%2")          # a healthy pane: no marker, alive
    assert 0.3 <= time.monotonic() - started < 1.5
