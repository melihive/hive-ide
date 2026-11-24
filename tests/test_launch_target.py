"""Where a launch is going is decided ONCE, by the driver that would run it.

Four call sites used to answer "what conversation will this launch land on?"
and each answered differently: two by searching an argv for the reference, one
by reading the reference and the attach decision separately, and one —
`_agent_command` — by actually deciding. Searching the argv cannot answer it:

- a session NAMED after a conversation puts the id in the argv of a launch that
  cannot resume it, and a `resume_strategy: none` driver carries a reference it
  never renders;
- `agy -c` joins a conversation its argv never names;
- a declarative driver can render byte-identical argv with and without its
  reference and genuinely resume in both, so comparing the two renderings is no
  better than searching one of them;
- and a command this installation did not generate cannot be read by rendering
  our own, which is the reading half of the mistake `launch_is_ours` guards.

So `select_launch` makes one `LaunchTarget` per launch, from the strategy the
driver declares and one process snapshot, and validation, the collision guards,
the command tmux is given and the cleanup afterwards all read that one value.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hive_ide.agents import AgentResumeState
from hive_ide.conversation import ConversationGuard
from hive_ide.drivers import (
    LAUNCH_FRESH,
    LAUNCH_RESUME,
    LAUNCH_UNKNOWN,
    DriverRegistry,
    bundled_drivers,
)
from hive_ide.errors import HeldConversationError
from hive_ide.frame import LAUNCH_ATTACH, Frame, LaunchTarget
from hive_ide.holders import ConversationHolders
from hive_ide.store import StateStore

REF = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"


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


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def _session(store, name, driver, reference=None):
    return store.create_session(
        name=name,
        working_dir=store.workspace_key,
        source={"kind": "stable", "interpreter": sys.executable, "version": "test"},
        driver=driver.resolve(
            name=name,
            working_dir=str(store.workspace_key),
            conversation_reference=reference,
        ),
    )


def _frame(store, monkeypatch, registry: DriverRegistry | None = None) -> Frame:
    frame = Frame(store, socket="sock")
    if registry is not None:
        monkeypatch.setattr(Frame, "driver_registry", lambda self: registry)
    return frame


class _NamedFresh:
    """A plugin-shaped driver that renders the session NAME and cannot resume.

    `add_declarative` does not propagate `name_argv`, so a driver that emits the
    name while declaring `resume_strategy: none` can only come from a plugin —
    and that is the shape where searching the argv for the reference reports a
    resume that cannot happen.
    """

    id = "claude"
    label = "Named"
    launch_flags: list[str] = []

    def resolve(self, *, name, working_dir, conversation_reference):
        return {
            "id": self.id,
            "label": self.label,
            "launch_argv": ["claude", "--name", name],
            "resume": {"strategy": "none", "reference": conversation_reference},
            "capabilities": ["launch"],
        }

    def conversation_exists(self, reference, working_dir):
        return None     # no probe of its own: UNKNOWN, never "gone"


class _ClaimsResumeRendersNothing:
    """Declares id-resume and renders no id — the destination is unknowable."""

    id = "claude"
    label = "Opaque"
    launch_flags: list[str] = []

    def resolve(self, *, name, working_dir, conversation_reference):
        return {
            "id": self.id,
            "label": self.label,
            "launch_argv": ["claude"],
            "resume": {"strategy": "conversation_id", "reference": conversation_reference},
            "capabilities": ["launch", "resume"],
        }

    def conversation_exists(self, reference, working_dir):
        return None     # no probe of its own: UNKNOWN, never "gone"


class _NamedExists(_NamedFresh):
    """Renders the NAME, cannot resume — and can prove a conversation exists.

    The combination the adoption rule turns on: a candidate the driver vouches
    for, in a command that would not go there.
    """

    def conversation_exists(self, reference, working_dir):
        return reference == OTHER


def test_an_exact_resume_is_the_conversation_it_names(store, monkeypatch):
    """The compatibility control: the ordinary case keeps its answer.

    It stays green on the implementation this replaces, so it is evidence of no
    regression and of nothing else — every other test here is the discriminating
    one.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    target = _frame(store, monkeypatch).select_launch(record)

    assert (target.kind, target.reference) == (LAUNCH_RESUME, REF)
    assert list(target.argv) == record["driver"]["launch_argv"]
    # An established destination carries nothing beside it: `collides_with`
    # prefers `reference`, so a `carried` set here would be a second answer to
    # the same question, read by whichever caller looked at the wrong field.
    assert target.carried is None


def test_a_driver_that_cannot_resume_starts_a_fresh_conversation(store, monkeypatch):
    """A reference it never renders is not a destination, and not a collision.

    Searching the argv for the reference said `resume` for any driver carrying
    one, so the owner guard refused a launch that was never a collision and the
    holder guard refused an ordinary new `claude` because something else held a
    conversation this command would not have resumed.
    """
    registry = DriverRegistry({"claude": _NamedFresh()})
    record = _session(store, "SOME SESSION", registry.get("claude"), REF)
    frame = _frame(store, monkeypatch, registry)

    target = frame.select_launch(record)
    assert (target.kind, target.reference, target.carried) == (LAUNCH_FRESH, None, None)

    # And the owner guard declines, even though another session owns REF.
    _session(store, "B", bundled_drivers()["claude"], REF)
    frame.refuse_owned_conversation(record)


def test_a_session_named_after_a_conversation_is_not_assumed_to_be_fresh(
    store, monkeypatch
):
    """The one case the renderer cannot settle, and the direction chosen for it.

    `claude --name <id>` and a configured `command` of `["claude", "--resume",
    <id>]` with the default `resume_strategy: none` are the same two facts — the
    renderings do not vary with the reference, and the argv contains it — so no
    rule can tell the pathological name from the baked-in resume. Treating them
    as fresh told both guards there was nothing to collide over, and a second
    driver on a live conversation is the blank pane the guards exist for.
    Treating them as UNKNOWN costs a refusal for a session named exactly after a
    conversation id. That is the cheaper mistake, and this is where it is paid.
    """
    registry = DriverRegistry({"claude": _NamedFresh()})
    record = _session(store, REF, registry.get("claude"), REF)
    frame = _frame(store, monkeypatch, registry)

    target = frame.select_launch(record)
    assert REF in " ".join(record["driver"]["launch_argv"]), "the id IS in the argv"
    assert (target.kind, target.reference) == (LAUNCH_UNKNOWN, None)
    assert target.carried == REF, "a guard still has something to ask about"

    _session(store, "B", bundled_drivers()["claude"], REF)
    with pytest.raises(HeldConversationError):
        frame.refuse_owned_conversation(record)


def test_a_configured_command_that_bakes_in_a_resume_is_not_read_as_fresh(
    store, monkeypatch
):
    """`command = ["claude", "--resume", X]` with `resume_strategy` left at `none`.

    The driver declares nothing and the command resumes X anyway. Reading the
    declaration alone classified it fresh and dropped the reference, so both
    collision guards skipped it and a second driver started on X — which the
    argv search this replaced did catch.
    """
    registry = DriverRegistry()
    registry.add_declarative(
        "claude", {"command": ["claude", "--resume", REF], "capabilities": ["launch"]}
    )
    record = _session(store, "A", registry.get("claude"), REF)
    frame = _frame(store, monkeypatch, registry)

    target = frame.select_launch(record)
    assert (target.kind, target.carried) == (LAUNCH_UNKNOWN, REF)

    _session(store, "B", bundled_drivers()["claude"], REF)
    with pytest.raises(HeldConversationError):
        frame.refuse_owned_conversation(record)


def test_a_driver_that_renders_the_same_command_either_way_still_resumes(
    store, monkeypatch
):
    """The counterexample that rules out comparing the two renderings.

    `command` already carries `--resume <id>`, so the with-reference and
    without-reference renderings are byte-identical — and both resume. A diff
    sees no difference and would call a real resume `fresh`, which loses the
    owner check entirely.
    """
    registry = DriverRegistry()
    registry.add_declarative(
        "claude",
        {
            "command": ["claude", "--resume", REF],
            "resume_argv": ["claude", "--resume"],
            "resume_strategy": "conversation_id",
            "capabilities": ["launch", "resume"],
        },
    )
    definition = registry.get("claude")
    assert definition.resolve(
        name="A", working_dir=str(store.workspace_key), conversation_reference=None
    )["launch_argv"] == definition.resolve(
        name="A", working_dir=str(store.workspace_key), conversation_reference=REF
    )["launch_argv"], "the premise: one command, both ways"

    record = _session(store, "A", definition, REF)
    target = _frame(store, monkeypatch, registry).select_launch(record)
    assert (target.kind, target.reference) == (LAUNCH_RESUME, REF)


def test_a_command_this_installation_did_not_generate_has_no_known_destination(
    store, monkeypatch
):
    """A wrapper's own command is read by nobody, and refuses nothing.

    `resolve()` is the only renderer there is, so interpreting a foreign command
    through it would take the wrapper's flags and store selection to be ours.
    Unknown routes to the inert outcome for an interactive launch: the user's
    intact command still runs, and repair and `verify` still report the holder.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["driver"]["launch_argv"] = [
        "/opt/custom/claude", "--profile", "work", "--resume", REF,
    ]
    frame = _frame(store, monkeypatch)

    target = frame.select_launch(record)
    assert (target.kind, target.reference) == (LAUNCH_UNKNOWN, None)

    # It still refuses a collision, on what the command CARRIES: this one
    # plainly contains the id, and a command that carries the id can reach it.
    # That is what this package has always done, and for the common case — a
    # record rendered by an older version of the same driver definition — it is
    # right. What is gone is refusing on metadata alone.
    _session(store, "B", bundled_drivers()["claude"], REF)
    assert target.carried == REF
    with pytest.raises(HeldConversationError):
        frame.refuse_owned_conversation(record)

    # A wrapper that does NOT carry the id has nothing for a guard to act on.
    record["driver"]["launch_argv"] = ["/opt/custom/claude", "--profile", "work"]
    quiet = frame.select_launch(record)
    assert (quiet.kind, quiet.reference, quiet.carried) == (LAUNCH_UNKNOWN, None, None)
    frame.refuse_owned_conversation(record)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    frame.refuse_held_conversation(record)


def test_a_driver_that_claims_id_resume_and_renders_no_id_is_unknown(store, monkeypatch):
    """Declared strategy and rendered command disagree, so neither answer holds.

    Calling it `fresh` would be a guess — it may resume through something we
    cannot see — and calling it `resume` would be a claim with no evidence.
    """
    registry = DriverRegistry({"claude": _ClaimsResumeRendersNothing()})
    record = _session(store, "A", registry.get("claude"), REF)
    target = _frame(store, monkeypatch, registry).select_launch(record)
    assert (target.kind, target.reference) == (LAUNCH_UNKNOWN, None)


def test_bare_agy_is_fresh_and_a_continuation_is_unknown(store, monkeypatch):
    """Classify the COMMAND, not the driver.

    `agy` with nothing to resume starts a new conversation and is known to; only
    `agy -c` is unknowable, because it joins whatever that workspace considers
    current. Marking the whole driver unknown loses the first half.
    """
    frame = _frame(store, monkeypatch)
    antigravity = bundled_drivers()["antigravity"]

    fresh = _session(store, "A", antigravity, None)
    assert list(fresh["driver"]["launch_argv"]) == ["agy"]
    assert frame.select_launch(fresh).kind == LAUNCH_FRESH

    continued = _session(store, "B", antigravity, REF)
    assert list(continued["driver"]["launch_argv"]) == ["agy", "-c"]
    target = frame.select_launch(continued)
    assert (target.kind, target.reference) == (LAUNCH_UNKNOWN, None)


def test_the_command_is_selected_once_and_never_re_derived(store, monkeypatch):
    """One process snapshot per launch, and no second look when the command is built.

    Round 4: `prepare_agent_launch` read the background host and `_agent_command`
    read it again, so a host appearing in between turned the command into an
    attach and dropped the one-shot prompt it was supposed to carry, while the
    command reported success. That was fixed by adding another check beside the
    first; this pins that there is only one read to disagree with.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    frame = _frame(store, monkeypatch)
    snapshots: list[int] = []
    monkeypatch.setattr(
        ConversationHolders, "snapshot",
        classmethod(lambda cls: snapshots.append(1) or []),
    )
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})

    target = frame.prepare_agent_launch(record)
    assert len(snapshots) == 1, snapshots

    looks: list[int] = []
    monkeypatch.setattr(
        Frame, "_attach_argv",
        staticmethod(lambda driver, **kw: looks.append(1) or None),
    )
    Frame._agent_command(record, target)
    assert looks == [], "the command is the one already selected"


def test_an_attach_keeps_the_full_reference_its_argv_abbreviates(store, monkeypatch):
    """`claude attach` takes a short id; ownership is asked about the conversation.

    Carrying the short form as the destination would ask the owner and holder
    questions about a string no record holds.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    monkeypatch.setattr(
        ConversationHolders, "background_hosts",
        classmethod(lambda cls, ref, **kw: [{"pid": 4242, "background": True}]),
    )
    target = _frame(store, monkeypatch).select_launch(record)

    assert target.kind == LAUNCH_ATTACH
    assert target.reference == REF, "the full id, not the attach form"
    assert list(target.argv) == ["claude", "attach", ConversationHolders.attach_id(REF)]
    assert target.carries_a_prompt() is False


def test_a_pending_prompt_attaches_and_stays_unspent(store, monkeypatch):
    """An attach carries no prompt — so it keeps the prompt, it does not resume.

    Suppressing the attach to make room for the prompt was the wrong half of the
    trade: the launch resumed instead, which starts a SECOND driver on a
    conversation whose host is live, and the window build then cleared the
    prompt the moment tmux made the panes. The attach now wins, the prompt stays
    pending for the next real launch, and a caller that PROMISED to deliver one
    refuses here rather than delivering it into a second driver.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["launch_prompt"] = "carry on"
    monkeypatch.setattr(
        ConversationHolders, "background_hosts",
        classmethod(lambda cls, ref, **kw: [{"pid": 4242, "background": True}]),
    )
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    frame = _frame(store, monkeypatch)

    target = frame.select_launch(record)
    assert target.kind == LAUNCH_ATTACH
    assert target.carries_a_prompt() is False, "the cleanup must leave it pending"
    assert "carry on" not in " ".join(target.argv)

    # And the launch refuses rather than delivering it into a second driver.
    with pytest.raises(HeldConversationError) as refusal:
        frame.prepare_agent_launch(record)
    assert "carries no prompt" in str(refusal.value)


def test_the_direct_chat_path_attaches_instead_of_starting_a_second_driver(
    store, monkeypatch, claude_store
):
    """`chat` assembled its own argv, so it validated an attach and ran a resume.

    That is a second driver on a conversation whose host is live — the blank pane
    the holder guard exempts that host FOR — and `chat` is the command a
    driverless pane tells the user to run.
    """
    _transcript(claude_store, REF)
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    frame = _frame(store, monkeypatch)
    monkeypatch.setattr(
        ConversationHolders, "background_hosts",
        classmethod(lambda cls, ref, **kw: [{"pid": 4242, "background": True}]),
    )
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    monkeypatch.setattr(Frame, "clear_sleep", lambda self, rec: None)
    monkeypatch.setattr(Frame, "_clear_open_error", lambda self, rec: None)
    launched: list[list[str]] = []

    def run(argv, **kw):
        # `ps` comes through the same patched module; the launch is what matters.
        if list(argv)[:1] != ["ps"]:
            launched.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("hive_ide.frame.subprocess.run", run)

    frame.current_chat(record)
    assert launched == [
        ["claude", "attach", ConversationHolders.attach_id(REF)]
    ], launched


def test_cleanup_follows_the_command_that_launched_not_a_later_probe(
    store, monkeypatch, claude_store
):
    """A host appearing after the panes exist must not un-deliver a prompt.

    The handoff is cleared because the command that ran carried it. Re-probing
    the process table here instead reads "attach", leaves the prompt pending,
    and the next launch delivers it a second time.
    """
    _transcript(claude_store, REF)
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["handoff"] = {"target_driver_prompt": "carry on"}
    frame = _frame(store, monkeypatch)
    reads: list[int] = []

    def hosts(cls, ref, **kw):
        reads.append(1)
        # Nothing at selection; a host from the next read on.
        return [] if len(reads) <= 1 else [{"pid": 4242, "background": True}]

    monkeypatch.setattr(ConversationHolders, "background_hosts", classmethod(hosts))
    monkeypatch.setattr(Frame, "_plan_command", lambda self, rec: "true")
    monkeypatch.setattr(Frame, "_tag", lambda self, t, rec: None)
    monkeypatch.setattr(Frame, "_apply_columns", lambda self, t: None)
    monkeypatch.setattr(Frame, "_environment", lambda self, rec, role, **kw: [])
    monkeypatch.setattr(Frame, "_is_sleeping", lambda self, rec: False)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    monkeypatch.setattr(
        Frame, "tmux",
        lambda self, argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    cleared: list[dict] = []
    monkeypatch.setattr(
        Frame, "_clear_consumed_handoff", lambda self, rec: cleared.append(rec)
    )

    frame._finish_build(record, "@7", str(store.workspace_key))
    assert len(cleared) == 1, "the launched command carried the prompt"


def test_a_holder_does_not_refuse_a_launch_that_would_not_join_it(
    store, monkeypatch, claude_store
):
    """A process on REF must not refuse a command that starts a fresh chat.

    `refuse_held_conversation` read the record's metadata reference — the same
    one the owner check already skipped for this shape — so an ordinary
    `claude --resume REF` process refused a launch that would have run a plain
    `claude`. Exercised through `prepare_agent_launch`, because each predicate
    alone was green while the pair was not.
    """
    _transcript(claude_store, REF)      # a live conversation, not a dropped one
    registry = DriverRegistry({"claude": _NamedFresh()})
    record = _session(store, "A", registry.get("claude"), REF)
    frame = _frame(store, monkeypatch, registry)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    monkeypatch.setattr(
        ConversationHolders, "holders",
        classmethod(
            lambda cls, ref, **kw: [
                {"pid": 9191, "ppid": 1, "command": f"claude --resume {ref}",
                 "orphaned": True, "background": False}
            ]
        ),
    )

    frame.prepare_agent_launch(record)          # must not raise

    # The inverse, so this cannot become a guard that never refuses: the same
    # holder against a launch that WOULD join the conversation. A bundled
    # registry, because the driver above is the one that cannot resume.
    bundled = DriverRegistry()
    monkeypatch.setattr(Frame, "driver_registry", lambda self: bundled)
    resuming = _session(store, "B", bundled.get("claude"), REF)
    with pytest.raises(HeldConversationError):
        Frame(store, socket="sock").prepare_agent_launch(resuming)


def test_a_promise_is_refused_when_the_launch_would_not_keep_it(
    store, monkeypatch, claude_store
):
    """`wake` PROMISES a conversation, and a metadata reference is not a promise.

    The check compared the record's reference and nothing else, so two commands
    passed it that cannot keep it: a driver whose `resume_strategy` is `none`,
    which carries a reference it never renders, and a command this installation
    did not generate, whose destination cannot be established at all. In both
    cases the prompt went to a conversation nobody had established, and `wake`
    reported success.

    Checked here rather than through the CLI because this is the last place that
    can still refuse — another reconcile follows every earlier check.
    """
    _transcript(claude_store, REF)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})

    registry = DriverRegistry({"claude": _NamedFresh()})
    fresh = _session(store, "A", registry.get("claude"), REF)
    with pytest.raises(Exception) as fresh_refusal:
        _frame(store, monkeypatch, registry).prepare_agent_launch(
            fresh, expect_reference=REF
        )
    assert "start a new conversation" in str(fresh_refusal.value)

    bundled = DriverRegistry()
    monkeypatch.setattr(Frame, "driver_registry", lambda self: bundled)
    opaque = _session(store, "B", bundled.get("claude"), REF)
    opaque["driver"]["launch_argv"] = ["/opt/custom/claude", "--resume", REF]
    with pytest.raises(Exception) as opaque_refusal:
        Frame(store, socket="sock").prepare_agent_launch(opaque, expect_reference=REF)
    assert "did not generate" in str(opaque_refusal.value)


def test_the_guard_does_not_adopt_a_survivor_the_driver_cannot_resume(
    store, monkeypatch
):
    """Adoption writes a CLAIM, so it needs the command to actually go there.

    The fallback walks displaced references and adopts the newest that still
    exists. It asked whether the candidate rendering CONTAINED the reference,
    and a session named after a conversation contains it in its `--name` — so a
    driver that cannot resume at all adopted a conversation it would never open,
    and the claim then said the session was on it. The session name here is the
    candidate's id, which is the coincidence that made the old test pass.
    """
    registry = DriverRegistry({"claude": _NamedExists()})
    record = _session(store, OTHER, registry.get("claude"), REF)
    record.setdefault("agents", {}).setdefault("resume_history", {})["claude"] = [OTHER]
    store.write("sessions", record["id"], record)
    assert OTHER in " ".join(record["driver"]["launch_argv"]), "the premise"

    guard = ConversationGuard(store, registry=registry)
    guard.check(record, working_dir=str(store.workspace_key), apply=True)

    reloaded = store.read("sessions", record["id"]) or {}
    assert AgentResumeState(reloaded).reference_for("claude") is None, (
        "a conversation this launch cannot resume is not adopted"
    )
    assert OTHER not in " ".join(
        (reloaded.get("driver") or {}).get("resume", {}).get("reference") or ""
    )


def test_the_direct_chat_path_delivers_no_one_shot_input(
    store, monkeypatch, claude_store
):
    """It runs in the FOREGROUND for as long as the agent lives.

    A prompt cleared only when that exits is delivered twice by two overlapping
    `chat` calls — both build their argv while the record still holds it. So this
    path carries none: the input is taken off the record before the command is
    built and stays pending in the store for the window build, which is where a
    one-shot input is delivered and spent. That is also what this path did before
    the selection was threaded through it, and restoring a prompt to it is a
    feature, not a fix.
    """
    _transcript(claude_store, REF)
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["handoff"] = {"target_driver_prompt": "pick up the plan"}
    store.write("sessions", record["id"], record)
    frame = _frame(store, monkeypatch)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    monkeypatch.setattr(Frame, "clear_sleep", lambda self, rec: None)
    monkeypatch.setattr(Frame, "_clear_open_error", lambda self, rec: None)
    launched: list[str] = []

    def run(argv, **kw):
        if list(argv)[:1] != ["ps"]:
            launched.append(" ".join(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("hive_ide.frame.subprocess.run", run)

    frame.current_chat(store.read("sessions", record["id"]))
    frame.current_chat(store.read("sessions", record["id"]))

    assert len(launched) == 2, launched
    assert not any("pick up the plan" in call for call in launched)
    after = store.read("sessions", record["id"]) or {}
    assert after.get("handoff"), "it is still owed, not thrown away"


def test_a_launch_consumes_the_input_that_rode_and_not_the_other(
    store, monkeypatch, claude_store
):
    """A session can hold both a `create --prompt` and a handoff prompt.

    Exactly one rides — the handoff's — and clearing both discarded a prompt the
    driver never saw, with nothing left to deliver it later.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["handoff"] = {"target_driver_prompt": "from the handoff"}
    record["launch_prompt"] = "from create --prompt"
    store.write("sessions", record["id"], record)
    frame = _frame(store, monkeypatch)

    assert Frame._delivered_inputs(record) == ("handoff",)
    assert "from the handoff" in " ".join(Frame.agent_launch_argv(record))
    assert "from create --prompt" not in " ".join(Frame.agent_launch_argv(record))

    frame._clear_consumed_handoff(record)

    after = store.read("sessions", record["id"]) or {}
    assert "handoff" not in after
    assert after.get("launch_prompt") == "from create --prompt", "still owed"


def test_a_record_saved_before_a_launch_flag_existed_still_resumes(
    store, monkeypatch, claude_store
):
    """Codex isolation is applied at the execution boundary, including to a
    stored pre-fix argv — so a record saved before `--no-daemon` existed differs
    from the current rendering by exactly that flag. Reading it as a command this
    installation did not generate made its destination unknown, and `wake`
    refused every legacy Codex session.
    """
    codex = bundled_drivers()["codex"]
    record = _session(store, "A", codex, REF)
    modern = list(record["driver"]["launch_argv"])
    assert "--no-daemon" in modern, "the premise: the flag is in the current shape"
    record["driver"]["launch_argv"] = [a for a in modern if a != "--no-daemon"]
    store.write("sessions", record["id"], record)
    frame = _frame(store, monkeypatch)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})

    target = frame.select_launch(record)
    assert (target.kind, target.reference) == (LAUNCH_RESUME, REF)
    # The promise a `wake` makes is kept by this record.
    frame.prepare_agent_launch(record, expect_reference=REF)
    # And the flag is still applied to what actually runs.
    assert "--no-daemon" in target.argv


def test_a_malformed_resume_block_does_not_escape_the_launch(store, monkeypatch):
    """`"resume": "bad"` made `.get` raise AttributeError out of the selection.

    `open` handles a per-session `HiveIdeError` and carries on to the next
    window; an AttributeError escapes that, so one unreadable record stopped
    every later session from opening.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["driver"]["resume"] = "bad"
    frame = _frame(store, monkeypatch)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})

    target = frame.select_launch(record)          # must not raise
    assert target.reference is None and target.collides_with() is None
    frame.prepare_agent_launch(record)            # nor here


def test_a_queued_launch_carries_its_own_destination_to_the_gate(store, monkeypatch):
    """The gate re-checks for a holder at the end of its wait, in another process.

    It read the RECORD there, so a session that moved on during the wait had its
    frozen command released against a conversation that command does not name.
    The destination selected for this launch goes into the pane's ENVIRONMENT
    instead — never a new flag: the pane's interpreter may be pinned to an older
    package whose gate parser exits 2 on an argument it has never seen, after the
    shell has already `exec`ed it, so the pane would close with no agent.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    frame = _frame(store, monkeypatch)
    selected = frame.select_launch(record)

    env = " ".join(
        frame._environment(record, role="agent", gate_reference=selected.collides_with())
    )
    assert f"HIVE_IDE_LAUNCH_REFERENCE={REF}" in env
    monkeypatch.setattr(Frame, "_launch_path", lambda self: "/usr/bin")
    gated = " ".join(frame._driver_argv("claude --resume " + REF, delay=4.0))
    assert "--delay" in gated and "--reference" not in gated

    # A launch that joins no conversation carries no question to the gate.
    fresh = _session(store, "B", bundled_drivers()["claude"], None)
    assert frame.select_launch(fresh).collides_with() is None
    assert "HIVE_IDE_LAUNCH_REFERENCE" not in " ".join(
        frame._environment(fresh, role="agent", gate_reference=None)
    )


def test_the_build_hands_the_queued_launch_its_own_destination(
    store, monkeypatch, claude_store
):
    """The window build is where a launch gets queued, so it is where the gate's
    question is set. Passing nothing left the gate re-deriving it from the record
    at the end of its wait — a record the session may have moved on from, while
    the frozen command has not.
    """
    _transcript(claude_store, REF)
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    frame = _frame(store, monkeypatch)
    calls: list[list[str]] = []

    def tmux(self, argv, **kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(Frame, "tmux", tmux)
    monkeypatch.setattr(Frame, "_plan_command", lambda self, rec: "true")
    monkeypatch.setattr(Frame, "_tag", lambda self, t, rec: None)
    monkeypatch.setattr(Frame, "_apply_columns", lambda self, t: None)
    # `_environment` is NOT faked here: it is what carries the question.
    monkeypatch.setattr(Frame, "_is_sleeping", lambda self, rec: False)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    monkeypatch.setattr(Frame, "_launch_path", lambda self: "/usr/bin")

    frame._finish_build(record, "@7", str(store.workspace_key), launch_delay=4.0)

    agent = [c for c in calls if any("launch_gate" in str(part) for part in c)]
    assert agent, "the agent pane was not queued behind the gate"
    assert f"HIVE_IDE_LAUNCH_REFERENCE={REF}" in " ".join(agent[0])
    plan = [c for c in calls if "true" in [str(part) for part in c]]
    assert plan and "HIVE_IDE_LAUNCH_REFERENCE" not in " ".join(plan[0]), (
        "only the pane that waits behind the gate is asked the question"
    )

    # And an ATTACH is asked nothing: it joins that host on purpose, so the host
    # is not a collision for it, and the gate would refuse its own command.
    calls.clear()
    monkeypatch.setattr(
        ConversationHolders, "background_hosts",
        classmethod(lambda cls, ref, **kw: [{"pid": 4242, "background": True}]),
    )
    frame._finish_build(record, "@7", str(store.workspace_key), launch_delay=4.0)
    attaching = [c for c in calls if any("launch_gate" in str(part) for part in c)]
    assert attaching, "the attach was not queued behind the gate"
    joined = " ".join(attaching[0])
    # Set, and EMPTY: a frame that speaks this protocol always answers, so the
    # gate can tell a considered "nothing to check" from an older frame that
    # carried no answer and must fall back to the record.
    assert "HIVE_IDE_LAUNCH_REFERENCE=" in joined
    assert f"HIVE_IDE_LAUNCH_REFERENCE={REF}" not in joined


def test_consuming_a_prompt_never_writes_back_a_session_that_is_gone(
    store, monkeypatch, claude_store
):
    """On the `chat` path this runs after an interactive driver EXITS.

    That can be hours, and the read used to fall back to the in-memory record
    and write it back — so a session archived or deleted while the driver was
    open came back from the dead, with whatever else the record held at launch
    time.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["handoff"] = {"target_driver_prompt": "pick up the plan"}
    store.write("sessions", record["id"], record)
    frame = _frame(store, monkeypatch)

    store.delete("sessions", record["id"])      # archived while the driver ran
    frame._clear_consumed_handoff(record)

    assert store.read("sessions", record["id"]) is None, "it stayed gone"
    assert "handoff" not in record, "and the in-memory copy is still consumed"


def test_a_prompt_queued_after_delivery_is_not_consumed_by_the_old_launch(
    store, monkeypatch, claude_store
):
    """Only the inputs THIS launch carried are spent.

    The cleanup popped the keys by name, so a handoff queued while the driver was
    running was deleted by the launch that never delivered it — and the next
    launch had nothing to deliver.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["handoff"] = {"target_driver_prompt": "the first one"}
    store.write("sessions", record["id"], record)
    frame = _frame(store, monkeypatch)

    queued = store.read("sessions", record["id"])
    queued["handoff"] = {"target_driver_prompt": "queued while it ran"}
    store.write("sessions", record["id"], queued)

    frame._clear_consumed_handoff(record)

    after = store.read("sessions", record["id"]) or {}
    assert after.get("handoff") == {"target_driver_prompt": "queued while it ran"}


def test_a_background_host_only_exempts_a_launch_that_is_attaching(
    store, monkeypatch, claude_store
):
    """The exemption exists because an attach JOINS that host on purpose.

    Applied to every launch kind, it also exempted a queued `--resume` released
    after the conversation moved into the background — which is a second driver
    on a live conversation, the exact fault this guard exists for. The launch
    gate reaches that state by construction: it freezes a command and re-checks
    seconds later.
    """
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    frame = _frame(store, monkeypatch)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    host = {
        "pid": 4242, "ppid": 1, "command": f"claude bg-pty-host --session-id {REF}",
        "orphaned": False, "background": True,
    }
    monkeypatch.setattr(
        ConversationHolders, "holders", classmethod(lambda cls, ref, **kw: [host])
    )
    monkeypatch.setattr(
        ConversationHolders, "background_tree", classmethod(lambda cls, ref, **kw: {4242})
    )

    resuming = LaunchTarget(LAUNCH_RESUME, "claude", REF, None, ())
    with pytest.raises(HeldConversationError):
        frame.refuse_held_conversation(record, resuming)

    attaching = LaunchTarget(LAUNCH_ATTACH, "claude", REF, None, ())
    frame.refuse_held_conversation(record, attaching)      # must not raise


def test_the_respawn_path_keeps_a_handoff_an_attach_could_not_deliver(
    store, monkeypatch, claude_store
):
    """`chat` into an idle agent pane cleared the handoff whatever it launched.

    With a background host the respawn attaches, an attach carries no argv, and
    the prompt was dropped without ever being delivered. The respawn now
    consumes what the command it launched carried, which is the same rule the
    window build uses.
    """
    _transcript(claude_store, REF)
    record = _session(store, "A", bundled_drivers()["claude"], REF)
    record["handoff"] = {"target_driver_prompt": "pick up the plan"}
    store.write("sessions", record["id"], record)
    frame = _frame(store, monkeypatch)
    monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: False)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    monkeypatch.setattr(Frame, "safe_working_dir", lambda self, rec: "/tmp")
    monkeypatch.setattr(Frame, "_environment", lambda self, rec, role, **kw: [])
    monkeypatch.setattr(Frame, "_clear_open_error", lambda self, rec: None)
    monkeypatch.setattr(
        Frame, "tmux",
        lambda self, argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
    )
    monkeypatch.setattr(
        ConversationHolders, "background_hosts",
        classmethod(lambda cls, ref, **kw: [{"pid": 4242, "background": True}]),
    )

    assert frame.respawn_agent(store.read("sessions", record["id"]), "%2") is True

    after = store.read("sessions", record["id"]) or {}
    assert after.get("handoff"), "an attach delivered nothing, so nothing is spent"

    # The inverse, so this cannot become a respawn that never consumes: with no
    # host the command is a resume, the prompt rides on its argv, and it is spent.
    monkeypatch.setattr(
        ConversationHolders, "background_hosts", classmethod(lambda cls, ref, **kw: [])
    )
    assert frame.respawn_agent(store.read("sessions", record["id"]), "%2") is True
    assert not (store.read("sessions", record["id"]) or {}).get("handoff")


def test_the_gate_asks_about_the_frozen_command_not_the_current_record(
    store, monkeypatch, claude_store
):
    """The whole round trip, in the shape the gate really runs in.

    The gate is a separate process with no record in hand. Two earlier versions
    of this test observed nothing: one asserted None with the store environment
    removed, which is None whether the environment is read or not; the other gave
    the environment and the record the SAME reference and returned a holder for
    anything, so an implementation that merely checked the variable EXISTED and
    then read the record still passed. Here the record has moved on — which is
    the whole point of freezing the destination — and only the frozen one is
    held.
    """
    from hive_ide import launch_gate

    record = _session(store, "A", bundled_drivers()["claude"], REF)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", str(store.workspace_key))
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    # The session moved to OTHER while the command for REF waited.
    moved = store.read("sessions", record["id"])
    moved["driver"] = bundled_drivers()["claude"].resolve(
        name="A", working_dir=str(store.workspace_key), conversation_reference=OTHER
    )
    store.write("sessions", record["id"], moved)
    monkeypatch.setattr(
        ConversationHolders, "holders",
        classmethod(
            lambda cls, ref, **kw: [
                {"pid": 9191, "ppid": 1, "command": f"claude --resume {ref}",
                 "orphaned": True, "background": False}
            ]
            if ref == REF      # only the FROZEN conversation is held
            else []
        ),
    )

    monkeypatch.setenv("HIVE_IDE_LAUNCH_REFERENCE", REF)
    reason = launch_gate.held_conversation_reason()
    assert reason and "9191" in reason, reason

    # EMPTY: this frame considered the question and there is none to ask, even
    # though the record names a conversation.
    monkeypatch.setenv("HIVE_IDE_LAUNCH_REFERENCE", "")
    assert launch_gate.held_conversation_reason() is None

    # ABSENT: an older frame built this command and carried no answer, so the
    # record is asked exactly as that frame's own gate asked it — which here
    # names OTHER, and nothing holds OTHER.
    monkeypatch.delenv("HIVE_IDE_LAUNCH_REFERENCE")
    assert launch_gate.held_conversation_reason() is None

    # ...and the same absent case DOES refuse when the record's own conversation
    # is the held one, which is the guard the mixed pairing would otherwise lose.
    back = store.read("sessions", record["id"])
    back["driver"] = bundled_drivers()["claude"].resolve(
        name="A", working_dir=str(store.workspace_key), conversation_reference=REF
    )
    store.write("sessions", record["id"], back)
    legacy = launch_gate.held_conversation_reason()
    assert legacy and "9191" in legacy, legacy


def test_a_command_that_forks_is_not_the_conversation_it_names(store, monkeypatch):
    """`--fork-session` starts a NEW conversation from the one it resumes.

    The id beside it is the SOURCE, so a command carrying it lands somewhere the
    record cannot name — and `wake` promised delivery into the source while the
    prompt went to the fork. The option list is the one the holder path already
    keeps for this exact reason, and like there it can only ever refuse.
    """
    registry = DriverRegistry()
    registry.add_declarative(
        "claude",
        {
            "command": ["claude"],
            "resume_argv": ["claude", "--fork-session", "--resume"],
            "resume_strategy": "conversation_id",
            "capabilities": ["launch", "resume"],
        },
    )
    record = _session(store, "A", registry.get("claude"), REF)
    frame = _frame(store, monkeypatch, registry)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    assert REF in " ".join(record["driver"]["launch_argv"]), "the id IS in the argv"

    target = frame.select_launch(record)
    assert (target.kind, target.reference) == (LAUNCH_UNKNOWN, None)
    assert target.carried == REF, "a guard still asks about what it carries"

    with pytest.raises(Exception) as refusal:
        frame.prepare_agent_launch(record, expect_reference=REF)
    assert "did not generate" in str(refusal.value) or "new conversation" in str(
        refusal.value
    )


def test_a_configured_continuation_still_guards_the_conversation_it_may_rejoin(
    store, monkeypatch, claude_store
):
    """`claude --continue` names no id and rejoins the latest conversation.

    With the record naming X and a `claude --resume X` process alive, reading the
    command as fresh dropped the guard for a launch that can genuinely collide —
    main asked about that reference. It carries no destination, so it cannot be
    promised; it carries a possible one, so it is still asked about.
    """
    _transcript(claude_store, REF)
    registry = DriverRegistry()
    registry.add_declarative(
        "claude", {"command": ["claude", "--continue"], "capabilities": ["launch"]}
    )
    record = _session(store, "A", registry.get("claude"), REF)
    frame = _frame(store, monkeypatch, registry)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {})
    assert REF not in " ".join(record["driver"]["launch_argv"]), "and names no id"

    target = frame.select_launch(record)
    assert (target.kind, target.carried) == (LAUNCH_UNKNOWN, REF)

    monkeypatch.setattr(
        ConversationHolders, "holders",
        classmethod(
            lambda cls, ref, **kw: [
                {"pid": 9191, "ppid": 1, "command": f"claude --resume {ref}",
                 "orphaned": True, "background": False}
            ]
        ),
    )
    with pytest.raises(HeldConversationError):
        frame.prepare_agent_launch(record)
