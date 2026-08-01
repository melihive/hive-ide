"""Driver handoff payload construction."""

from __future__ import annotations

from typing import Any


class HandoffPackage:
    """Build the structured context passed to a newly selected driver."""

    def __init__(
        self,
        record: dict[str, Any],
        *,
        created_at: str,
        previous_driver: str | None,
        target_driver: str,
        previous_reference: str | None,
        target_reference: str | None,
    ):
        self.record = record
        self.created_at = created_at
        self.previous_driver = previous_driver
        self.target_driver = target_driver
        self.previous_reference = previous_reference
        self.target_reference = target_reference

    def build(self) -> dict[str, Any]:
        plan = self.record.get("plan") if isinstance(self.record.get("plan"), dict) else {}
        payload = {
            "created_at": self.created_at,
            "session_id": self.record["id"],
            "session_name": self.record["name"],
            "from_driver": self.previous_driver,
            "to_driver": self.target_driver,
            "previous_resume_reference": self.previous_reference,
            "target_resume_reference": self.target_reference,
            "working_dir": self.record.get("working_dir"),
            "plan": plan.get("path"),
            "active_task": plan.get("active_task"),
        }
        payload["target_driver_prompt"] = self._target_driver_prompt(payload)
        return payload

    @staticmethod
    def _target_driver_prompt(payload: dict[str, Any]) -> str:
        parts = [
            "You are now the active driver for this hive-ide session.",
            f"Session: {payload.get('session_name') or payload.get('session_id')}.",
        ]
        if payload.get("working_dir"):
            parts.append(f"Working directory: {payload['working_dir']}.")
        if payload.get("plan"):
            plan = str(payload["plan"])
            if payload.get("active_task"):
                parts.append(f"Plan: {plan}; active task: {payload['active_task']}.")
            else:
                parts.append(f"Plan: {plan}.")
        if payload.get("previous_resume_reference"):
            parts.append(
                "Previous driver conversation reference: "
                f"{payload['previous_resume_reference']}."
            )
        parts.append(
            "Use this metadata to produce a brief handoff summary when useful, "
            "then continue the current task."
        )
        return " ".join(parts)
