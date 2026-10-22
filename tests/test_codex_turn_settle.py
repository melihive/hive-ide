"""A Codex turn that ends in an error runs no hook, so it must not read as `working` forever.

Codex 0.163 has no failure hook event and skips `Stop` when a turn fails (an API
400, a usage limit). The `UserPromptSubmit` status would then say `working`
until the next prompt, and anything that holds work for an idle agent holds it
indefinitely. `UserPromptSubmit` records codex's `turn_id`; readers settle the
turn when codex's own rollout records its end.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from hive_ide.drivers import ConversationProbe, bundled_drivers
from hive_ide.frame import Frame
from hive_ide.hook import IdeHook
from hive_ide.nudge import AgentNudge
from hive_ide.state_compat import StateIO
from hive_ide.store import StateStore
from hive_ide.turns import TurnSequence

from test_labels_nudge import FakeTmux, NOW, _frame


THREAD = "01a16431-b530-7ed3-aefb-4f1141e46a23"
TURN = "01a16431-b58c-7951-9b66-b0dd9b5a9692"
EARLIER_TURN = "01a16420-9688-7c83-a7ee-27afa52afbac"


def _event(kind: str, turn_id: str, **extra) -> dict:
    return {
        "timestamp": "2026-10-23T14:23:21.000Z",
        "type": "event_msg",
        "payload": {"type": kind, "turn_id": turn_id, **extra},
    }


def _rollout(codex_home: Path, records: list[dict], *, padding: int = 0) -> Path:
    day = codex_home / "sessions" / "2026" / "10" / "23"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-23T14-23-19-{THREAD}.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {"id": THREAD, "cwd": "/w"}})]
    if padding:
        lines.append(json.dumps({"type": "response_item", "payload": {"text": "x" * padding}}))
    lines += [json.dumps(record) for record in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _errored_turn(turn_id: str = TURN) -> list[dict]:
    """What codex wrote on mim for a turn the API rejected (400) after ~1 s."""
    return [
        _event("task_started", turn_id, started_at=1792765400),
        {"timestamp": "2026-10-23T14:23:21.300Z", "type": "event_msg",
         "payload": {"type": "error", "message": "400 model not supported",
                     "codex_error_info": "bad_request"}},
        _event("task_complete", turn_id, last_agent_message=None, duration_ms=1100),
    ]


@pytest.fixture()
def codex_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "codex-home"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _session(tmp_path) -> tuple[StateStore, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="S",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver=bundled_drivers()["codex"].resolve(
            name="S", working_dir=str(workspace), conversation_reference=None
        ),
    )
    return store, record


def _prompt(store, record, monkeypatch, tmp_path, payload: dict | None = None) -> dict:
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)
    body = {"hook_event_name": "UserPromptSubmit", "session_id": THREAD, "turn_id": TURN}
    if payload is not None:
        body = payload
    assert IdeHook.main(
        ["--state-home", str(store.home), "--state", "working", "--driver", "codex",
         json.dumps(body)]
    ) == 0
    return store.read("status", record["id"])


# -- the rollout probe: three states ------------------------------------------


def test_probe_errored_turn_has_ended(codex_home):
    _rollout(codex_home, _errored_turn())
    assert ConversationProbe.codex_turn_ended(THREAD, TURN) is True


def test_probe_interrupted_turn_has_ended(codex_home):
    _rollout(codex_home, [_event("task_started", TURN),
                          _event("turn_aborted", TURN, reason="interrupted")])
    assert ConversationProbe.codex_turn_ended(THREAD, TURN) is True


def test_probe_open_turn_has_not_ended(codex_home):
    _rollout(codex_home, [_event("task_started", TURN), _event("token_count", TURN)])
    assert ConversationProbe.codex_turn_ended(THREAD, TURN) is False


def test_probe_reads_only_the_tail_of_a_large_rollout(codex_home):
    _rollout(codex_home, _errored_turn(),
             padding=ConversationProbe.CODEX_TAIL_BYTES * 2)
    assert ConversationProbe.codex_turn_ended(THREAD, TURN) is True


@pytest.mark.parametrize(
    "setup, reference, turn_id",
    [
        ("none", THREAD, TURN),                      # no rollout on disk
        ("other_turn", THREAD, TURN),                # only an EARLIER turn ended
        ("errored", "not-a-uuid", TURN),             # a session name, not a thread id
        ("errored", THREAD, ""),                     # no turn id
        ("garbage", THREAD, TURN),                   # unparseable rollout
    ],
)
def test_probe_unknown_is_none_never_ended(codex_home, setup, reference, turn_id):
    if setup == "errored":
        _rollout(codex_home, _errored_turn())
    elif setup == "other_turn":
        _rollout(codex_home, _errored_turn(EARLIER_TURN))
    elif setup == "garbage":
        path = _rollout(codex_home, [])
        path.write_text(f"{{not json {TURN}\n", encoding="utf-8")
    assert ConversationProbe.codex_turn_ended(reference, turn_id) is None


def test_probe_without_a_sessions_dir_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    assert ConversationProbe.codex_turn_ended(THREAD, TURN) is None


# -- the hook records the turn id; readers settle the errored turn ------------


def test_prompt_hook_records_codex_turn_id(tmp_path, monkeypatch, codex_home):
    store, record = _session(tmp_path)
    status = _prompt(store, record, monkeypatch, tmp_path)
    assert status["state"] == "working"
    assert status["turns"]["turn_id"] == TURN
    assert status["turns"]["last_event"] == "UserPromptSubmit"


def test_errored_codex_turn_reads_as_ended_and_idle(tmp_path, monkeypatch, codex_home):
    """The mim probe: prompt hook fires, the turn 400s, no Stop ever arrives."""
    store, record = _session(tmp_path)
    _prompt(store, record, monkeypatch, tmp_path)
    _rollout(codex_home, _errored_turn())

    raw = store.read("status", record["id"])
    assert raw["state"] == "working", "a reader must not rewrite the stored document"

    read = StateIO.read_session_status(store.home, record)
    assert read["state"] == "waiting"
    assert read["settled_by"] == "codex_rollout"
    assert read["turns"]["stopped_turn"] == read["turns"]["turn"] == 1
    assert read["turns"]["last_event"] == TurnSequence.TURN_ENDED
    assert TurnSequence.idle_proven(read) is True
    assert TurnSequence.attention_pending(read) is False


def test_nudge_reaches_codex_after_an_errored_turn(tmp_path, monkeypatch, codex_home):
    store, record = _session(tmp_path)
    _prompt(store, record, monkeypatch, tmp_path)
    _rollout(codex_home, _errored_turn())
    fake = FakeTmux()
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "wake up"
    )
    assert result["sent"] is True
    assert result["reason"] == "codex_idle_proven"


@pytest.mark.parametrize(
    "rollout",
    [
        None,                                        # rollout not found: unknown
        [_event("task_started", TURN)],              # the turn is genuinely running
        _errored_turn(EARLIER_TURN),                 # the PREVIOUS turn errored
    ],
)
def test_nudge_still_refuses_codex_while_its_turn_is_not_known_ended(
    tmp_path, monkeypatch, codex_home, rollout
):
    store, record = _session(tmp_path)
    _prompt(store, record, monkeypatch, tmp_path)
    if rollout is not None:
        _rollout(codex_home, rollout)
    fake = FakeTmux()
    result = AgentNudge(_frame(store, monkeypatch, fake), clock=lambda: NOW).nudge(
        record, "wake up"
    )
    assert result["reason"] == "codex_not_idle"
    assert fake.sent == []
    assert StateIO.read_session_status(store.home, record)["state"] == "working"


def test_prompt_without_a_turn_id_is_never_settled(tmp_path, monkeypatch, codex_home):
    store, record = _session(tmp_path)
    _prompt(store, record, monkeypatch, tmp_path,
            payload={"hook_event_name": "UserPromptSubmit", "session_id": THREAD})
    _rollout(codex_home, _errored_turn())
    assert StateIO.read_session_status(store.home, record)["state"] == "working"


def test_next_prompt_replaces_a_settled_turn(tmp_path, monkeypatch, codex_home):
    store, record = _session(tmp_path)
    _prompt(store, record, monkeypatch, tmp_path)
    _rollout(codex_home, _errored_turn())
    assert StateIO.read_session_status(store.home, record)["state"] == "waiting"
    status = _prompt(store, record, monkeypatch, tmp_path, payload={
        "hook_event_name": "UserPromptSubmit", "session_id": THREAD,
        "turn_id": "01a16440-0000-7000-8000-000000000000",
    })
    assert status["turns"]["turn"] == 2
    assert StateIO.read_session_status(store.home, record)["state"] == "working"


# -- settle itself: only an open Codex turn, never a mutated input ------------


def _working(**turns) -> dict:
    return {
        "state": "working",
        "driver": "codex",
        "conversation_reference": THREAD,
        "turns": {"driver": "codex", "turn": 1, "turn_id": TURN,
                  "stopped_turn": None, "stop_blocked": False,
                  "last_event": "UserPromptSubmit", **turns},
    }


def test_settle_leaves_the_input_untouched():
    status = _working()
    before = json.dumps(status, sort_keys=True)
    settled = TurnSequence.settle(status, lambda _ref, _turn: True)
    assert settled["state"] == "waiting"
    assert json.dumps(status, sort_keys=True) == before


@pytest.mark.parametrize(
    "status",
    [
        None,
        {**_working(), "state": "waiting"},
        {**_working(), "driver": "claude",
         "turns": {**_working()["turns"], "driver": "claude"}},
        _working(last_event="Notification"),
        _working(turn=0),
        _working(turn_id=None),
        {**_working(), "conversation_reference": None},
    ],
)
def test_settle_ignores_anything_but_an_open_codex_turn(status):
    calls = []

    def probe(ref, turn):
        calls.append((ref, turn))
        return True

    assert TurnSequence.settle(status, probe) is status
    assert calls == []


@pytest.mark.parametrize("answer", [None, False])
def test_settle_unknown_or_open_changes_nothing(answer):
    status = _working()
    assert TurnSequence.settle(status, lambda _ref, _turn: answer) is status


def test_settle_survives_a_failing_probe():
    def boom(_ref, _turn):
        raise OSError("disk")

    status = _working()
    assert TurnSequence.settle(status, boom) is status
