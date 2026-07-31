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
        self.seed_current_driver()

    @staticmethod
    def reference_from_driver(driver: dict[str, Any]) -> str | None:
        reference = (driver.get("resume") or {}).get("reference")
        return reference if isinstance(reference, str) and reference else None

    def seed_current_driver(self) -> None:
        driver = self.record.get("driver")
        if not isinstance(driver, dict):
            return
        driver_id = driver.get("id")
        reference = self.reference_from_driver(driver)
        if isinstance(driver_id, str) and driver_id and reference:
            self.resume_ids.setdefault(driver_id, reference)

    def remember(self, driver_id: str | None, reference: str | None) -> None:
        if not driver_id:
            return
        if reference:
            self.resume_ids[driver_id] = reference

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
        }
