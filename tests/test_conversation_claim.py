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

    # B legitimately attaches X. Its LAUNCH has to name X too, not just its
    # slot: `refuse_owned_conversation` is bounded by the rendered argv, so a B
    # whose command carries no reference makes the "B can still launch"
    # assertion below vacuous — it would pass on any code at all.
    b = _session(store, "B", X)
    assert X in " ".join(b["driver"]["launch_argv"]), "B's command really resumes X"
    assert b["agents"]["resume_ids"]["claude"] == X

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

    # And the consequences, which are what the user would actually hit. Both
    # records now carry X in their rendered command, so the ownership guard
    # really does look up an owner for each of them.
    frame = Frame(store, socket="sock")
    frame.refuse_owned_conversation(b_after)        # B can launch its own chat
    with pytest.raises(HeldConversationError) as refusal:
        frame.refuse_owned_conversation(a_after)    # A may not resume B's
    assert "B" in str(refusal.value), "the refusal names the session that owns X"


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


def test_the_in_process_hook_is_the_same_entry_point_for_the_claim_rule(
    tmp_path, claude_store, monkeypatch
):
    """The CLI is not the only way an event arrives.

    `cmd_status_event` is what an external hook config calls; a pane that the
    package wired itself goes through `IdeHook.main` instead, and that path
    builds its own `AgentResumeState`. Both are writers, so both can acquire a
    conversation the moment construction does it for them — a rule enforced at
    one of two doors is not enforced.
    """
    import json

    from hive_ide.hook import IdeHook

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    _transcript(claude_store, X)

    a = _session(store, "A", X)
    AgentResumeState(a).forget("claude")      # A gave up its claim
    store.write("sessions", a["id"], a)

    b = _session(store, "B", None)
    AgentResumeState(b).remember("claude", X)
    store.write("sessions", b["id"], b)

    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", a["id"])
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            "waiting",
            "--driver",
            "claude",
            json.dumps({"session_id": "", "hook_event_name": "Stop"}),
        ]
    ) == 0

    assert "claude" not in store.find_session(a["id"])["agents"]["resume_ids"], (
        "the hook entry point must not re-seed A's claim from its stale argv"
    )
    assert store.find_session(b["id"])["agents"]["resume_ids"]["claude"] == X


def test_repair_leaves_nothing_for_a_later_read_to_infer_a_claim_from(
    tmp_path, claude_store
):
    """Repair builds state several times per run; a reload must stay harmless.

    The sweep hoists ONE state object precisely because rebuilding one used to
    re-seed what the previous iteration had removed. That hoist only ever
    covered the sweep's own loop — `ConversationGuard.check` constructs state
    again later in the SAME run, and the next run constructs it again from disk.

    What makes those later constructions safe is not the hoist and not the
    removed seeding: it is that clearing a duplicate clears the LAUNCH too, and
    re-renders the argv without it. Nothing is left on the record for a later
    read to infer a claim from.

    That is worth pinning because it is the load-bearing half. The first draft
    of this test asserted the slot stayed empty across a reload and passed with
    the seeding restored — it could not go red, because by then the launch was
    already gone. The assertion that moves is the one below.
    """
    from hive_ide.repair import SessionRepair

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    _transcript(claude_store, X)

    # B owns X. A's record names it too — the duplicate the sweep exists to clear.
    b = _session(store, "B", X)
    a = _session(store, "A", X)

    repair = SessionRepair(store, Frame(store, socket="sock"))
    a_record = store.find_session(a["id"])
    repair._remove_duplicate_conversation_refs(a_record, [], [], apply=True)
    store.write("sessions", a_record["id"], a_record)

    assert "claude" not in a_record["agents"]["resume_ids"], "the duplicate is cleared"
    assert AgentResumeState.reference_from_driver(a_record["driver"]) is None, (
        "the LAUNCH must be cleared with the slot; a conversation left in the "
        "stored command is a claim waiting for the next thing that reads it"
    )
    assert X not in " ".join(a_record["driver"].get("launch_argv") or []), (
        "and the argv is re-rendered without it"
    )

    # So the stages that follow — a reload, the sweep again, and the guard's own
    # construction inside the same run — have nothing to hand back.
    a_again = store.find_session(a["id"])
    AgentResumeState(a_again)
    repair._remove_duplicate_conversation_refs(a_again, [], [], apply=True)
    ConversationGuard(store).check(a_again, working_dir=str(workspace), apply=True)

    assert "claude" not in store.find_session(a["id"])["agents"]["resume_ids"]
    assert store.find_session(b["id"])["agents"]["resume_ids"]["claude"] == X, (
        "B keeps the conversation it owns throughout"
    )


def test_the_first_unproven_event_still_fills_a_slot_that_is_now_genuinely_empty(
    tmp_path, claude_store
):
    """Removing the seeding makes an old asymmetry reachable for the first time.

    `remember(proven=False)` has always been allowed to FILL an empty slot and
    forbidden to REPLACE a different one — the 2026-11-22 DJ DEV rule. Until
    now the fill arm was nearly unreachable, because constructing the state
    object had already put the launch there. With seeding gone the slot really
    can be empty when an unproven event arrives, so the arm needs its own test
    rather than inheriting one.

    Filling it is still right: a session holding no reference loses nothing by
    trusting the only one on offer. What it must not do is reach that state by
    reading its own argv, which is what the tests above pin.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    record = _session(store, "A", X)
    state = AgentResumeState(record)
    state.forget("claude")
    assert record["agents"]["resume_ids"] == {}

    # No transcript was written for Y, so this reference is unproven.
    assert state.remember("claude", Y, proven=False) is True
    assert record["agents"]["resume_ids"]["claude"] == Y

    # And it still may not displace a reference that is there.
    assert state.remember("claude", X, proven=False) is False
    assert record["agents"]["resume_ids"]["claude"] == Y, (
        "an unproven reference fills an empty slot but never replaces one"
    )


# -- the readers that answer "what is this session on right now" ---------------
#
# `session_reference` falls back to the launch when the slot is empty. That was
# harmless while the slot was never empty — construction filled it. Now that a
# forgotten conversation stays forgotten, the fallback is reachable, and it
# answers a question nobody asked it: the argv's conversation, reported as the
# session's own. Codex finding 2 on the shipped claim fix.


def test_a_session_that_forgot_its_conversation_reports_none_not_its_argv(
    tmp_path, claude_store
):
    """The accessor needs the same absent-vs-empty rule the constructor uses.

    An absent `agents` block is a record from before per-driver references, and
    its launch is the only claim it has. An EMPTY `resume_ids` is a session that
    had a reference and gave it up — asking its argv hands back exactly what it
    released.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    record = _session(store, "A", X)
    AgentResumeState(record).forget("claude")
    assert AgentResumeState.reference_from_driver(record["driver"]) == X, (
        "the stored command still names X — that is the whole hazard"
    )
    assert AgentResumeState.session_reference(record) is None, (
        "a session that forgot its conversation is ON nothing; the launch "
        "speaks for the next command, never for the session"
    )

    legacy = store.find_session(record["id"])
    legacy.pop("agents")
    assert AgentResumeState.session_reference(legacy) == X, (
        "a record with no agents block at all still falls back to its launch"
    )


def test_a_sleeping_session_does_not_advertise_a_conversation_it_released(
    tmp_path, claude_store
):
    """The sleeping status row is what the sidebar renders.

    It is a report ABOUT the session, so a released conversation showing up here
    tells the user the sleeping agent is sitting on a chat that another session
    may now own.
    """
    from hive_ide import cli

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    record = _session(store, "A", X)
    AgentResumeState(record).forget("claude")
    store.write("sessions", record["id"], record)

    cli._write_sleeping_status(store, record, "2026-11-23T12:00:00+00:00")
    row = store.read("status", record["id"])
    assert row["conversation_reference"] is None, (
        "a sleeping session reports the conversation it is on, and it is on none"
    )


def test_the_sidebar_projection_does_not_put_a_released_conversation_back(
    tmp_path, claude_store
):
    """`StateIO._legacy` projects a thin record for the sidebar and plugins.

    It re-`remember`s whatever the accessor returns, so the launch fallback was
    being copied straight into the projection's own `resume_ids` — the sidebar
    naming the conversation the session had just left.
    """
    from hive_ide.state_compat import StateIO

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    record = _session(store, "A", X)
    AgentResumeState(record).forget("claude")
    store.write("sessions", record["id"], record)

    projected = StateIO._legacy(store.find_session(record["id"]))
    assert X not in str(projected.get("agents")), (
        "the projection must not re-acquire the released conversation"
    )
    assert record["agents"]["resume_ids"] == {}, "and the real record is untouched"


def test_the_stranded_holder_release_declines_a_conversation_no_longer_claimed(
    tmp_path, claude_store
):
    """Repair would otherwise hunt a process on a conversation the session released.

    The release path exists to stop a driver stranded on THIS session's
    conversation. Once the session claims none, there is nothing of its own to
    be stranded on, and a process found on the old argv's conversation belongs
    to whoever owns it now.
    """
    from hive_ide.repair import SessionRepair

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    record = _session(store, "A", X)
    AgentResumeState(record).forget("claude")
    store.write("sessions", record["id"], record)

    repair = SessionRepair(store, Frame(store, socket="sock"))

    def _first_probe(_record):
        # Fail HERE, not after the call returns. This runs with apply=True, so
        # letting the regression proceed past this point lets repair scan real
        # host processes and SIGTERM a matching driver on a detached server —
        # a test that terminates one of the machine's own processes to prove a
        # bug. Codex round 2, finding 2.
        pytest.fail(
            "the release must decline on the empty claim, before it probes for "
            "a holder of a conversation this session no longer claims"
        )

    repair._has_live_shell_agent = _first_probe
    repair._release_stranded_holder(
        store.find_session(record["id"]), [], [], apply=True
    )
