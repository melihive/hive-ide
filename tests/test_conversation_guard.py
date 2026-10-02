"""Conversation reference reconciliation on launch, in repair, and on attach."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hive_ide.cli import main
from hive_ide.conversation import ConversationGuard
from hive_ide.drivers import ConversationState, bundled_drivers
from hive_ide.frame import Frame
from hive_ide.repair import SessionRepair
from hive_ide.store import StateStore

REF = "01e5e081-63f1-4c38-a6c2-644acd2f655a"
OTHER = "cd9c4573-1111-4222-8333-944455556666"


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": "test"}


@pytest.fixture
def stores(tmp_path, monkeypatch) -> dict[str, Path]:
    claude = tmp_path / "claude-config"
    codex = tmp_path / "codex-home"
    (claude / "projects").mkdir(parents=True)
    (codex / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return {"claude": claude / "projects", "codex": codex}


def _transcript(projects: Path, reference: str = REF) -> Path:
    path = projects / "-some-project" / f"{reference}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def _session(tmp_path: Path, driver_id: str = "claude", reference: str | None = REF):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="ALPHA",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()[driver_id].resolve(
            name="ALPHA",
            working_dir=str(workspace),
            conversation_reference=reference,
        ),
    )
    return store, record, workspace


# -- probe state beyond exists/gone -------------------------------------------------


def test_codex_archived_rollout_is_archived_with_unarchive_remedy(stores):
    archived = stores["codex"] / "archived_sessions" / f"rollout-2026-09-28T19-24-45-{REF}.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_text("{}\n", encoding="utf-8")

    status = bundled_drivers()["codex"].conversation_status(REF, "/anywhere")

    assert status.state == ConversationState.ARCHIVED
    assert f"codex unarchive {REF}" in status.detail


# -- the guard -------------------------------------------------------------------------


def test_guard_drops_a_gone_active_reference_and_launches_fresh(tmp_path, stores):
    store, record, workspace = _session(tmp_path)
    assert "--resume" in record["driver"]["launch_argv"]

    result = ConversationGuard(store).check(record, working_dir=str(workspace), apply=True)

    assert result["changed"] is True
    saved = store.find_session(record["id"])
    assert "--resume" not in saved["driver"]["launch_argv"]
    assert saved["driver"]["resume"]["reference"] is None
    assert "claude" not in saved["agents"]["resume_ids"]


def test_guard_drops_a_gone_parked_reference_but_keeps_the_active_driver(tmp_path, stores):
    store, record, workspace = _session(tmp_path)
    _transcript(stores["claude"])
    record["agents"]["resume_ids"]["codex"] = OTHER

    ConversationGuard(store).check(record, working_dir=str(workspace), apply=True)

    saved = store.find_session(record["id"])
    assert saved["agents"]["resume_ids"] == {"claude": REF}
    assert saved["driver"]["resume"]["reference"] == REF


def test_guard_leaves_an_unknown_reference_untouched(tmp_path):
    store, record, workspace = _session(tmp_path)
    before = json.dumps(store.find_session(record["id"]), sort_keys=True)
    # conftest points both stores at a missing dir: every probe is unknown.

    result = ConversationGuard(store).check(record, working_dir=str(workspace), apply=True)

    assert result == {"changed": False, "actions": [], "warnings": []}
    assert json.dumps(store.find_session(record["id"]), sort_keys=True) == before


def test_guard_preview_reports_but_does_not_write(tmp_path, stores):
    store, record, workspace = _session(tmp_path)
    before = json.dumps(store.find_session(record["id"]), sort_keys=True)

    result = ConversationGuard(store).check(record, working_dir=str(workspace), apply=False)

    assert result["changed"] is True
    assert json.dumps(store.find_session(record["id"]), sort_keys=True) == before


def test_guard_keeps_an_archived_reference_and_warns(tmp_path, stores):
    store, record, workspace = _session(tmp_path, driver_id="codex")
    archived = stores["codex"] / "archived_sessions" / f"rollout-x-{REF}.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_text("{}\n", encoding="utf-8")

    result = ConversationGuard(store).check(record, working_dir=str(workspace), apply=True)

    assert result["changed"] is False
    assert any("codex unarchive" in warning for warning in result["warnings"])
    assert store.find_session(record["id"])["driver"]["resume"]["reference"] == REF


# -- launch path: a dead reference starts fresh instead of exiting 1 ------------------


def _recording_tmux(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_tmux(_self, args, **_kwargs):
        calls.append(args)
        if args[:1] in (["new-window"], ["new-session"]):
            return subprocess.CompletedProcess(args, 0, "@9\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(Frame, "tmux", fake_tmux)
    monkeypatch.setattr(Frame, "_refresh_source_if_needed", lambda *_args: None)
    return calls


def _agent_split(calls: list[list[str]]) -> list[str]:
    splits = [call for call in calls if call[:1] == ["split-window"]]
    return splits[0]


def test_build_launches_fresh_when_the_reference_is_gone(tmp_path, stores, monkeypatch):
    store, record, _workspace = _session(tmp_path)
    calls = _recording_tmux(monkeypatch)

    Frame(store, socket="test").build(record)

    command = _agent_split(calls)[-1]
    assert "--resume" not in command
    assert REF not in command


def test_build_keeps_an_unknown_reference(tmp_path, monkeypatch):
    store, record, workspace = _session(tmp_path)
    calls = _recording_tmux(monkeypatch)

    Frame(store, socket="test").build(record)

    split = _agent_split(calls)
    assert f"--resume {REF}" in split[-1]
    assert split[split.index("-c") + 1] == str(workspace)


def test_build_still_opens_when_the_guard_fails(tmp_path, monkeypatch):
    store, record, _workspace = _session(tmp_path)
    calls = _recording_tmux(monkeypatch)

    def broken(*_args, **_kwargs):
        raise OSError("state dir is read-only")

    monkeypatch.setattr(ConversationGuard, "check", broken)

    Frame(store, socket="test").build(record)

    assert f"--resume {REF}" in _agent_split(calls)[-1]


def test_respawn_agent_launches_fresh_when_the_reference_is_gone(
    tmp_path, stores, monkeypatch
):
    store, record, _workspace = _session(tmp_path)
    calls = _recording_tmux(monkeypatch)

    Frame(store, socket="test").respawn_agent(record, "%2")

    respawn = next(call for call in calls if call[:1] == ["respawn-pane"])
    assert "--resume" not in respawn[-1]


# -- repair ----------------------------------------------------------------------------


def test_repair_preview_reports_a_gone_reference_and_apply_drops_it(
    tmp_path, stores, monkeypatch
):
    store, record, _workspace = _session(tmp_path)
    monkeypatch.setattr(Frame, "windows", lambda _self: {})
    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "apply_columns", lambda _self, _record: None)
    repair = SessionRepair(store, Frame(store, socket="test"))

    preview = repair.repair(dict(store.find_session(record["id"])), apply=False)
    assert any("no longer exists" in action for action in preview["actions"])
    assert store.find_session(record["id"])["driver"]["resume"]["reference"] == REF

    repair.repair(store.find_session(record["id"]), apply=True)
    assert store.find_session(record["id"])["driver"]["resume"]["reference"] is None


# -- attach-conversation ---------------------------------------------------------------


def _attach(store: StateStore, session_id: str, driver: str, reference: str) -> int:
    return main(
        [
            "--state-home",
            str(store.home),
            "--workspace-key",
            store.workspace_key,
            "attach-conversation",
            f"--session-id={session_id}",
            f"--driver={driver}",
            f"--reference={reference}",
        ]
    )


def test_attach_refuses_a_gone_claude_conversation(tmp_path, stores, capsys):
    store, record, _workspace = _session(tmp_path, reference=None)

    assert _attach(store, record["id"], "claude", REF) != 0
    assert "cannot find conversation" in capsys.readouterr().err
    _transcript(stores["claude"])
    assert _attach(store, record["id"], "claude", REF) == 0


def test_attach_refuses_an_archived_codex_conversation(tmp_path, stores, capsys):
    store, record, _workspace = _session(tmp_path, driver_id="codex", reference=None)
    archived = stores["codex"] / "archived_sessions" / f"rollout-x-{REF}.jsonl"
    archived.parent.mkdir(parents=True)
    archived.write_text("{}\n", encoding="utf-8")

    assert _attach(store, record["id"], "codex", REF) != 0
    assert "codex unarchive" in capsys.readouterr().err


def test_attach_refuses_a_conversation_owned_in_another_workspace(tmp_path, capsys):
    store, record, _workspace = _session(tmp_path, driver_id="codex", reference=None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    foreign = StateStore(tmp_path / "state", elsewhere)
    owner = foreign.create_session(
        name="FLEET",
        working_dir=elsewhere,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="FLEET", working_dir=str(elsewhere), conversation_reference=REF
        ),
    )

    assert store.find_conversation_owner(driver_id="codex", reference=REF)["id"] == owner["id"]
    assert _attach(store, record["id"], "codex", REF) != 0
    assert "FLEET" in capsys.readouterr().err


def test_owner_search_skips_an_unreadable_foreign_record(tmp_path):
    store, _record, _workspace = _session(tmp_path, driver_id="codex", reference=None)
    broken = store.home / "workspaces" / "zz-broken" / "sessions" / "bad.json"
    broken.parent.mkdir(parents=True)
    broken.write_text("{not json", encoding="utf-8")

    assert store.find_conversation_owner(driver_id="codex", reference=REF) is None


def test_adoption_sees_a_reference_owned_in_another_workspace(tmp_path):
    """Adoption asks "is this conversation already wrapped?" — the same question
    `find_conversation_owner` answers, so it must cover the same ground. A
    local-workspace-only scan let adoption mint a second wrapper for a
    conversation another workspace already owned."""
    from hive_ide.adoption import ConversationAdopter

    store, _record, _workspace = _session(tmp_path, driver_id="codex", reference=None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    foreign = StateStore(tmp_path / "state", elsewhere)
    foreign.create_session(
        name="FLEET",
        working_dir=elsewhere,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="FLEET", working_dir=str(elsewhere), conversation_reference=REF
        ),
    )

    adopter = ConversationAdopter(store, {})
    assert REF in adopter.existing_references(driver_id="codex")


def test_adoption_sees_a_parked_reference(tmp_path):
    """A session that switched driver still owns the conversation it switched away
    from; that reference lives in `agents.resume_ids`, not in the active driver."""
    from hive_ide.adoption import ConversationAdopter

    store, record, workspace = _session(tmp_path, driver_id="claude", reference=OTHER)
    record["agents"] = {"active": "claude", "resume_ids": {"claude": OTHER, "codex": REF}}
    store.write("sessions", record["id"], record)

    adopter = ConversationAdopter(store, {})
    assert REF in adopter.existing_references(driver_id="codex")
    assert OTHER in adopter.existing_references(driver_id="claude")
