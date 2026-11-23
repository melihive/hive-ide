"""Session agent state helpers."""

from __future__ import annotations

from typing import Any


class AgentResumeState:
    """Maintain per-driver resume references on a session record."""

    def __init__(self, record: dict[str, Any]):
        self.record = record
        agents = record.get("agents")
        # A record with no `agents` block at all predates per-driver references:
        # its launch is the only claim it has ever made, so converting it is the
        # moment that claim becomes explicit. A record that HAS the block has
        # already spoken for itself — an empty `resume_ids` is a session that
        # remembered and forgot, and forgetting is not an invitation to re-infer.
        legacy = not isinstance(agents, dict)
        if legacy:
            agents = {}
            record["agents"] = agents
        self.agents = agents
        resume_ids = agents.get("resume_ids")
        if not isinstance(resume_ids, dict):
            resume_ids = {}
            agents["resume_ids"] = resume_ids
        self.resume_ids = resume_ids
        history = agents.get("resume_history")
        if not isinstance(history, dict):
            history = {}
            agents["resume_history"] = history
        self.resume_history = history
        self.seed_current_driver(legacy=legacy)

    @staticmethod
    def reference_from_driver(driver: dict[str, Any]) -> str | None:
        """The conversation the STORED LAUNCH COMMAND would resume.

        Not the same question as `session_reference`. Read this one only when
        the subject is the command itself — matching a process's argv, or
        deciding what a relaunch is about to do — and read it only AFTER
        `ConversationGuard` has reconciled the launch, which `Frame` does in
        `prepare_agent_launch` before anything inspects the argv.

        `resume` is type-checked, not merely truth-checked. `("bad" or {})` is
        the string, and `.get` on it raises — which did not matter while every
        caller held a record it had just written, and does now: ownership asks
        this of every session in every workspace, so ONE malformed record
        anywhere aborted the scan before it reached the real owner.
        """
        resume = driver.get("resume") if isinstance(driver, dict) else None
        reference = resume.get("reference") if isinstance(resume, dict) else None
        return reference if isinstance(reference, str) and reference else None

    @staticmethod
    def session_reference(
        record: dict[str, Any], driver_id: str | None = None
    ) -> str | None:
        """The conversation this session is ON, whoever rendered its launch.

        Status events move a session to a new conversation by writing
        `agents.resume_ids` alone; the launch command is only brought into line
        at launch time. Between those two moments the two fields name different
        conversations, and every question of the form "what is this session
        talking to right now" — is a holder stranded on it, what does the
        sidebar show, which chat does `current_chat` open — is about
        `resume_ids`, never about the argv.

        Falls back to the launched reference ONLY for a record with no `agents`
        block at all — one written before per-driver references, whose launch is
        the only claim it has ever been able to make. This is the same
        absent-versus-empty rule the constructor and `_owns_conversation` use,
        and it has to be, or the three disagree about one record.

        An empty `resume_ids` is NOT that case. It is a session that recorded a
        reference and released it, and answering with its argv hands back the
        conversation it just gave up — to a sleeping session's status row, to
        the sidebar, and to the repair that goes hunting for a holder of it.
        Those readers all ask "what is this session on right now", and the
        honest answer for a session that claims nothing is nothing.

        `driver_id` names a driver to ask about; omitting it asks about the
        session's current one. Naming one scopes the fallback — a launch speaks
        for its own driver and for no other — while omitting it falls back to
        the launch whatever the record calls it, so a driver block too thin to
        carry an `id` degrades to exactly what reading the launch gave before.

        Static and non-mutating on purpose. Constructing `AgentResumeState`
        would answer the same question — `seed_current_driver` fills the empty
        slot — but it writes three keys into the record, which a read-only
        caller such as `verify` must not do.
        """
        driver = record.get("driver")
        driver = driver if isinstance(driver, dict) else {}
        wanted = driver_id or driver.get("id")
        if isinstance(wanted, str) and wanted:
            agents = record.get("agents")
            resume_ids = agents.get("resume_ids") if isinstance(agents, dict) else None
            if isinstance(resume_ids, dict):
                remembered = resume_ids.get(wanted)
                if isinstance(remembered, str) and remembered:
                    return remembered
        if driver_id is not None and driver.get("id") != driver_id:
            return None
        # An empty block means "released" only for a record that could have
        # recorded a claim in the first place. Claims are keyed by driver, so a
        # record whose driver block carries no `id` has no key to store one
        # under — its empty block is silence, not a release, and its launch
        # still speaks. `create_session` writes a block for every new record,
        # so without this an id-less driver lost its reference the moment the
        # session was created and the sidebar showed nothing.
        own_driver_id = driver.get("id")
        if isinstance(record.get("agents"), dict) and (
            isinstance(own_driver_id, str) and own_driver_id
        ):
            return None
        return AgentResumeState.reference_from_driver(driver)

    def seed_current_driver(self, *, legacy: bool = False) -> None:
        """Reconcile the launched conversation with the driver's references.

        Constructing this object must not ACQUIRE a conversation. A claim is
        made at the two moments a session can honestly make one — it is created
        on a conversation, or it is told about one — and an empty slot means the
        opposite of a claim: the session had a reference and gave it up. Seeding
        the launch back into that slot handed the conversation straight back, so
        one `status-event` after a forget could take it from whichever session
        had since attached it, with nothing but a stale argv as evidence. The
        event did not even have to carry a reference; merely constructing the
        state object was enough.

        `legacy` is the one case where the launch still speaks: a record with no
        `agents` block at all has never recorded a reference anywhere else, so
        converting it is where its existing claim becomes explicit.

        The other half stands on its own. When `resume_ids` names a DIFFERENT
        conversation the record is internally inconsistent — which older
        versions of this package could write, by moving `resume_ids` without
        re-rendering the launch — and the launched reference is remembered as
        history rather than dropped, so an event confirming the other one cannot
        quietly erase the last trace of a real conversation. That needs a
        current reference to disagree WITH, so an empty slot is not it.
        """
        driver = self.record.get("driver")
        if not isinstance(driver, dict):
            return
        driver_id = driver.get("id")
        reference = self.reference_from_driver(driver)
        if not (isinstance(driver_id, str) and driver_id and reference):
            return
        current = self.reference_for(driver_id)
        if not current:
            if legacy:
                self.resume_ids[driver_id] = reference
            return
        if current != reference and reference not in self.history_for(driver_id):
            self._set_history(driver_id, [reference, *self.history_for(driver_id)])

    #: How many displaced references to keep per driver. The fallback only ever
    #: wants the newest one that still exists; a long tail is dead weight that
    #: ages out of the driver's own store anyway.
    HISTORY_LIMIT = 8

    def remember(
        self, driver_id: str | None, reference: str | None, *, proven: bool = True
    ) -> bool:
        """Record a conversation reference. Returns whether it was taken.

        `proven=False` says the caller could not show this conversation has had
        a real turn. An unproven reference may FILL an empty slot — a session
        that has never recorded one loses nothing by trusting it — but it may
        never REPLACE a different reference that is already there.

        That asymmetry is the 2026-11-22 DJ DEV incident. A `claude` launch with
        no prompt minted `44dcfed8`, its hook event arrived, and the id went
        straight into `resume_ids.claude` over `b5050320` — a conversation live
        since 10-30 with a 69 MB transcript. Nothing about the launch was
        abnormal; `claude --resume` mints a temporary id of its own at startup
        too, so a resume that dies early can do the same thing. The reference a
        session has is worth more than the one that just turned up, until the
        new one proves it is real.

        A replaced reference is not discarded: it goes to the front of the
        driver's history, which is what lets `ConversationGuard` fall back to it
        instead of starting fresh.
        """
        if not driver_id or not reference:
            return False
        current = self.reference_for(driver_id)
        if current == reference:
            return True
        if current and not proven:
            return False
        # One pass, then one trim: dropping the reactivated reference AFTER
        # trimming would spend a slot on it and evict the oldest entry, which
        # may be the only survivor left to fall back to.
        kept = [r for r in self.history_for(driver_id) if r not in (current, reference)]
        self._set_history(driver_id, [current, *kept] if current else kept)
        self.resume_ids[driver_id] = reference
        return True

    def forget(self, driver_id: str | None) -> None:
        if driver_id:
            self.resume_ids.pop(driver_id, None)

    def history_for(self, driver_id: str) -> list[str]:
        """The driver's displaced references, newest first."""
        values = self.resume_history.get(driver_id)
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, str) and value]

    def _set_history(self, driver_id: str, references: list[str]) -> None:
        trimmed = references[: self.HISTORY_LIMIT]
        if trimmed:
            self.resume_history[driver_id] = trimmed
        else:
            self.resume_history.pop(driver_id, None)

    def adopt(self, driver_id: str, reference: str) -> None:
        """Make a remembered reference current again, taking it out of history."""
        self._set_history(
            driver_id, [r for r in self.history_for(driver_id) if r != reference]
        )
        self.resume_ids[driver_id] = reference

    def drop_from_history(self, driver_id: str, reference: str) -> None:
        """Forget a remembered reference the driver has confirmed is gone."""
        self._set_history(
            driver_id, [r for r in self.history_for(driver_id) if r != reference]
        )

    def reference_for(self, driver_id: str) -> str | None:
        reference = self.resume_ids.get(driver_id)
        return reference if isinstance(reference, str) and reference else None

    def mark_active(self, driver_id: str | None) -> None:
        if driver_id:
            self.agents["active"] = driver_id

    def as_legacy(self, active_driver: str) -> dict[str, Any]:
        return {
            "active": self.agents.get("active") or active_driver,
            "parked": self.agents.get("parked") or [],
            "resume_ids": {
                str(key): value
                for key, value in self.resume_ids.items()
                if isinstance(value, str) and value
            },
            "resume_history": {
                str(key): [r for r in value if isinstance(r, str) and r]
                for key, value in self.resume_history.items()
                if isinstance(value, list)
            },
        }
