"""Session labels, hook turn sequencing, nudge, and the driver-rename guard."""

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
IDLE_CODEX_SCREEN = [
    "• Done. The tests pass.",
    "",
    "› Ask Codex to do anything",
    "",
    "  ? for shortcuts                                   100% context left",
]
WORKING_CODEX_SCREEN = [
    "• Running the tests",
    "",
    "• Working (12s • esc to interrupt)",
    "",
    "› Ask Codex to do anything",
    "",
    "  tab to queue message                              90% context left",
]
APPROVAL_CODEX_SCREEN = [
    "Would you like to run the following command?",
    "  $ pnpm test",
    "› 1. Yes, proceed",
    "  2. No, and tell Codex what to do differently",
    "  Press enter to confirm or esc to cancel",
]
RULE = "─" * 40
IDLE_CLAUDE_SCREEN = ["⏺ Done.", "", RULE, "> ", RULE, "  ? for shortcuts"]
WORKING_CLAUDE_SCREEN = [
    "> earlier prompt echoed in the transcript",
    "✻ Working… (12s · esc to interrupt)",
    "",
    RULE,
    ">",
    RULE,
    "  ? for shortcuts",
]
BOX_CLAUDE_SCREEN = ["⏺ Done.", "╭" + "─" * 30 + "╮", "│ >                            │", "╰" + "─" * 30 + "╯"]
HINT_CLAUDE_SCREEN = [RULE, '> Try "write a test for app.py"', RULE]
DRAFT_CLAUDE_SCREEN = ["⏺ Done.", RULE, "> half-typed human draft", RULE, "  ? for shortcuts"]
MULTILINE_DRAFT_CLAUDE_SCREEN = ["⏺ Done.", RULE, ">", "  second line of a draft", RULE]
UNRECOGNIZED_CLAUDE_SCREEN = ["⏺ Done.", "> something without rules"]
PERMISSION_CLAUDE_SCREEN = [
    "Bash command",
    "  pnpm test",
    "Do you want to proceed?",
    "❯ 1. Yes",
    "  2. Yes, and don't ask again for pnpm commands in this project",
    "  3. No, and tell Claude what to do differently (esc)",
]
PLAN_CLAUDE_SCREEN = [
    "Ready to code?",
    "Here is Claude's plan:",
    "  1. Add the guard",
    "Claude has written up a plan and is ready to execute. Would you like to proceed?",
    "❯ 1. Yes, and auto-accept edits",
    "  2. Yes, and manually approve edits",
    "  3. No, keep planning",
]
QUESTION_CLAUDE_SCREEN = [
    "Which database?",
    "❯ 1. Postgres",
    "  2. SQLite",
    "↑/↓ to navigate · Enter to select · Esc to cancel",
]
DRAFT_CODEX_SCREEN = [
    "• Done.",
    "",
    "› half-typed human draft",
    "",
    "  ? for shortcuts                                   100% context left",
]


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
    """Answers the tmux calls nudge/rename make; records every call."""

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
    ):
        self.screen = screen or []
        self.command = command
        self.in_mode = in_mode
        self.dead = dead
        self.clients = clients
        self.clients_rc = clients_rc
        self.target = target
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
                stdout=f"{self.in_mode}\t{self.command}\t{self.dead}\n",
                stderr="",
            )
        if verb == "list-clients":
            return SimpleNamespace(returncode=self.clients_rc, stdout=self.clients, stderr="")
        if verb == "capture-pane":
            return SimpleNamespace(returncode=0, stdout="\n".join(self.screen) + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @property
    def sent(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "send-keys"]


def _frame(store, monkeypatch, fake: FakeTmux) -> Frame:
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    monkeypatch.setattr(frame, "tmux", fake)
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


# -- nudge ------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "x" * 201,
        "line one\nline two",
        "tab\there",
        "esc \x1b[2J",
        "café",
        "/clear",
        "!rm -rf ~",
        "# remember this",
        "-R",
        "-t %0 C-c",
        " leading space",
        "trailing space ",
    ],
)
def test_nudge_refuses_unsafe_text_without_touching_tmux(tmp_path, monkeypatch, text):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude")
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, text)
    assert result["sent"] is False
    assert result["reason"] == "unsafe_text"
    assert fake.calls == []


def test_nudge_claude_is_allowed_while_working(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"}, driver="claude")
    fake = FakeTmux(command="claude", screen=WORKING_CLAUDE_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "Relay: unread in agent.abc. Run ./hive relay read."
    )
    assert result["sent"] is True
    assert fake.sent == [
        ["send-keys", "-l", "-t", "%2", "--",
         "Relay: unread in agent.abc. Run ./hive relay read."],
        ["send-keys", "-t", "%2", "Enter"],
    ]


def test_nudge_codex_working_is_refused(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    fake = FakeTmux(screen=WORKING_CODEX_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result == {
        "session_id": record["id"],
        "driver": "codex",
        "forced": False,
        "sent": False,
        "reason": "codex_not_idle_proven",
    }
    assert fake.sent == []


def test_nudge_codex_unknown_state_is_refused(tmp_path, monkeypatch):
    store, record = _session(tmp_path)  # no hook event ever arrived
    fake = FakeTmux(screen=IDLE_CODEX_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "codex_not_idle_proven"
    assert fake.sent == []


def test_nudge_codex_blocked_stop_is_refused(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path, stop_payload={"stop_hook_active": True})
    fake = FakeTmux(screen=IDLE_CODEX_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "codex_not_idle_proven"
    assert fake.sent == []


@pytest.mark.parametrize("screen", [WORKING_CODEX_SCREEN, APPROVAL_CODEX_SCREEN, DRAFT_CODEX_SCREEN, []])
def test_nudge_codex_idle_proof_still_needs_the_prompt_signature(tmp_path, monkeypatch, screen):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(screen=screen)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "codex_prompt_not_recognized"
    assert fake.sent == []


def test_nudge_codex_idle_proven_with_signature_is_sent(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(screen=IDLE_CODEX_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["sent"] is True
    assert result["reason"] == "codex_idle_proven"
    assert fake.sent == [
        ["send-keys", "-l", "-t", "%2", "--", "wake up"],
        ["send-keys", "-t", "%2", "Enter"],
    ]


def test_nudge_codex_forced_is_sent_while_working(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    fake = FakeTmux(screen=WORKING_CODEX_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "wake up", force=True
    )
    assert result["sent"] is True
    assert result["forced"] is True
    assert result["reason"] == "forced"
    assert len(fake.sent) == 2


@pytest.mark.parametrize("driver_id", ["claude", "codex"])
def test_nudge_refuses_recent_human_keystroke_even_when_forced(tmp_path, monkeypatch, driver_id):
    store, record = _session(tmp_path, driver_id)
    fake = FakeTmux(command=driver_id, clients=f"{NOW - 10}\t%2\n")
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "wake up", force=True
    )
    assert result["reason"] == "human_active"
    assert fake.sent == []


def test_nudge_ignores_old_or_other_pane_client_activity(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(
        command="claude", screen=IDLE_CLAUDE_SCREEN,
        clients=f"{NOW - 61}\t%2\n{NOW - 1}\t%1\n",
    )
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["sent"] is True


def test_nudge_refuses_when_client_activity_is_unobservable(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude", clients_rc=1)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "human_activity_unknown"
    assert fake.sent == []


def test_nudge_refuses_a_shell_left_in_the_agent_pane(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="fish")
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "wake up", force=True
    )
    assert result["reason"] == "agent_not_running"
    assert fake.sent == []


def test_nudge_refuses_a_pane_in_copy_mode(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude", in_mode="1")
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "pane_in_mode"
    assert fake.sent == []


def test_nudge_refuses_unsupported_driver(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "term")
    fake = FakeTmux(command="fish")
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "wake up", force=True
    )
    assert result["reason"] == "unsupported_driver"
    assert fake.calls == []


def test_nudge_cli_reports_a_structured_refusal(tmp_path, capsys):
    store, record = _session(tmp_path)
    base = ["--state-home", str(store.home), "--workspace-key", store.workspace_key]
    assert main([*base, "nudge", f"--session={record['id']}", "--text=/clear"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sent"] is False
    assert result["reason"] == "unsafe_text"


# -- driver rename guard ------------------------------------------------------


def test_driver_rename_refuses_codex_mid_turn(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"})
    fake = FakeTmux(screen=WORKING_CODEX_SCREEN)
    with pytest.raises(UsageError, match="codex_not_idle_proven"):
        _frame(store, monkeypatch, fake).driver_rename(record)
    assert fake.sent == []


def test_driver_rename_refuses_codex_whose_screen_shows_a_turn_running(tmp_path, monkeypatch):
    # Hook says idle (e.g. a Stop another hook blocked), the screen says working.
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(screen=WORKING_CODEX_SCREEN)
    with pytest.raises(UsageError, match="codex_prompt_not_recognized"):
        _frame(store, monkeypatch, fake).driver_rename(record)
    assert any(call[0] == "capture-pane" for call in fake.calls)
    assert fake.sent == []


def test_driver_rename_sends_to_idle_proven_codex(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(screen=IDLE_CODEX_SCREEN)
    result = _frame(store, monkeypatch, fake).driver_rename(record)
    assert result["sent"] == "/rename"
    assert fake.sent == [
        ["send-keys", "-l", "-t", "%2", "/rename S"],
        ["send-keys", "-t", "%2", "Enter"],
    ]


def test_driver_rename_claude_while_working_with_an_empty_composer(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"}, driver="claude")
    fake = FakeTmux(command="claude", screen=WORKING_CLAUDE_SCREEN, clients=f"{NOW}\t%2\n")
    _frame(store, monkeypatch, fake).driver_rename(record)
    # no list-clients: the requester of a rename is the human at the keyboard
    assert [call[0] for call in fake.calls] == [
        "display-message", "capture-pane", "send-keys", "send-keys",
    ]


@pytest.mark.parametrize("screen", [PERMISSION_CLAUDE_SCREEN, PLAN_CLAUDE_SCREEN, DRAFT_CLAUDE_SCREEN])
def test_driver_rename_refuses_claude_dialog_or_draft(tmp_path, monkeypatch, screen):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude", screen=screen)
    with pytest.raises(UsageError, match="Claude is not ready"):
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
    fake = FakeTmux(**{"command": driver_id, "screen": IDLE_CLAUDE_SCREEN, **fake_kwargs})
    with pytest.raises(UsageError, match=reason):
        _frame(store, monkeypatch, fake).driver_rename(record, force=True)
    assert fake.sent == []


# -- signature unit -----------------------------------------------------------


def test_codex_signature_requires_placeholder_near_the_bottom():
    assert AgentNudge.codex_prompt_signature(IDLE_CODEX_SCREEN) is True
    assert AgentNudge.codex_prompt_signature(
        ["› Ask a follow-up question", "  ? for shortcuts"]
    ) is True
    far = ["› Ask Codex to do anything", *[f"output {i}" for i in range(20)]]
    assert AgentNudge.codex_prompt_signature(far) is False
    assert AgentNudge.codex_prompt_signature(None) is False
    assert AgentNudge.codex_prompt_signature(["", "   "]) is False


def test_codex_idle_proof_survives_the_window_being_viewed(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    assert IdeSeen.mark(store.home, store.workspace_key, record["id"]) is True
    assert store.read("status", record["id"])["state"] == "idle"
    fake = FakeTmux(screen=IDLE_CODEX_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["sent"] is True
    assert result["reason"] == "codex_idle_proven"


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


# -- Claude dialog / draft guard ------------------------------------------------


@pytest.mark.parametrize(
    ("screen", "reason"),
    [
        (PERMISSION_CLAUDE_SCREEN, "claude_dialog_open"),
        (PLAN_CLAUDE_SCREEN, "claude_dialog_open"),
        (QUESTION_CLAUDE_SCREEN, "claude_dialog_open"),
        (DRAFT_CLAUDE_SCREEN, "claude_draft_present"),
        (MULTILINE_DRAFT_CLAUDE_SCREEN, "claude_draft_present"),
        (UNRECOGNIZED_CLAUDE_SCREEN, "claude_composer_not_recognized"),
        ([], "claude_composer_not_recognized"),
    ],
)
def test_nudge_claude_refuses_dialogs_drafts_and_unknown_screens_even_forced(
    tmp_path, monkeypatch, screen, reason
):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude", screen=screen)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "wake up", force=True
    )
    assert result["reason"] == reason
    assert fake.sent == []


@pytest.mark.parametrize("screen", [IDLE_CLAUDE_SCREEN, BOX_CLAUDE_SCREEN, HINT_CLAUDE_SCREEN])
def test_nudge_claude_empty_composer_styles_are_sent(tmp_path, monkeypatch, screen):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude", screen=screen)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["sent"] is True


@pytest.mark.parametrize("kind", ["permission_prompt", "elicitation_dialog", None])
def test_nudge_claude_refuses_while_a_dialog_notification_is_latest(tmp_path, monkeypatch, kind):
    store, record = _session(tmp_path, "claude")
    _hook(store, record, monkeypatch, tmp_path, "working",
          {"hook_event_name": "UserPromptSubmit", "session_id": "c-1"}, driver="claude")
    payload = {"hook_event_name": "Notification", "session_id": "c-1", "message": "m"}
    if kind:
        payload["notification_type"] = kind
    _hook(store, record, monkeypatch, tmp_path, "waiting", payload, driver="claude")
    fake = FakeTmux(command="claude", screen=IDLE_CLAUDE_SCREEN)  # screen looks fine
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "claude_notification_pending"
    assert fake.sent == []
    _hook(store, record, monkeypatch, tmp_path, "waiting",
          {"hook_event_name": "Stop", "session_id": "c-1"}, driver="claude")
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["sent"] is True


def test_nudge_claude_idle_reminder_notification_is_harmless(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    _hook(store, record, monkeypatch, tmp_path, "waiting",
          {"hook_event_name": "Notification", "session_id": "c-1",
           "notification_type": "idle_prompt"}, driver="claude")
    fake = FakeTmux(command="claude", screen=IDLE_CLAUDE_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["sent"] is True


def test_nudge_refuses_a_dead_pane(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    fake = FakeTmux(command="claude", dead="1", screen=IDLE_CLAUDE_SCREEN)
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "agent_not_running"
    assert fake.sent == []


# -- timeouts -----------------------------------------------------------------


def test_every_nudge_tmux_call_carries_a_timeout(tmp_path, monkeypatch):
    store, record = _session(tmp_path)
    _codex_turn(store, record, monkeypatch, tmp_path)
    fake = FakeTmux(screen=IDLE_CODEX_SCREEN)
    AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(record, "wake up")
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


def test_a_wedged_tmux_probe_refuses_the_nudge(tmp_path, monkeypatch):
    store, record = _session(tmp_path, "claude")
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    calls = []

    def wedged(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr("hive_ide.frame.subprocess.run", wedged)
    result = AgentNudge(frame, clock=lambda: NOW).nudge(record, "wake up")
    assert result["reason"] == "pane_unobservable"
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
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    fake = FakeTmux(command="claude", screen=IDLE_CLAUDE_SCREEN)
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
    fake = FakeTmux(command="claude", screen=IDLE_CLAUDE_SCREEN)
    _frame(store, monkeypatch, fake).driver_rename(record)
    sends = [t for call, t in zip(fake.calls, fake.timeouts) if call[0] == "send-keys"]
    assert sends == [AgentNudge.TMUX_TIMEOUT, AgentNudge.TMUX_TIMEOUT]
