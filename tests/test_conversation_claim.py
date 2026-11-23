"""A conversation claim is MADE, not inferred from a leftover command.

Phase 84 established that `driver.resume.reference` is not evidence of what a
session is on, and made ownership read `agents.resume_ids` alone. It left one
door open, which Codex found on the shipped commit: `AgentResumeState.__init__`
calls `seed_current_driver`, which writes the LAUNCH's reference into an EMPTY
slot. So merely CONSTRUCTING the state object can acquire a conversation, and
the field that is supposed to exclude the launch gets it back.

The sequence below is the one that matters, and every step of it is ordinary:

    A is on X  ->  A moves to Y  ->  Y is deleted, the guard forgets it
                -> B attaches X  ->  one status event for A

After that last step A claims X again — from nothing but its stale argv — and B,
which legitimately holds X, is refused its own conversation.

What makes this test different from the round-11 regression it replaces: that
one manufactured the empty map and called the refusal predicate directly, so it
never ran the writer that does the damage. Here the guard really forgets, the
event really arrives through the CLI entry point, and both records are reloaded
from disk before anything is asserted.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from hive_ide.agents import AgentResumeState
from hive_ide.conversation import ConversationGuard, ConversationState
from hive_ide.drivers import bundled_drivers
from hive_ide.errors import HeldConversationError
from hive_ide.frame import Frame
from hive_ide.store import StateStore


X = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"   # the conversation B ends up on
Y = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"   # A moves here, then it is deleted


@pytest.fixture
def claude_store(tmp_path, monkeypatch) -> Path:
    projects = tmp_path / "claude-config" / "projects"
    projects.mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    return projects


def _transcript(projects: Path, reference: str) -> Path:
    path = projects / "-some-project" / f"{reference}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def _session(store: StateStore, name: str, reference: str | None):
    return store.create_session(
        name=name,
        working_dir=store.workspace_key,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver=bundled_drivers()["claude"].resolve(
            name=name,
            working_dir=str(store.workspace_key),
            conversation_reference=reference,
        ),
    )


def test_reference_free_status_event_after_forget_does_not_reclaim_another_sessions_conversation(
    tmp_path, claude_store
):
    """The whole sequence, with the real guard and the real event entry point."""
    from hive_ide import cli

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    # X exists throughout. Y exists until A has moved onto it.
    _transcript(claude_store, X)
    y_transcript = _transcript(claude_store, Y)

    # A is launched on X, then a status event moves it to Y.
    a = _session(store, "A", X)
    AgentResumeState(a).remember("claude", Y)
    store.write("sessions", a["id"], a)

    # Y is deleted and the REAL guard forgets it. A's argv still names X.
    y_transcript.unlink()
    guard = ConversationGuard(store)
    assert guard.status("claude", Y, str(workspace)).state == ConversationState.GONE
    assert guard.status("claude", X, str(workspace)).state == ConversationState.EXISTS
    guard.check(a, working_dir=str(workspace), apply=True)
    assert "claude" not in (store.find_session(a["id"])["agents"]["resume_ids"]), (
        "the guard forgets a conversation that is gone"
    )

    # B legitimately attaches X.
    b = _session(store, "B", None)
    AgentResumeState(b).remember("claude", X)
    store.write("sessions", b["id"], b)

    # One status event for A, carrying NO conversation reference.
    cli.cmd_status_event(
        argparse.Namespace(
            session_id=a["id"],
            state_home=str(store.home),
            workspace_key=str(workspace),
            config=None,
            driver="claude",
            state="waiting",
            conversation_reference=None,
            subagents_running=None,
        )
    )

    a_after = store.find_session(a["id"])
    b_after = store.find_session(b["id"])

    # The first failing assertion on 2eca7d6.
    assert "claude" not in a_after["agents"]["resume_ids"], (
        "a reference-free event must not reclaim the conversation A forgot; "
        "constructing AgentResumeState re-seeded it from A's stale argv"
    )
    assert b_after["agents"]["resume_ids"]["claude"] == X, "B still holds X"

    # And the consequences, which are what the user would actually hit.
    frame = Frame(store, socket="sock")
    frame.refuse_owned_conversation(b_after)        # B can launch its own chat
    with pytest.raises(HeldConversationError):
        frame.refuse_owned_conversation(a_after)    # A may not resume B's


def test_creating_a_session_on_a_conversation_claims_it_on_the_first_write(
    tmp_path, claude_store
):
    """Adoption's claim is persisted by `create_session`, not by a side effect.

    `_create_adopted_session` resolves the launch against the conversation the
    user picked, and the record used to acquire it only because constructing
    `AgentResumeState` filled the empty slot. With that seeding gone the claim
    has to be made outright, or adoption silently stops owning what it adopted —
    and the next session to come along takes it.
    """
    store = StateStore(tmp_path / "state", str(tmp_path / "ws"))
    record = _session(store, "ADOPTED", X)

    assert record["agents"]["resume_ids"]["claude"] == X, "claimed in the record"
    on_disk = store.find_session(record["id"])
    assert on_disk["agents"]["resume_ids"]["claude"] == X, (
        "the claim must be on the FIRST write; a claim that only exists in the "
        "returned dict is lost the moment anything re-reads the session"
    )
    assert store.find_conversation_owner(driver_id="claude", reference=X)["id"] == record["id"]


def test_a_record_with_no_agents_block_still_claims_its_launched_conversation(
    tmp_path, claude_store
):
    """Legacy conversion is where an old record's existing claim becomes explicit.

    A record written before per-driver references has no `agents` block at all,
    so its launch is the only claim it has ever been able to make. Converting it
    must carry that across — otherwise upgrading the package silently releases
    every pre-existing session's conversation.

    This is the ONLY case where the launch still speaks. An `agents` block that
    exists with an empty `resume_ids` is the opposite: a session that recorded a
    reference and gave it up (see the forget test above).
    """
    store = StateStore(tmp_path / "state", str(tmp_path / "ws"))
    record = _session(store, "OLD", X)
    record.pop("agents")                      # as an older version wrote it
    store.write("sessions", record["id"], record)

    reloaded = store.find_session(record["id"])
    assert "agents" not in reloaded, "fixture must present a genuinely legacy record"
    AgentResumeState(reloaded)
    assert reloaded["agents"]["resume_ids"]["claude"] == X, (
        "a legacy record's launch is its claim; conversion must not drop it"
    )

    forgetful = store.find_session(record["id"])
    forgetful["agents"] = {"resume_ids": {}, "resume_history": {}}
    AgentResumeState(forgetful)
    assert forgetful["agents"]["resume_ids"] == {}, (
        "an EMPTY block is not legacy — it is a session that forgot, and "
        "conversion must not hand the conversation back to it"
    )


def test_an_empty_slot_does_not_push_the_launch_into_history(tmp_path, claude_store):
    """History records a DISAGREEMENT, and an empty slot disagrees with nothing.

    `seed_current_driver` still pushes a launched reference that contradicts
    `resume_ids` into history, so an inconsistent record cannot lose the last
    trace of a real conversation. That branch needs a current reference to
    contradict. If an empty slot reached it instead, every session that had
    forgotten a conversation would leave the launch sitting in history — where
    the fallback would pick it up and resume it, re-entering by the back door
    exactly what removing the seeding closed at the front.
    """
    store = StateStore(tmp_path / "state", str(tmp_path / "ws"))
    record = _session(store, "FORGOT", X)
    state = AgentResumeState(record)
    state.forget("claude")

    reloaded = dict(record)
    AgentResumeState(reloaded)
    assert reloaded["agents"]["resume_ids"] == {}, "still forgotten"
    assert not reloaded["agents"]["resume_history"].get("claude"), (
        "an empty slot must leave history empty; a launch parked there is a "
        "claim the fallback would act on"
    )
