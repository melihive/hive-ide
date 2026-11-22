"""Conversation reference reconciliation on launch, in repair, and on attach."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from hive_ide.agents import AgentResumeState
from hive_ide.cli import main
from hive_ide.conversation import ConversationGuard
from hive_ide.drivers import ConversationState, bundled_drivers
from hive_ide.frame import Frame
from hive_ide.hook import IdeHook
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
    monkeypatch.setattr(Frame, "ensure", lambda _self, _record, **_kw: False)
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


# -- a session keeps the conversation it actually had (Phase 84) --------------------
#
# All three defects come from one night: 2026-11-22, session DJ DEV. The agent
# pane's scope was OOM-killed after 4d 23h; a `claude` launch that was never
# typed into minted `44dcfed8`; its hook event replaced `b5050320` (live since
# 10-30, 69 MB of transcript) in the record; and the guard then found no
# transcript for `44dcfed8`, dropped it, and reset the driver to a bare
# `claude --name "DJ DEV"`. Only a hand-edit kept the conversation.


def _hook_env(monkeypatch, tmp_path, store, record) -> None:
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)


def _hook(store, *, state: str, payload: dict) -> None:
    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            state,
            "--driver",
            "claude",
            json.dumps(payload),
        ]
    ) == 0


def test_a_conversation_with_no_transcript_cannot_displace_one_that_has_it(
    tmp_path, stores, monkeypatch
):
    """The write that cost DJ DEV its conversation, refused.

    `44dcfed8` is a real id from a real launch — it just never held a turn, so
    no transcript was ever written for it. The status dot may follow it (the
    pane IS running that process); the resume reference may not.
    """
    store, record, _ = _session(tmp_path)
    _transcript(stores["claude"], REF)
    _hook_env(monkeypatch, tmp_path, store, record)

    _hook(store, state="waiting", payload={"session_id": OTHER, "hook_event_name": "SessionStart"})

    assert store.read("status", record["id"])["conversation_reference"] == OTHER
    held = store.find_session(record["id"])
    assert held["agents"]["resume_ids"]["claude"] == REF
    assert held["driver"]["resume"]["reference"] == REF
    assert held["driver"]["launch_argv"] == [
        "claude", "--resume", REF, "--name", "ALPHA",
    ]


def test_the_same_id_is_taken_once_it_has_a_transcript(tmp_path, stores, monkeypatch):
    """The other half of the rule: proof is all that was being asked for.

    Nothing about the id changes here — only that the conversation now exists on
    disk. A gate that refused this too would stop the record ever following a
    `/clear`.
    """
    store, record, _ = _session(tmp_path)
    _transcript(stores["claude"], REF)
    _transcript(stores["claude"], OTHER)
    _hook_env(monkeypatch, tmp_path, store, record)

    _hook(store, state="waiting", payload={"session_id": OTHER, "hook_event_name": "SessionStart"})

    updated = store.find_session(record["id"])
    assert updated["agents"]["resume_ids"]["claude"] == OTHER
    assert updated["driver"]["resume"]["reference"] == OTHER
    # The displaced reference is kept, which is what the fallback below needs.
    assert updated["agents"]["resume_history"]["claude"] == [REF]


def test_a_turn_proves_a_conversation_a_driver_cannot_show_a_transcript_for(
    tmp_path, stores, monkeypatch
):
    """`codex` keeps its rollouts where this package will not look for them, so
    every reference probes UNKNOWN. A real turn is then the only proof available,
    and refusing it would stop that driver recording anything at all."""
    store, record, _ = _session(tmp_path, driver_id="codex", reference=REF)
    _hook_env(monkeypatch, tmp_path, store, record)

    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            "working",
            "--driver",
            "codex",
            json.dumps({"session_id": OTHER, "hook_event_name": "UserPromptSubmit"}),
        ]
    ) == 0

    updated = store.find_session(record["id"])
    assert updated["agents"]["resume_ids"]["codex"] == OTHER
    assert updated["agents"]["resume_history"]["codex"] == [REF]


def test_an_unproven_reference_still_fills_an_empty_slot(tmp_path, stores, monkeypatch):
    """A session with nothing recorded loses nothing by trusting what arrives."""
    store, record, _ = _session(tmp_path, reference=None)
    _hook_env(monkeypatch, tmp_path, store, record)

    _hook(store, state="waiting", payload={"session_id": OTHER, "hook_event_name": "SessionStart"})

    updated = store.find_session(record["id"])
    assert updated["agents"]["resume_ids"]["claude"] == OTHER


def test_the_guard_falls_back_to_the_newest_reference_that_still_exists(
    tmp_path, stores
):
    """What repair should have done at 22:20:08 instead of resetting the driver."""
    store, record, workspace = _session(tmp_path, reference=OTHER)
    agents = AgentResumeState(record)
    agents.remember("claude", REF)
    agents.remember("claude", OTHER)
    store.write("sessions", record["id"], record)
    _transcript(stores["claude"], REF)  # OTHER has no transcript: it is gone

    result = ConversationGuard(store).check(
        record, working_dir=str(workspace), apply=True
    )

    assert result["changed"] is True
    assert any("fell back to" in action for action in result["actions"])
    updated = store.find_session(record["id"])
    assert updated["agents"]["resume_ids"]["claude"] == REF
    assert updated["driver"]["resume"]["reference"] == REF
    assert updated["driver"]["launch_argv"] == [
        "claude", "--resume", REF, "--name", "ALPHA",
    ]
    assert updated["agents"].get("resume_history", {}).get("claude", []) == []


def test_the_guard_starts_fresh_only_when_no_remembered_reference_survives(
    tmp_path, stores
):
    """A history of nothing but gone conversations still ends in a fresh one —
    and the dead references are pruned rather than retried forever."""
    store, record, workspace = _session(tmp_path, reference=OTHER)
    agents = AgentResumeState(record)
    agents.remember("claude", REF)
    agents.remember("claude", OTHER)
    store.write("sessions", record["id"], record)

    result = ConversationGuard(store).check(
        record, working_dir=str(workspace), apply=True
    )

    assert result["changed"] is True
    assert any("starts a fresh conversation" in action for action in result["actions"])
    updated = store.find_session(record["id"])
    assert "claude" not in updated["agents"]["resume_ids"]
    assert updated["driver"]["resume"]["reference"] is None
    assert "--resume" not in updated["driver"]["launch_argv"]
    assert updated["agents"].get("resume_history", {}).get("claude", []) == []


def test_a_remembered_reference_the_driver_cannot_speak_for_is_kept_not_resumed(
    tmp_path, stores, monkeypatch
):
    """UNKNOWN is not EXISTS: resuming it is what makes a pane exit on
    `--resume`. So it is skipped — and kept, because it may be readable again
    once whatever hid it (an unmounted home, a renamed config dir) is back."""
    store, record, workspace = _session(tmp_path, reference=OTHER)
    agents = AgentResumeState(record)
    agents.remember("claude", REF)
    agents.remember("claude", OTHER)
    store.write("sessions", record["id"], record)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "gone-config"))

    guard = ConversationGuard(store)
    assert guard.status("claude", REF, str(workspace)).state == ConversationState.UNKNOWN
    # The ACTIVE reference is unknown too, so nothing is dropped at all.
    result = guard.check(record, working_dir=str(workspace), apply=True)

    assert result["changed"] is False
    updated = store.find_session(record["id"])
    assert updated["agents"]["resume_ids"]["claude"] == OTHER
    assert updated["agents"]["resume_history"]["claude"] == [REF]


def test_the_fallback_refuses_a_remembered_reference_it_cannot_prove(tmp_path, stores):
    """The active conversation IS gone, and the one behind it cannot be probed.

    A reference the driver cannot speak for is not a survivor: resuming it is
    what leaves a pane exiting on `--resume`, which is the failure the guard was
    written for in the first place. So it is skipped — and kept, because unknown
    is not gone — and the launch starts fresh.

    `not-a-uuid` probes UNKNOWN rather than GONE for `claude`: the id never
    matches a transcript name, so absence proves nothing about it.
    """
    store, record, workspace = _session(tmp_path, reference=OTHER)
    agents = AgentResumeState(record)
    agents.remember("claude", "not-a-uuid")
    agents.remember("claude", OTHER)
    store.write("sessions", record["id"], record)

    guard = ConversationGuard(store)
    assert guard.status("claude", OTHER, str(workspace)).state == ConversationState.GONE
    assert (
        guard.status("claude", "not-a-uuid", str(workspace)).state
        == ConversationState.UNKNOWN
    )
    result = guard.check(record, working_dir=str(workspace), apply=True)

    assert any("starts a fresh conversation" in action for action in result["actions"])
    updated = store.find_session(record["id"])
    assert "claude" not in updated["agents"]["resume_ids"]
    assert updated["driver"]["resume"]["reference"] is None
    assert updated["agents"]["resume_history"]["claude"] == ["not-a-uuid"]
