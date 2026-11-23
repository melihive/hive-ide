"""Two fields, two questions: what the session is ON, and what its argv resumes.

Before the event/launch split these were the same string. A status event wrote
`agents.resume_ids` AND re-rendered the launch, so any reader could ask either
one and get the same answer. The split stopped the event re-rendering a launch
it cannot reproduce, and `ConversationGuard` now reconciles the launch at launch
time instead — which means that between an event and the next launch the record
names two different conversations:

    agents.resume_ids[<driver>]   the conversation the session is ON
    driver.resume.reference        the conversation the stored COMMAND resumes

Every reader of either field had to be sorted into one question or the other,
and this file is the audit's residue: one test per reader that was asking the
wrong one, each red without its fix.

The shape of the mistake is always the same, and always silent. Reading the argv
where the session was meant gives a stale but perfectly well-formed conversation
id — so nothing errors, nothing warns, and the wrong conversation is reported,
opened or left unguarded. `/clear` is the cheap way to reach it: the agent mints
a new conversation, the hook records it, and the argv still names the old one
until something launches.

The readers that legitimately ask about the argv — `_refuse_if_held`,
`_attach_argv`, the monitor's command-line matcher — are NOT bugs and are not
here. They run after `prepare_agent_launch` has reconciled the record (or match
against a real command line, which carries the launched id by construction).
Their docstrings say so, because the next person to read them will have this
file in mind.
"""

from __future__ import annotations

import pytest

from hive_ide.agents import AgentResumeState
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.repair import SessionRepair
from hive_ide.state_compat import StateIO
from hive_ide.store import StateStore


#: The conversation the session LEFT — still named by the stored launch argv.
LEFT = "11111111-1111-4111-8111-111111111111"
#: The conversation a status event moved the session to.
JOINED = "22222222-2222-4222-8222-222222222222"
#: A third conversation, for a second driver on the same record.
OTHER = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def _moved(store, *, name="SESSION", left=LEFT, joined=JOINED):
    """A session whose event moved it to `joined` while its argv still says `left`.

    Built through the real driver so the launch argv is one this package
    defines, and written back, because the readers under test re-read the
    record rather than taking it from a caller.
    """
    workspace = store.workspace_key
    record = store.create_session(
        name=name,
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=bundled_drivers()["claude"].resolve(
            name=name, working_dir=str(workspace), conversation_reference=left
        ),
    )
    agents = AgentResumeState(record)
    agents.remember("claude", joined)
    store.write("sessions", record["id"], record)
    return record


# --- the accessor itself ---------------------------------------------------


def test_session_reference_prefers_what_an_event_recorded(store):
    record = _moved(store)
    assert AgentResumeState.session_reference(record) == JOINED
    assert AgentResumeState.reference_from_driver(record["driver"]) == LEFT, (
        "the launch is deliberately left alone until something launches"
    )


def test_session_reference_falls_back_to_the_launch_before_any_event(store):
    """A session that has never had an event has only its launch to go on."""
    workspace = store.workspace_key
    record = store.create_session(
        name="FRESH",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=bundled_drivers()["claude"].resolve(
            name="FRESH", working_dir=str(workspace), conversation_reference=LEFT
        ),
    )
    record["agents"] = {}
    assert AgentResumeState.session_reference(record) == LEFT


def test_session_reference_does_not_lend_one_drivers_launch_to_another(store):
    """The fallback is the launch's own driver's, or nothing.

    Asking about `codex` on a record launched as `claude` must not hand back
    the claude conversation — that is how a driver would be resumed into a
    transcript it cannot read.
    """
    record = _moved(store)
    assert AgentResumeState.session_reference(record, "codex") is None


def test_session_reference_does_not_write_to_the_record(store):
    """A read-only caller (`verify`) must not seed state as a side effect."""
    record = _moved(store)
    record["agents"].pop("resume_history", None)
    before = {k: dict(v) if isinstance(v, dict) else v for k, v in record["agents"].items()}
    AgentResumeState.session_reference(record)
    assert record["agents"] == before


# --- verify's holder check -------------------------------------------------


def test_verify_reports_a_holder_on_the_conversation_the_session_moved_to(
    store, monkeypatch
):
    """The orphan sits on the LIVE conversation, which the argv no longer names.

    Red before the fix: `_conversation_holders` read the argv, found no holder
    on a conversation nothing is running, and reported the session healthy —
    the exact silence the finding exists to break.
    """
    from hive_ide.cli import _conversation_holders

    record = _moved(store)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent"), (4242, 1, f"claude --resume {JOINED}")]

    found = _conversation_holders(frame, [record], rows)

    assert [f["pid"] for f in found] == [4242]


def test_verify_also_reports_a_holder_on_the_launch_the_next_open_would_run(
    store, monkeypatch
):
    """The argv is not redundant once the guard may decline to re-render it.

    A configured wrapper is a launch this package cannot reproduce, so the guard
    leaves the command alone and the next `open` really does resume the OLD
    conversation. An orphan there raises `HeldConversationError` at launch while
    verify, asking only about `resume_ids`, said the frame was healthy. Both
    conversations refuse a launch, so both are reported.

    This reverses an assertion written one round earlier, which pinned "a
    process on the conversation the session left is not a finding". That was the
    comfortable answer, not the true one: what makes a holder a finding is that
    it blocks the next launch, and this one does.
    """
    from hive_ide.cli import _conversation_holders

    record = _moved(store)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent"), (4242, 1, f"claude --resume {LEFT}")]

    found = _conversation_holders(frame, [record], rows)

    assert [(f["pid"], f["reference"]) for f in found] == [(4242, LEFT)]


def test_verify_reports_one_finding_when_both_fields_name_one_conversation(
    store, monkeypatch
):
    """The ordinary session: two fields, one conversation, one finding.

    Without this the fix above is satisfied by reporting every holder twice,
    which would double every finding on every healthy session.
    """
    from hive_ide.cli import _conversation_holders

    record = _moved(store, left=JOINED, joined=JOINED)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent"), (4242, 1, f"claude --resume {JOINED}")]

    assert len(_conversation_holders(frame, [record], rows)) == 1


# --- who owns a conversation ------------------------------------------------


def test_ownership_is_what_a_session_is_on_not_what_its_stale_launch_names(store):
    """A stale argv must not outrank a live remembered reference.

    Session OWNER is on `LEFT`. Session MOVER has gone to `JOINED` but its argv
    still says `LEFT`. Before the fix, repairing OWNER found MOVER as the
    "owner" of its own conversation, deleted OWNER's reference and blanked its
    launch — the repair erasing the session that actually held the thing.
    """
    owner = _moved(store, name="OWNER", left=LEFT, joined=LEFT)
    _moved(store, name="MOVER", left=LEFT, joined=JOINED)

    assert (
        store.find_conversation_owner(
            driver_id="claude", reference=LEFT, exclude_session_id=owner["id"]
        )
        is None
    )


def test_ownership_still_falls_back_to_the_launch_of_a_legacy_record(store):
    """The negative arm: a record with no event has only its launch to speak for it."""
    workspace = store.workspace_key
    legacy = store.create_session(
        name="LEGACY",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=bundled_drivers()["claude"].resolve(
            name="LEGACY", working_dir=str(workspace), conversation_reference=LEFT
        ),
    )
    legacy.pop("agents", None)
    store.write("sessions", legacy["id"], legacy)

    found = store.find_conversation_owner(
        driver_id="claude", reference=LEFT, exclude_session_id="someone-else"
    )

    assert found is not None and found["id"] == legacy["id"]


# --- the sleeping status row -----------------------------------------------


def test_the_sleeping_status_row_names_the_conversation_the_session_is_on(store):
    """The row is a report about the session, so it answers the session's question."""
    from hive_ide.cli import _write_sleeping_status

    record = _moved(store)
    _write_sleeping_status(store, record, "2026-11-23T00:00:00Z")

    assert store.read("status", record["id"])["conversation_reference"] == JOINED


# --- `current chat` ---------------------------------------------------------


def _chat_args(store, record):
    import argparse

    return argparse.Namespace(
        session_id=record["id"],
        state_home=str(store.home),
        workspace_key=str(store.workspace_key),
        config=None,
        tmux_socket=None,
    )


def _stub_registry(monkeypatch, rendered):
    """A driver that records what `chat` asked it to render, and is 'installed'.

    Detection is stubbed because it runs before anything under test and CI has
    no driver on `PATH` — the real one passing here and failing there is how a
    test like this gets written green and lands red.
    """
    from types import SimpleNamespace

    from hive_ide import cli

    class _StubDriver:
        def detect(self):
            return SimpleNamespace(available=True, detail="")

        def resolve(self, **kwargs):
            rendered.append(kwargs.get("conversation_reference"))
            return {"id": "claude", "launch_argv": ["claude"], "resume": {}}

    monkeypatch.setattr(
        cli,
        "configured_registry",
        lambda config: SimpleNamespace(get=lambda driver_id: _StubDriver()),
    )
    monkeypatch.setattr(Frame, "current_chat", lambda self, rec: {"ok": True})


def test_current_chat_renders_the_launchs_own_reference_not_the_sessions(
    store, monkeypatch
):
    """`chat` canonicalizes a launch; it does not move one to a new conversation.

    Rendering the session's reference here injected one nothing had validated.
    A Codex session that resumes BY NAME carries no reference and must skip this
    entirely; reading `resume_ids` gave it a uuid, which overwrote the name, and
    when the guard then rejected that uuid a bare `codex` with no session is
    what started. Moving a launch to the session's conversation is the guard's
    decision, taken where it can first prove the launch is reproducible.
    """
    from hive_ide import cli

    record = _moved(store)
    rendered: list[str | None] = []
    _stub_registry(monkeypatch, rendered)

    assert cli.cmd_current_chat(_chat_args(store, record)) == {"ok": True}

    assert rendered == [LEFT], "the launch's own reference, not JOINED"


def test_current_chat_does_not_overwrite_an_event_that_lands_before_the_lock(
    store, monkeypatch
):
    """The record is read before the mutation lock; the write happens inside it.

    Writing the read-time snapshot back put the session on whatever it named
    then, undoing a hook event that landed in between. Here the event arrives
    while `chat` waits for the lock, and `chat` must leave it alone.
    """
    from hive_ide import cli

    record = _moved(store)
    rendered: list[str | None] = []
    _stub_registry(monkeypatch, rendered)

    # Patched on the CLASS, not this fixture's instance: `cmd_current_chat`
    # builds its own store from the arguments, so an instance patch here would
    # never fire and the test would pass without observing anything.
    real_lock = StateStore.mutation_lock
    fired: list[bool] = []

    def _event_then_lock(self, *args, **kwargs):
        # Only the BLOCKING take. The source refresh inside `_current_record`
        # grabs the same lock with `blocking=False` first, and firing there
        # would land the event before the read this test is about.
        if not fired and kwargs.get("blocking", True):
            fired.append(True)
            # A hook event moves the launch while `chat` waits for the lock.
            moved = self.read("sessions", record["id"])
            moved["driver"]["resume"] = {"reference": OTHER}
            self.write("sessions", record["id"], moved)
        return real_lock(self, *args, **kwargs)

    monkeypatch.setattr(StateStore, "mutation_lock", _event_then_lock)

    cli.cmd_current_chat(_chat_args(store, record))

    written = store.read("sessions", record["id"])
    assert AgentResumeState.reference_from_driver(written["driver"]) == OTHER, (
        "the newer event stands; chat does not restore what it read"
    )


# --- the legacy projection the sidebar renders -----------------------------


def test_the_legacy_projection_keeps_what_an_event_recorded(store):
    """Red before the fix: `_legacy` remembered the LAUNCH over the event.

    `remember` defaults to proven, so the launched reference displaced the one
    the hook had recorded, and the sidebar named the conversation the session
    had just left.
    """
    record = _moved(store)

    projected = StateIO._legacy(record)

    assert projected["agents"]["resume_ids"]["claude"] == JOINED


def test_the_legacy_projection_does_not_write_through_to_the_record(store):
    """`dict(record)` is shallow: a pure read must not mutate the caller's record.

    Seeding is the visible half — `AgentResumeState.__init__` fills an empty
    `resume_ids` from the launch and adds a `resume_history` key — and it landed
    in the caller's own dict, so merely rendering the sidebar edited the session
    state the next writer would save.
    """
    record = _moved(store)
    record["agents"] = {}

    projected = StateIO._legacy(record)

    assert projected["agents"]["resume_ids"]["claude"] == LEFT, (
        "the projection still seeds its own copy from the launch"
    )
    assert record["agents"] == {}, "and leaves the record exactly as it found it"


# --- repair's duplicate-reference sweep ------------------------------------


def test_repair_clears_a_launch_ref_owned_elsewhere_that_resume_ids_does_not_name(
    store, tmp_path
):
    """The sweep iterated `resume_ids`, so a stolen LAUNCH ref survived it.

    Our session moved to `JOINED`; its argv still names `LEFT`; another session
    owns `LEFT`. Nothing in the loop ever visits `LEFT`, so before the fix the
    next `open` relaunched straight onto somebody else's conversation.
    """
    owner = _moved(store, name="OWNER", left=LEFT, joined=LEFT)
    assert AgentResumeState.session_reference(owner) == LEFT
    record = _moved(store, name="OURS")

    frame = Frame(store, socket="sock")
    warnings: list[str] = []
    SessionRepair(store, frame)._remove_duplicate_conversation_refs(
        record, [], warnings, apply=False
    )

    assert AgentResumeState.reference_from_driver(record["driver"]) is None
    assert any("belonged to" in w for w in warnings)
    assert AgentResumeState.session_reference(record) == JOINED, (
        "our own live conversation is not what was contested"
    )


def test_repair_does_not_resurrect_a_ref_it_removed_on_a_later_drivers_turn(store):
    """A second driver's iteration must not re-seed the first driver's loss.

    `AgentResumeState.__init__` seeds an empty `resume_ids` slot from the
    launch, so building a fresh one per iteration put the claude reference back
    the moment the codex iteration constructed its own — and the sweep reported
    a removal it had silently undone.
    """
    _moved(store, name="OWNS_JOINED", left=LEFT, joined=JOINED)
    # A conversation id belongs to ONE driver, so the rival owner has to hold
    # this one under `codex` for the sweep to see a collision at all.
    rival = _moved(store, name="OWNS_OTHER", left=LEFT, joined=LEFT)
    rival["agents"]["resume_ids"]["codex"] = OTHER
    store.write("sessions", rival["id"], rival)
    # Our claude slot is contested but our LAUNCH names something else, so the
    # sweep removes the slot without clearing the launch — and the launch is
    # then exactly what a fresh state object would seed back into it.
    record = _moved(store, name="OURS")
    record["agents"]["resume_ids"]["codex"] = OTHER

    frame = Frame(store, socket="sock")
    SessionRepair(store, frame)._remove_duplicate_conversation_refs(
        record, [], [], apply=False
    )

    assert record["agents"]["resume_ids"] == {}


def test_repair_checks_the_launch_of_a_record_with_no_agents_block(store):
    """A legacy record returned early, so its launch was never checked at all.

    That is the one record shape where the launch is the ONLY reference there
    is — so the early return skipped the check in exactly the case it mattered.
    """
    _moved(store, name="RIVAL", left=LEFT, joined=LEFT)
    workspace = store.workspace_key
    record = store.create_session(
        name="LEGACY",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=bundled_drivers()["claude"].resolve(
            name="LEGACY", working_dir=str(workspace), conversation_reference=LEFT
        ),
    )
    record.pop("agents", None)

    frame = Frame(store, socket="sock")
    SessionRepair(store, frame)._remove_duplicate_conversation_refs(
        record, [], [], apply=False
    )

    assert AgentResumeState.reference_from_driver(record["driver"]) is None


def test_repair_checks_the_launch_when_another_driver_parks_the_same_string(store):
    """"Does ANY slot remember this" let an unrelated driver vouch for the launch.

    Our claude launch names `LEFT` and a rival owns claude `LEFT`; our parked
    CODEX slot happens to name `LEFT` too. A conversation id belongs to one
    driver, so the codex slot says nothing about the claude launch — but the
    loose check read it as "already accounted for" and skipped the lookup.
    """
    _moved(store, name="RIVAL", left=LEFT, joined=LEFT)
    record = _moved(store, name="OURS")
    record["agents"]["resume_ids"]["codex"] = LEFT

    frame = Frame(store, socket="sock")
    SessionRepair(store, frame)._remove_duplicate_conversation_refs(
        record, [], [], apply=False
    )

    assert AgentResumeState.reference_from_driver(record["driver"]) is None


def test_repair_leaves_a_launch_ref_nobody_else_owns(store):
    """The negative arm: divergence alone is not grounds to strip the launch."""
    record = _moved(store)

    frame = Frame(store, socket="sock")
    SessionRepair(store, frame)._remove_duplicate_conversation_refs(
        record, [], [], apply=False
    )

    assert AgentResumeState.reference_from_driver(record["driver"]) == LEFT


def test_the_legacy_projection_still_answers_for_a_driver_with_no_id(store):
    """A driver block with a reference but no `id` still projects that reference.

    `_legacy` substitutes the synthetic id `"term"`, and naming it to the
    accessor would scope the launch fallback to a driver the record does not
    claim — so the projection fell silent for a record that used to expose its
    reference. Asked without an id, the fallback behaves as documented.
    """
    workspace = store.workspace_key
    record = store.create_session(
        name="ODD",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver={"resume": {"reference": LEFT}},
    )
    record["agents"] = {}

    projected = StateIO._legacy(record)

    assert projected["agents"]["resume_ids"]["term"] == LEFT


# --- repair's stranded-holder release --------------------------------------


def test_the_stranded_release_hunts_the_conversation_the_session_is_on(
    store, monkeypatch
):
    """The holder is a RUNNING driver, so it sits on the live conversation.

    Red before the fix: the release asked about the argv's conversation, found
    nothing stranded there, and left the real holder running — which is the
    stalemate the release exists to break, reintroduced by a stale field.
    """
    from hive_ide import repair as repair_module

    record = _moved(store)
    asked: list[str] = []
    monkeypatch.setattr(
        repair_module.ConversationHolders, "snapshot", staticmethod(lambda: [(1, 1, "x")])
    )

    def _stranded(reference, **kwargs):
        asked.append(reference)
        return []

    monkeypatch.setattr(
        repair_module.ConversationHolders, "stranded", staticmethod(_stranded)
    )
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    monkeypatch.setattr(Frame, "agent_pane_pid", lambda self, rec: None)

    SessionRepair(store, frame)._release_stranded_holder(
        record, [], [], apply=False
    )

    assert asked == [JOINED]


# --- health's status-hook diagnostic ---------------------------------------


def test_health_warns_about_a_conversation_only_the_session_names(store, monkeypatch):
    """`remembered` means the session's reference, not the argv's.

    The hard case is a record whose argv carries NO reference at all while an
    event has put the session on one — a `/clear` away from a fresh launch.
    Reading the argv there stopped the warning for precisely the sessions whose
    status rows had the most to report.
    """
    from hive_ide.health import SessionHealth

    record = _moved(store)
    record["driver"]["resume"] = {}
    store.write("sessions", record["id"], record)
    store.write(
        "status",
        record["id"],
        {
            "session_id": record["id"],
            "driver": "claude",
            "observed_at": "2026-11-23T00:00:00Z",
            # no conversation_reference: the partial report being diagnosed
        },
    )
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "windows", lambda self: {record["id"]: object()})
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})

    warnings = SessionHealth(store, frame).hook_warnings(record)

    assert any("remembered conversation reference" in w for w in warnings)
