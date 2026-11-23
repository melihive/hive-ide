"""Reconcile a session's driver conversation references with each driver's own store."""

from __future__ import annotations

from typing import Any

from .agents import AgentResumeState
from .drivers import ConversationState, ConversationStatus, DriverRegistry
from .errors import HiveIdeError
from .store import StateStore
from .turns import TurnSequence


class ConversationGuard:
    """Act only on answers the driver can prove; leave everything else alone.

    - gone: fall back to the newest remembered reference that still exists, and
      only start a fresh conversation when none does.
    - archived: keep the reference and warn; the driver can restore it.
    - exists / unknown: keep the reference untouched.
    """

    def __init__(self, store: StateStore, *, registry: DriverRegistry | None = None):
        self.store = store
        self.registry = registry or DriverRegistry()

    def check(
        self, record: dict[str, Any], *, working_dir: str, apply: bool
    ) -> dict[str, Any]:
        """Reconcile a session's remembered references, then its launch.

        Two jobs, kept apart, because conflating them is what six review rounds
        kept finding. Bookkeeping first: a remembered reference the driver
        confirms is gone is dropped, an archived one is reported. Then ONE
        decision about the launch, and only when the conversation the launch
        ITSELF names is confirmed gone — never because some other remembered
        reference beside it was.

        That distinction is the whole lesson. Deciding from `resume_ids` reset a
        `codex` session that resumes by NAME to a bare `codex`, because a stale
        UUID sitting next to it had expired; deciding from "which reference
        matched" left a launch resuming a conversation that was already dead.
        The launch names what it names, and that is what has to be true.
        """
        actions: list[str] = []
        warnings: list[str] = []
        agents = AgentResumeState(record)
        changed = False
        for driver_id, reference in list(agents.resume_ids.items()):
            if not isinstance(driver_id, str) or not isinstance(reference, str):
                continue
            status = self.status(driver_id, reference, working_dir)
            short = reference[:8]
            if status.state == ConversationState.GONE:
                agents.forget(driver_id)
                changed = True
                actions.append(
                    f"driver: forgot {driver_id} conversation {short}, which no "
                    "longer exists"
                )
            elif status.state == ConversationState.ARCHIVED:
                warnings.append(
                    f"{driver_id} conversation {short} is archived; {status.detail}"
                )
        if self._reconcile_launch(record, agents, working_dir, actions, warnings):
            changed = True
        if changed and apply:
            self.store.write("sessions", record["id"], record)
        return {"changed": changed, "actions": actions, "warnings": warnings}

    def _reconcile_launch(
        self,
        record: dict[str, Any],
        agents: AgentResumeState,
        working_dir: str,
        actions: list[str],
        warnings: list[str],
    ) -> bool:
        """Bring the launch into line with the session's own references.

        Two reasons to rewrite a launch, and this is the only place either one
        is acted on, because here the session's own drivers are known:

        - the session is on a DIFFERENT conversation than the launch resumes.
          An event records that in `resume_ids` (proven, per `had_a_turn`) and
          deliberately does not touch the command — a `/clear` mints a new
          conversation, and this is where the launch catches up with it.
        - the conversation the launch names is confirmed GONE, and then the
          newest remembered survivor takes its place, or it starts fresh.

        What is NOT a reason: some other remembered reference being gone.
        Deciding from `resume_ids` alone reset a `codex` session that resumes by
        NAME to a bare `codex`, because a stale UUID beside it had expired.
        """
        driver = record.get("driver") or {}
        driver_id = driver.get("id")
        if not isinstance(driver_id, str) or not driver_id:
            return False
        launched = AgentResumeState.reference_from_driver(driver)
        current = agents.reference_for(driver_id)
        if current and current != launched:
            if not self._may_re_render(record, driver_id, launched, warnings):
                return False
            self._resolve_reference(record, driver_id, current)
            actions.append(
                f"driver: {driver_id} is on conversation {current[:8]}; the launch "
                + (
                    f"resumed {launched[:8]} and now resumes that one"
                    if launched
                    else "now resumes it"
                )
            )
            return True
        if not launched:
            # Nothing to resume, or a driver that resumes some other way (a
            # `codex` session addressed by name). This launch makes no claim
            # about a conversation, so there is nothing to correct.
            return False
        if self.status(driver_id, launched, working_dir).state != ConversationState.GONE:
            return False
        if not self._may_re_render(record, driver_id, launched, warnings):
            return False
        short = launched[:8]
        replacement = self._previous_that_survives(
            agents, driver_id, working_dir, record=record
        )
        if replacement:
            self._resolve_reference(record, driver_id, replacement)
            agents.remember(driver_id, replacement)
            actions.append(
                f"driver: {driver_id} conversation {short} no longer exists; the "
                f"launch now resumes {replacement[:8]}, which still does"
            )
        else:
            self._resolve_fresh(record, driver_id)
            actions.append(
                f"driver: {driver_id} conversation {short} no longer exists and "
                "nothing remembered survives it; the next launch starts fresh"
            )
        return True

    def _may_re_render(
        self,
        record: dict[str, Any],
        driver_id: str,
        launched: str | None,
        warnings: list[str],
    ) -> bool:
        """May this launch be rewritten at all? Warn rather than substitute.

        `resolve()` is the only renderer, so rewriting a command this
        installation did not generate would replace a wrapper's own executable
        and flags with the bundled shape. A visible stalemate beats a silent
        substitution.
        """
        if self.launch_is_ours(record, driver_id):
            return True
        warnings.append(
            f"{driver_id} launch"
            + (f" resumes conversation {launched[:8]}, which" if launched else " which")
            + " needs rewriting, but it is not a command this installation "
            "generates (a configured wrapper, or a record from another version); "
            "it is left untouched"
        )
        return False

    #: Events only a real turn produces. `UserPromptSubmit` opens one and `Stop`
    #: closes one; `SessionStart` and `Notification` say nothing about whether a
    #: word was ever exchanged, and a launch nobody types into sends only those.
    TURN_PROVING_EVENTS = frozenset({TurnSequence.PROMPT, TurnSequence.STOP})

    def launch_is_ours(self, record: dict[str, Any], driver_id: str) -> bool:
        """Is the record's CURRENT launch one this registry would generate?

        Only then may it be re-rendered with a different conversation, because
        `resolve()` is the only renderer there is: re-rendering a launch this
        package did not produce replaces it with the shape this package knows.
        Three reviews found three faces of that one mistake — a declarative
        wrapper's executable and flags replaced by the bundled driver, a `codex`
        launch that resumes by session NAME reset to a bare `codex` because a
        stale UUID beside it was gone, and an event arriving without the
        `--config` the session was opened with rebuilding the command from a
        different configuration.

        So the kill path's rule from Phase 83 applies here too: only the launch
        this package DEFINES can authorize acting on it. Everything else keeps
        the launch it has, and the reference still moves in `resume_ids` — the
        next launch this package renders will pick it up.
        """
        driver = record.get("driver") or {}
        if driver.get("id") != driver_id:
            return False
        try:
            definition = self.registry.get(driver_id)
        except HiveIdeError:
            return False
        ours = definition.resolve(
            name=str(record.get("name") or ""),
            working_dir=str(record.get("working_dir") or self.store.workspace_key),
            conversation_reference=AgentResumeState.reference_from_driver(driver),
        )
        return list(ours.get("launch_argv") or []) == list(driver.get("launch_argv") or [])

    def launch_would_resume(
        self, record: dict[str, Any], driver_id: str, reference: str
    ) -> bool:
        """Would this driver's generated launch actually resume `reference`?

        A declarative driver can declare a conversation probe and still have no
        way to resume (`resume_strategy` defaults to `none`), so the guard could
        adopt a surviving conversation whose launch was just the executable and
        the prompt — a fresh chat wearing a resumed conversation's name.
        """
        try:
            definition = self.registry.get(driver_id)
        except HiveIdeError:
            return False
        ours = definition.resolve(
            name=str(record.get("name") or ""),
            working_dir=str(record.get("working_dir") or self.store.workspace_key),
            conversation_reference=reference,
        )
        return any(
            reference in str(part) for part in ours.get("launch_argv") or []
        )

    def had_a_turn(
        self,
        driver_id: str,
        reference: str | None,
        *,
        working_dir: str,
        payload: dict[str, Any] | None = None,
        state: str | None = None,
    ) -> bool:
        """Has this conversation actually been used? Either proof is enough.

        A transcript is the stronger one: `claude` writes the file on the first
        turn, so a reference with no transcript anywhere is an id that has held
        no conversation yet (verified against the 2026-11-22 DJ DEV launch,
        which left its session-env and `started` directories but no transcript).
        The proof is the file's EXISTENCE, as everywhere else in this package —
        nothing here reads its contents, so a driver that one day writes an
        empty transcript at startup would need this rule revisited.

        The event is the other proof, for a driver whose conversation store this
        package cannot read: it answers UNKNOWN for every reference, and
        refusing them all would stop it recording anything at all.

        Only ever used to decide whether a reference may DISPLACE a different
        one; an empty slot is filled either way. False on anything unproven,
        which is the inert direction — the session keeps the conversation it
        already has, and the next real turn records the new one.
        """
        if not reference:
            return False
        if TurnSequence.event_name(payload or {}, state) in self.TURN_PROVING_EVENTS:
            return True
        return self.status(driver_id, reference, working_dir).state == ConversationState.EXISTS

    def status(self, driver_id: str, reference: str, working_dir: str) -> ConversationStatus:
        try:
            driver = self.registry.get(driver_id)
        except HiveIdeError:
            return ConversationStatus(ConversationState.UNKNOWN)
        probe = getattr(driver, "conversation_status", None)
        if callable(probe):
            return probe(reference, working_dir)
        exists = driver.conversation_exists(reference, working_dir)
        if exists is True:
            return ConversationStatus(ConversationState.EXISTS)
        if exists is False:
            return ConversationStatus(ConversationState.GONE)
        return ConversationStatus(ConversationState.UNKNOWN)

    def _previous_that_survives(
        self,
        agents: AgentResumeState,
        driver_id: str,
        working_dir: str,
        *,
        record: dict[str, Any],
    ) -> str | None:
        """The newest displaced reference the driver can still prove exists.

        A session whose current conversation is gone usually still has the one
        before it, and resuming that is strictly better than starting empty: on
        2026-11-22 a DJ DEV repair reset the driver to a bare
        `claude --name "DJ DEV"` while 69 MB of history sat one reference back,
        and only a hand-edit saved it.

        Walks newest first and takes the first PROVEN, UNOWNED survivor. A candidate the
        driver confirms is gone is pruned — it cannot come back — while one it
        cannot speak for is left in history and skipped, because an unprovable
        reference is exactly what makes a pane exit on `--resume`.
        """
        for candidate in agents.history_for(driver_id):
            state = self.status(driver_id, candidate, working_dir).state
            if state == ConversationState.GONE:
                agents.drop_from_history(driver_id, candidate)
                continue
            if state != ConversationState.EXISTS:
                continue
            if not self.launch_would_resume(record, driver_id, candidate):
                continue
            if self.store.find_conversation_owner(
                driver_id=driver_id,
                reference=candidate,
                exclude_session_id=str(record.get("id") or ""),
            ) is not None:
                # Another session took it while we were not using it. Two
                # drivers on one conversation leave a pane permanently blank,
                # which is the fault the holder guard exists for — so a
                # displaced reference is never reclaimed from its new owner.
                continue
            agents.adopt(driver_id, candidate)
            return candidate
        return None

    def _resolve_reference(
        self, record: dict[str, Any], driver_id: str, reference: str
    ) -> None:
        try:
            driver = self.registry.get(driver_id)
        except HiveIdeError:
            return
        record["driver"] = driver.resolve(
            name=str(record.get("name") or ""),
            working_dir=str(record.get("working_dir") or self.store.workspace_key),
            conversation_reference=reference,
        )

    def _resolve_fresh(self, record: dict[str, Any], driver_id: str) -> None:
        try:
            driver = self.registry.get(driver_id)
        except HiveIdeError:
            return
        record["driver"] = driver.resolve(
            name=str(record.get("name") or ""),
            working_dir=str(record.get("working_dir") or self.store.workspace_key),
            conversation_reference=None,
        )
