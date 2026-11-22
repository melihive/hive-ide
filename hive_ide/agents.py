"""Session agent state helpers."""

from __future__ import annotations

from typing import Any


class AgentResumeState:
    """Maintain per-driver resume references on a session record."""

    def __init__(self, record: dict[str, Any]):
        self.record = record
        agents = record.get("agents")
        if not isinstance(agents, dict):
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
        self.seed_current_driver()

    @staticmethod
    def reference_from_driver(driver: dict[str, Any]) -> str | None:
        reference = (driver.get("resume") or {}).get("reference")
        return reference if isinstance(reference, str) and reference else None

    def seed_current_driver(self) -> None:
        """Take the launched conversation into the driver's references.

        Usually `resume_ids` already names it. When it names a DIFFERENT one the
        record is internally inconsistent — which older versions of this package
        could write, by moving `resume_ids` without re-rendering the launch — and
        the launched reference is then the one that is actually in use. It is
        remembered rather than ignored, so an event that confirms the other one
        cannot quietly make it the last trace of a real conversation.
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
