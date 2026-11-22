"""Reconcile a session's driver conversation references with each driver's own store."""

from __future__ import annotations

from typing import Any

from .agents import AgentResumeState
from .drivers import ConversationState, ConversationStatus, DriverRegistry
from .errors import HiveIdeError
from .store import StateStore


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
                was_active = self._is_active_reference(record, driver_id, reference)
                agents.forget(driver_id)
                changed = True
                replacement = self._previous_that_survives(
                    agents, driver_id, working_dir
                )
                if replacement:
                    if was_active:
                        self._resolve_reference(record, driver_id, replacement)
                    actions.append(
                        f"driver: {driver_id} conversation {short} no longer exists; "
                        f"fell back to {replacement[:8]}, which still does"
                    )
                else:
                    if was_active:
                        self._resolve_fresh(record, driver_id)
                    actions.append(
                        f"driver: dropped {driver_id} conversation {short}, which no longer "
                        "exists; the next launch starts a fresh conversation"
                    )
            elif status.state == ConversationState.ARCHIVED:
                warnings.append(
                    f"{driver_id} conversation {short} is archived; {status.detail}"
                )
        if changed and apply:
            self.store.write("sessions", record["id"], record)
        return {"changed": changed, "actions": actions, "warnings": warnings}

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

    @staticmethod
    def _is_active_reference(record: dict[str, Any], driver_id: str, reference: str) -> bool:
        driver = record.get("driver") or {}
        return (
            driver.get("id") == driver_id
            and AgentResumeState.reference_from_driver(driver) == reference
        )

    def _previous_that_survives(
        self, agents: AgentResumeState, driver_id: str, working_dir: str
    ) -> str | None:
        """The newest displaced reference the driver can still prove exists.

        A session whose current conversation is gone usually still has the one
        before it, and resuming that is strictly better than starting empty: on
        2026-11-22 a DJ DEV repair reset the driver to a bare
        `claude --name "DJ DEV"` while 69 MB of history sat one reference back,
        and only a hand-edit saved it.

        Walks newest first and takes the first PROVEN survivor. A candidate the
        driver confirms is gone is pruned — it cannot come back — while one it
        cannot speak for is left in history and skipped, because an unprovable
        reference is exactly what makes a pane exit on `--resume`.
        """
        for candidate in agents.history_for(driver_id):
            state = self.status(driver_id, candidate, working_dir).state
            if state == ConversationState.EXISTS:
                agents.adopt(driver_id, candidate)
                return candidate
            if state == ConversationState.GONE:
                agents.drop_from_history(driver_id, candidate)
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
