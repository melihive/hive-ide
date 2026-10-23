"""Session labels, hook turn sequencing, and the driver-rename typing gates.

The typing gate decides from hook state, the process table and tmux client
activity — never from the screen. Pane widths and hidden windows broke every
screen check; tests that use a narrow pane assert `capture-pane` is never called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hive_ide.cli import main
from hive_ide.drivers import bundled_drivers
from hive_ide.errors import UsageError
from hive_ide.frame import Frame
from hive_ide.hook import IdeHook
from hive_ide.info import _card
from hive_ide.nudge import AgentNudge
from hive_ide.seen import IdeSeen
from hive_ide.state_compat import StateIO
from hive_ide.store import StateStore
from hive_ide.turns import TurnSequence


NOW = 1_800_000_000
# A one-column pane: what `capture-pane` returns there. The gate must never read it.
ONE_COLUMN_SCREEN = ["❯", "─", "T", "r", "y", "─", "⏵"]


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": "test"}


def _session(tmp_path, driver_id: str = "codex", name: str = "S") -> tuple[StateStore, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name=name,
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()[driver_id].resolve(
            name=name, working_dir=str(workspace), conversation_reference=None
        ),
    )
    return store, record


class FakeTmux:
    """Answers the tmux calls the gates and rename make; records every call."""

    def __init__(
        self,
        *,
        screen: list[str] | None = None,
        command: str = "codex",
        in_mode: str = "0",
        dead: str = "0",
        clients: str = "",
        clients_rc: int = 0,
        target: str = "%2",
        pane_pid: str = "4100",
        processes: list[tuple[str, str, str]] | None = None,
        ps_ok: bool = True,
    ):
        self.screen = screen or []
        self.command = command
        self.in_mode = in_mode
        self.dead = dead
        self.clients = clients
        self.clients_rc = clients_rc
        self.target = target
        self.pane_pid = pane_pid
        # `ps` rows as (ppid, pid, args); the default is the pane process alone,
        # with no children. `ps_ok=False` = the process table cannot be read.
        self.processes = processes if processes is not None else [("1", pane_pid, command)]
        self.ps_ok = ps_ok
        self.calls: list[list[str]] = []
        self.timeouts: list[object] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        self.timeouts.append(kwargs.get("timeout"))
        verb = args[0]
        if "-t" in args:
            assert args[args.index("-t") + 1] == self.target, args
        if verb == "send-keys" and "-l" in args:
            # literal text must follow `--` so tmux never parses it as flags
            assert args[:5] == ["send-keys", "-l", "-t", self.target, "--"] or (
                args[4].startswith("/rename ") and len(args) == 5
            ), args
        if verb == "display-message":
            return SimpleNamespace(
                returncode=0,
                stdout=f"{self.in_mode}\t{self.command}\t{self.dead}\t{self.pane_pid}\n",
                stderr="",
            )
        if verb == "list-clients":
            return SimpleNamespace(returncode=self.clients_rc, stdout=self.clients, stderr="")
        if verb == "capture-pane":
            return SimpleNamespace(returncode=0, stdout="\n".join(self.screen) + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def ps(self, args):
        if not self.ps_ok:
            return None
        if args == ["-p", self.pane_pid, "-o", "args="]:
            rows = [row for row in self.processes if row[1] == self.pane_pid]
            return f"{rows[0][2]}\n" if rows else None
        assert args == ["-A", "-o", "ppid=,pid=,comm="], args
        return "".join(
            f"{ppid:>5} {pid:>5} {cmd.split()[0]}\n" for ppid, pid, cmd in self.processes
        )

    @property
    def sent(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "send-keys"]


def _frame(store, monkeypatch, fake: FakeTmux) -> Frame:
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    monkeypatch.setattr(frame, "tmux", fake)
    monkeypatch.setattr(AgentNudge, "_ps", lambda _self, args: fake.ps(args))
    return frame


def _hook(store, record, monkeypatch, tmp_path, state: str, payload: dict, driver="codex"):
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)
    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            state,
            "--driver",
            driver,
            json.dumps(payload),
        ]
    ) == 0
    return store.read("status", record["id"])


def _codex_turn(store, record, monkeypatch, tmp_path, *, stop_payload=None):
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    return _hook(store, record, monkeypatch, tmp_path, "waiting",
                 {"hook_event_name": "Stop", "session_id": "c-1", **(stop_payload or {})})


# -- labels -----------------------------------------------------------------


def test_old_records_read_labels_as_empty(tmp_path):
    store, record = _session(tmp_path)
    assert "labels" not in record
    assert StateStore.labels(record) == {}
    assert StateStore.labels({"labels": "garbage"}) == {}


def test_label_set_clear_and_show(tmp_path):
    store, record = _session(tmp_path)
    updated = store.set_label(record["id"], "relay", "Eva")
    assert updated["labels"] == {"relay": "Eva"}
    assert store.find_session(record["id"])["labels"] == {"relay": "Eva"}
    store.set_label(record["id"], "relay", None)
    assert store.find_session(record["id"])["labels"] == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("Relay", "Eva"),          # uppercase key
        ("1relay", "Eva"),         # must start with a letter
        ("a" * 17, "Eva"),         # key too long
        ("re lay", "Eva"),         # space in key
        ("relay", ""),             # empty value
        ("relay", "x" * 33),       # value too long
        ("relay", "Eva\nrm -rf"),  # control character
        ("relay", "Eva\x1b[31m"),  # escape sequence
    ],
)
def test_label_validation_rejects(tmp_path, key, value):
    store, record = _session(tmp_path)
    with pytest.raises(UsageError):
        store.set_label(record["id"], key, value)
    assert "labels" not in store.find_session(record["id"])


def test_label_boundaries_accepted(tmp_path):
    store, record = _session(tmp_path)
    store.set_label(record["id"], "a" + "b" * 15, "x" * 32)
    store.set_label(record["id"], "op_er-1", "Ana B.")
    assert len(store.find_session(record["id"])["labels"]) == 2


def test_label_cap_is_eight_keys_but_overwrite_is_allowed(tmp_path):
    store, record = _session(tmp_path)
    for index in range(8):
        store.set_label(record["id"], f"k{index}", "v")
    store.set_label(record["id"], "k0", "changed")
    with pytest.raises(UsageError, match="at most 8"):
        store.set_label(record["id"], "k8", "v")
    assert store.find_session(record["id"])["labels"]["k0"] == "changed"


def test_label_takes_the_workspace_mutation_lock(tmp_path):
    store, record = _session(tmp_path)
    finished = threading.Event()

    def writer():
        store.set_label(record["id"], "relay", "Eva")
        finished.set()

    with store.mutation_lock():
        thread = threading.Thread(target=writer)
        thread.start()
        assert not finished.wait(0.3), "label wrote while the lock was held"
    thread.join(5)
    assert finished.is_set()
    assert store.find_session(record["id"])["labels"] == {"relay": "Eva"}


def test_label_survives_a_hook_write(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    store.set_label(record["id"], "relay", "Eva")
    _codex_turn(store, record, monkeypatch, tmp_path)
    assert store.find_session(record["id"])["labels"] == {"relay": "Eva"}


def test_label_cli(tmp_path, capsys):
    store, record = _session(tmp_path)
    base = ["--state-home", str(store.home), "--workspace-key", store.workspace_key]
    assert main([*base, "label", f"--session={record['id']}", "--key=relay", "--value=Eva"]) == 0
    assert json.loads(capsys.readouterr().out)["labels"] == {"relay": "Eva"}
    assert main([*base, "label", f"--session-id={record['id']}", "--key=relay", "--clear"]) == 0
    assert json.loads(capsys.readouterr().out)["labels"] == {}
    assert main([*base, "label", f"--session={record['id']}", "--key=relay"]) == 2
    assert main([*base, "label", f"--session={record['id']}", "--key=BAD", "--value=x"]) == 2


def test_info_card_shows_one_row_per_label(tmp_path):
    store, record = _session(tmp_path)
    plain = "\n".join(_card(record, {}))
    assert "🔖" not in plain
    record = store.set_label(record["id"], "relay", "Eva")
    record = store.set_label(record["id"], "operator", "web-1")
    rendered = _card(record, {})
    rows = [line for line in rendered if "🔖" in line]
    assert len(rows) == 2
    assert "operator" in rows[0] and "web-1" in rows[0]
    assert "relay" in rows[1] and "Eva" in rows[1]


# -- turn sequencing --------------------------------------------------------


def test_turn_sequence_proves_idle_after_clean_stop(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    working = _hook(store, record, monkeypatch, tmp_path, "working",
                    {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    assert working["turns"]["turn"] == 1
    assert "idle_proven" not in working
    assert TurnSequence.idle_proven(working) is False
    status = _hook(store, record, monkeypatch, tmp_path, "waiting",
                   {"hook_event_name": "Stop", "session_id": "c-1"})
    assert status["turns"]["stopped_turn"] == 1
    assert "idle_proven" not in status
    assert TurnSequence.idle_proven(status) is True
    again = _hook(store, record, monkeypatch, tmp_path, "working",
                  {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    assert again["turns"]["turn"] == 2
    assert TurnSequence.idle_proven(again) is False


def test_notification_is_not_idle_proof(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"}, driver="claude")
    status = _hook(store, record, monkeypatch, tmp_path, "waiting",
                   {"hook_event_name": "Notification", "session_id": "c-1"}, driver="claude")
    assert status["state"] == "waiting"
    assert status["turns"]["last_event"] == "Notification"
    assert TurnSequence.idle_proven(status) is False


def test_session_start_alone_is_not_idle_proof(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    status = _hook(store, record, monkeypatch, tmp_path, "waiting",
                   {"hook_event_name": "SessionStart", "session_id": "c-1"}, driver="claude")
    assert TurnSequence.idle_proven(status) is False


def test_waiting_without_event_name_is_not_a_stop(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _hook(store, record, monkeypatch, tmp_path, "working", {"session_id": "c-1"})
    status = _hook(store, record, monkeypatch, tmp_path, "waiting", {"session_id": "c-1"})
    assert status["turns"]["turn"] == 1
    assert status["turns"]["stopped_turn"] is None
    assert TurnSequence.idle_proven(status) is False


def test_blocked_stop_is_not_idle_proof(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    status = _codex_turn(store, record, monkeypatch, tmp_path,
                         stop_payload={"stop_hook_active": True})
    assert status["turns"]["stop_blocked"] is True
    assert TurnSequence.idle_proven(status) is False


def test_turns_reset_when_the_driver_changes(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    status = _codex_turn(store, record, monkeypatch, tmp_path)
    assert TurnSequence.idle_proven(status)
    other = _hook(store, record, monkeypatch, tmp_path, "waiting",
                  {"hook_event_name": "Stop", "session_id": "c-2"}, driver="claude")
    assert other["turns"]["turn"] == 0
    assert TurnSequence.idle_proven(other) is False


def test_idle_proof_rejects_missing_or_malformed_state():
    assert TurnSequence.idle_proven(None) is False
    assert TurnSequence.idle_proven({"state": "waiting", "driver": "codex"}) is False
    good = {
        "state": "waiting",
        "driver": "codex",
        "turns": {"driver": "codex", "turn": 1, "stopped_turn": 1, "stop_blocked": False},
    }
    assert TurnSequence.idle_proven(good) is True
    assert TurnSequence.idle_proven({**good, "state": "sleeping"}) is False
    assert TurnSequence.idle_proven(
        {**good, "turns": {**good["turns"], "turn": True, "stopped_turn": True}}
    ) is False
    assert TurnSequence.idle_proven(
        {**good, "turns": {**good["turns"], "stop_blocked": None}}
    ) is False


# -- typing gates (driver-rename) --------------------------------------------
#
# `nudge` (typing a caller's line + Enter into an agent pane) is gone: relay
# delivery is native and a first instruction rides `create --prompt`. The gates
# it shared with `driver-rename` stay, and are pinned here directly.


def _read_screen(fake: FakeTmux) -> bool:
    return any(call[0] == "capture-pane" for call in fake.calls)


def _gate(frame, record, *, force: bool = False) -> tuple[bool, str]:
    """pane gate then driver gate, in `driver_rename`'s order."""
    gate = AgentNudge(frame)
    allowed, reason, _detail = gate.pane_gate("%2")
    if not allowed:
        return False, reason
    return gate.driver_gate(record, force=force)


def _claude_prompt(store, record, monkeypatch, tmp_path):
    return _hook(store, record, monkeypatch, tmp_path, "working",
                 {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"}, driver="claude")


def _claude_stop(store, record, monkeypatch, tmp_path):
    return _hook(store, record, monkeypatch, tmp_path, "waiting",
                 {"hook_event_name": "Stop", "session_id": "c-1"}, driver="claude")


def _claude_notification(store, record, monkeypatch, tmp_path, kind):
    payload = {"hook_event_name": "Notification", "session_id": "c-1", "message": "m"}
    if kind:
        payload["notification_type"] = kind
    return _hook(store, record, monkeypatch, tmp_path, "waiting", payload, driver="claude")


def _claude_idle(store, record, monkeypatch, tmp_path):
    _claude_prompt(store, record, monkeypatch, tmp_path)
    return _claude_stop(store, record, monkeypatch, tmp_path)


def test_nudge_command_is_gone(tmp_path, capsys):
    store, record = _session(tmp_path, "claude")
    base = ["--state-home", str(store.home), "--workspace-key", store.workspace_key]
    with pytest.raises(SystemExit) as exc:
        main([*base, "nudge", f"--session-id={record['id']}", "--text=wake up"])
    assert exc.value.code == 2
    assert not hasattr(AgentNudge, "nudge")


def test_gate_claude_idle_in_a_one_column_pane_is_allowed(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_idle(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(command="claude", screen=ONE_COLUMN_SCREEN)
    assert _gate(_frame(store, monkeypatch, fake), record) == (True, "claude_idle")
    assert not _read_screen(fake)
    assert fake.sent == []


def test_gate_claude_is_allowed_while_working(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_prompt(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(command="claude")
    assert _gate(_frame(store, monkeypatch, fake), record) == (True, "claude_queues_input")


@pytest.mark.parametrize("kind", ["permission_prompt", "elicitation_dialog", None])
def test_gate_claude_refuses_while_a_dialog_notification_is_latest(tmp_path, monkeypatch, kind):
    store, record = _session(tmp_path, "claude")
    _claude_prompt(store, record, monkeypatch, tmp_path)
    _claude_notification(store, record, monkeypatch, tmp_path, kind)
    fake = FakeTmux(command="claude", screen=ONE_COLUMN_SCREEN)
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "claude_dialog_open")
    assert not _read_screen(fake)
    # the next Stop is newer than the dialog: the dialog has closed
    _claude_stop(store, record, monkeypatch, tmp_path)
    assert _gate(_frame(store, monkeypatch, fake), record)[0] is True


def test_gate_claude_dialog_after_a_stop_is_refused(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_idle(store, record, monkeypatch, tmp_path)
    _claude_notification(store, record, monkeypatch, tmp_path, "permission_prompt")
    fake = FakeTmux(command="claude")
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "claude_dialog_open")


def test_gate_claude_idle_reminder_notification_is_harmless(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_notification(store, record, monkeypatch, tmp_path, "idle_prompt")
    fake = FakeTmux(command="claude")
    assert _gate(_frame(store, monkeypatch, fake), record)[0] is True


def test_gate_claude_without_hook_state_is_refused_even_when_forced(tmp_path, monkeypatch):
    # no hook has run yet — e.g. the folder-trust prompt; the width is irrelevant
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude", screen=ONE_COLUMN_SCREEN)
    assert _gate(_frame(store, monkeypatch, fake), record, force=True) == (False, "state_unknown")
    assert not _read_screen(fake)


def test_gate_claude_ignores_another_drivers_hook_state(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _codex_turn(store, record, monkeypatch, tmp_path)  # left by a codex driver
    fake = FakeTmux(command="claude")
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "state_unknown")


def test_gate_codex_working_is_refused_unless_forced(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    fake = FakeTmux()
    frame = _frame(store, monkeypatch, fake)
    assert _gate(frame, record) == (False, "codex_not_idle")
    assert _gate(frame, record, force=True) == (True, "forced")


@pytest.mark.parametrize("screen", [None, ONE_COLUMN_SCREEN])
def test_gate_codex_unknown_state_is_refused_at_any_width(tmp_path, monkeypatch, screen):
    store, record = _session(tmp_path)  # no hook event ever arrived
    fake = FakeTmux(screen=screen)
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "codex_not_idle")


def test_gate_codex_blocked_stop_is_refused(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path, stop_payload={"stop_hook_active": True})
    fake = FakeTmux()
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "codex_not_idle")


def test_gate_codex_idle_proven_in_a_one_column_pane_is_allowed(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(screen=ONE_COLUMN_SCREEN)
    assert _gate(_frame(store, monkeypatch, fake), record) == (True, "codex_idle_proven")
    assert not _read_screen(fake)


def test_driver_rename_pauses_between_the_text_and_enter(tmp_path, monkeypatch):
    """Text then Enter in one burst reads as a paste to the Codex TUI, which keeps
    the Enter as a newline instead of submitting. The pause sits strictly between
    the literal text and the Enter keypress."""
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux()
    events: list[object] = []
    real_call = fake.__call__

    def recording_tmux(args, **kwargs):
        if args and args[0] == "send-keys":
            events.append(args[-1])
        return real_call(args, **kwargs)

    frame = _frame(store, monkeypatch, fake)
    monkeypatch.setattr(frame, "tmux", recording_tmux)

    class RecordingGate(AgentNudge):
        def __init__(self, gate_frame):
            super().__init__(gate_frame, sleep=lambda seconds: events.append(("sleep", seconds)))

    monkeypatch.setattr("hive_ide.nudge.AgentNudge", RecordingGate)
    frame.driver_rename(record)
    assert AgentNudge.ENTER_DELAY_SECONDS >= 0.5
    assert events == ["/rename S", ("sleep", AgentNudge.ENTER_DELAY_SECONDS), "Enter"]


def test_gate_refuses_a_shell_left_in_the_agent_pane(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="fish")
    assert _gate(_frame(store, monkeypatch, fake), record, force=True) == (
        False, "agent_not_running"
    )


WRAPPER_ARGS = (
    "sh -c unset NO_COLOR; claude --resume c-1 --name 'S'; status=$?; "
    'if [ "$status" -ne 0 ]; then printf x; fi; exec "${SHELL:-/bin/sh}"'
)


def test_gate_allows_claude_running_under_the_pane_wrapper(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_prompt(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(
        command="sh",
        processes=[("1", "4100", WRAPPER_ARGS), ("4100", "4109", "claude --resume c-1")],
    )
    assert _gate(_frame(store, monkeypatch, fake), record) == (True, "claude_queues_input")


def test_gate_refuses_the_pane_wrapper_with_no_driver_child(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(
        command="sh",
        processes=[("1", "4100", WRAPPER_ARGS), ("4100", "4109", "bash -c true")],
    )
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "agent_not_running")


def test_gate_refuses_a_login_shell_even_with_a_child(tmp_path, monkeypatch):
    # the wrapper exec'd fish after the driver exited; a command the human ran
    # from that prompt is not the agent
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(
        command="fish",
        processes=[("1", "4100", "-fish"), ("4100", "4109", "claude")],
    )
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "agent_not_running")


def test_gate_refuses_when_the_process_table_is_unreadable(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="sh", ps_ok=False)
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "pane_unobservable")


def test_ps_failure_and_timeout_read_as_unobservable(monkeypatch):
    def timed_out(*_args, **kwargs):
        assert kwargs["timeout"] == AgentNudge.TMUX_TIMEOUT
        raise subprocess.TimeoutExpired("ps", kwargs["timeout"])

    monkeypatch.setattr("hive_ide.nudge.subprocess.run", timed_out)
    assert AgentNudge(None).wrapped_agent_running("4100") is None
    monkeypatch.setattr(
        "hive_ide.nudge.subprocess.run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="ps: boom"),
    )
    assert AgentNudge(None).wrapped_agent_running("4100") is None


def test_gate_refuses_a_pane_in_copy_mode(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_idle(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(command="claude", in_mode="1")
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "pane_in_mode")


def test_gate_refuses_a_dead_pane(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_idle(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(command="claude", dead="1")
    assert _gate(_frame(store, monkeypatch, fake), record) == (False, "agent_not_running")


def test_gate_refuses_unsupported_driver_even_when_forced(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "term")
    fake = FakeTmux(command="claude")
    assert AgentNudge(_frame(store, monkeypatch, fake)).driver_gate(record, force=True) == (
        False, "unsupported_driver"
    )


# -- driver rename guard ------------------------------------------------------


def test_driver_rename_refuses_codex_mid_turn(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    fake = FakeTmux()
    with pytest.raises(UsageError, match="codex_not_idle"):
        _frame(store, monkeypatch, fake).driver_rename(record)
    assert fake.sent == []


def test_driver_rename_sends_to_idle_proven_codex(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(screen=ONE_COLUMN_SCREEN)
    result = _frame(store, monkeypatch, fake).driver_rename(record)
    assert result["sent"] == "/rename"
    assert fake.sent == [
        ["send-keys", "-l", "-t", "%2", "/rename S"],
        ["send-keys", "-t", "%2", "Enter"],
    ]


def test_driver_rename_claude_while_working(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_prompt(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(command="claude", clients=f"{NOW}\t%2\n")
    _frame(store, monkeypatch, fake).driver_rename(record)
    # no list-clients (the requester of a rename is the human at the keyboard)
    # and no capture-pane
    assert [call[0] for call in fake.calls] == ["display-message", "send-keys", "send-keys"]


@pytest.mark.parametrize("dialog", [True, False])
def test_driver_rename_refuses_claude_dialog_or_unknown_state(tmp_path, monkeypatch, dialog):
    store, record = _session(tmp_path, "claude")
    if dialog:
        _claude_notification(store, record, monkeypatch, tmp_path, "permission_prompt")
    fake = FakeTmux(command="claude")
    reason = "claude_dialog_open" if dialog else "state_unknown"
    with pytest.raises(UsageError, match=f"Claude is not ready.*{reason}"):
        _frame(store, monkeypatch, fake).driver_rename(record, force=True)
    assert fake.sent == []


@pytest.mark.parametrize(
    ("driver_id", "fake_kwargs", "reason"),
    [
        ("claude", {"in_mode": "1"}, "pane_in_mode"),
        ("codex", {"in_mode": "1"}, "pane_in_mode"),
        ("claude", {"dead": "1"}, "agent_not_running"),
        ("codex", {"dead": "1"}, "agent_not_running"),
        ("claude", {"command": "bash"}, "agent_not_running"),
        ("codex", {"command": "fish"}, "agent_not_running"),
    ],
)
def test_driver_rename_applies_the_pane_gate_even_when_forced(
    tmp_path, monkeypatch, driver_id, fake_kwargs, reason
):
    store, record = _session(tmp_path, driver_id)
    fake = FakeTmux(**{"command": driver_id, **fake_kwargs})
    with pytest.raises(UsageError, match=reason):
        _frame(store, monkeypatch, fake).driver_rename(record, force=True)
    assert fake.sent == []


# -- hook state ---------------------------------------------------------------


def test_codex_idle_proof_survives_the_window_being_viewed(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    assert IdeSeen.mark(store.home, store.workspace_key, record["id"]) is True
    assert store.read("status", record["id"])["state"] == "idle"
    fake = FakeTmux()
    assert _gate(_frame(store, monkeypatch, fake), record) == (True, "codex_idle_proven")


def test_hook_state_is_observed_only_for_its_own_driver(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    assert TurnSequence.observed(None, "codex") is False
    status = _codex_turn(store, record, monkeypatch, tmp_path)
    assert TurnSequence.observed(status, "codex") is True
    assert TurnSequence.observed(status, "claude") is False
    assert TurnSequence.observed({"turns": "garbage"}, "codex") is False


def test_status_event_carries_turns_but_never_proves_idle_for_working(tmp_path, monkeypatch, capsys):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    base = ["--state-home", str(store.home), "--workspace-key", store.workspace_key]
    assert main([*base, "status-event", f"--session-id={record['id']}",
                 "--state=working", "--driver=codex"]) == 0
    status = store.read("status", record["id"])
    assert status["turns"]["stopped_turn"] == 1
    assert TurnSequence.idle_proven(status) is False
    assert main([*base, "status-event", f"--session-id={record['id']}",
                 "--state=waiting", "--driver=claude"]) == 0
    assert TurnSequence.idle_proven(store.read("status", record["id"])) is False
    assert main([*base, "status-event", f"--session-id={record['id']}",
                 "--state=waiting", "--driver=codex"]) == 0
    assert TurnSequence.idle_proven(store.read("status", record["id"])) is True


def test_state_compat_update_keeps_turns_and_fails_safe(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    StateIO.write_session_status_update(store.home, record, {"state": "working"})
    status = store.read("status", record["id"])
    assert status["turns"]["stopped_turn"] == 1
    assert TurnSequence.idle_proven(status) is False



# -- timeouts -----------------------------------------------------------------


def test_every_gate_tmux_call_carries_a_timeout(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux()
    _gate(_frame(store, monkeypatch, fake), record)
    assert fake.calls and all(timeout == AgentNudge.TMUX_TIMEOUT for timeout in fake.timeouts)


def test_frame_tmux_timeout_becomes_a_failed_result(tmp_path, monkeypatch):
    store, _record = _session(tmp_path)
    seen = {}

    def wedged(argv, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr("hive_ide.frame.subprocess.run", wedged)
    result = Frame(store).tmux(["list-clients"], timeout=0.5)
    assert seen["timeout"] == 0.5
    assert result.returncode == 124
    assert "timed out" in result.stderr


def test_a_wedged_tmux_probe_refuses_the_rename(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    calls = []

    def wedged(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr("hive_ide.frame.subprocess.run", wedged)
    with pytest.raises(UsageError, match="pane_unobservable"):
        frame.driver_rename(record)
    assert not any("send-keys" in argv for argv in calls)


# -- cross-process locking ----------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parents[1]


def _child_env(tmp_path, store, record) -> dict:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HIVE_IDE_") and key not in {"TMUX", "TMUX_PANE"}
    }
    env.update(
        PYTHONPATH=str(REPO_ROOT),
        HIVE_IDE_STATE_HOME=str(store.home),
        HIVE_IDE_WORKSPACE_KEY=store.workspace_key,
        HIVE_IDE_SESSION_ID=record["id"],
        HIVE_IDE_CONFIG=str(tmp_path / "missing-config.json"),
    )
    return env


def _blocked_then_done(store, proc) -> int:
    time.sleep(0.8)
    assert proc.poll() is None, "child wrote while another process held the lock"
    return proc


def test_label_cli_waits_for_the_workspace_lock_across_processes(tmp_path):
    store, record = _session(tmp_path)
    argv = [
        sys.executable, "-m", "hive_ide.cli",
        "--state-home", str(store.home), "--workspace-key", store.workspace_key,
        "label", f"--session={record['id']}", "--key=relay", "--value=Eva",
    ]
    with store.mutation_lock():
        proc = subprocess.Popen(
            argv, env=_child_env(tmp_path, store, record), cwd=tmp_path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _blocked_then_done(store, proc)
    assert proc.wait(30) == 0, proc.stderr.read()
    assert store.find_session(record["id"])["labels"] == {"relay": "Eva"}


def test_hook_process_waits_for_a_label_write_and_keeps_it(tmp_path):
    store, record = _session(tmp_path)
    argv = [
        sys.executable, "-m", "hive_ide.hook",
        "--state-home", str(store.home), "--state", "working", "--driver", "codex",
        json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "c-1"}),
    ]
    with store.mutation_lock():
        proc = subprocess.Popen(
            argv, env=_child_env(tmp_path, store, record), cwd=tmp_path,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        _blocked_then_done(store, proc)
        store.set_label(record["id"], "relay", "Eva")
    assert proc.wait(30) == 0
    assert store.find_session(record["id"])["labels"] == {"relay": "Eva"}
    assert store.read("status", record["id"])["turns"]["turn"] == 1


def test_a_timed_out_rename_fails_clearly_and_releases_the_lock(tmp_path, monkeypatch, capsys):
    store, record = _session(tmp_path, "claude")
    _claude_prompt(store, record, monkeypatch, tmp_path)
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    fake = FakeTmux(command="claude")
    timeouts = []

    def tmux(args, **kwargs):
        if args[0] == "send-keys":
            timeouts.append(kwargs.get("timeout"))
            return SimpleNamespace(
                returncode=124, stdout="", stderr="tmux timed out after 3.0s"
            )
        return fake(args, **kwargs)

    monkeypatch.setattr(frame, "tmux", tmux)
    monkeypatch.setattr("hive_ide.cli.Frame", lambda *_args, **_kwargs: frame)
    base = ["--state-home", str(store.home), "--workspace-key", store.workspace_key]
    assert main([*base, "driver-rename", f"--session-id={record['id']}"]) == 2
    assert "Could not send driver rename: tmux timed out after 3.0s" in capsys.readouterr().err
    assert timeouts == [AgentNudge.TMUX_TIMEOUT]
    # the CLI took the workspace lock around the command; it must be free again
    with store.mutation_lock(blocking=False):
        pass
    assert not store.mutation_lock_held()


def test_rename_send_keys_carry_the_timeout(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _claude_prompt(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(command="claude")
    _frame(store, monkeypatch, fake).driver_rename(record)
    sends = [t for call, t in zip(fake.calls, fake.timeouts) if call[0] == "send-keys"]
    assert sends == [AgentNudge.TMUX_TIMEOUT, AgentNudge.TMUX_TIMEOUT]
