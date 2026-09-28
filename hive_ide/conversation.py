"""Reconcile a session's driver conversation references with each driver's own store."""

from __future__ import annotations

from typing import Any

from .agents import AgentResumeState
from .drivers import ConversationState, ConversationStatus, DriverRegistry
from .errors import HiveIdeError
from .store import StateStore


class ConversationGuard:
    """Act only on answers the driver can prove; leave everything else alone.

    - gone: forget the reference so the next launch starts a fresh conversation
      instead of a pane that exits on `--resume`.
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
                agents.forget(driver_id)
                if self._is_active_reference(record, driver_id, reference):
                    self._resolve_fresh(record, driver_id)
                changed = True
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
