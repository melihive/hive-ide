"""Session health checks that do not mutate IDE state."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .frame import Frame
from .store import StateStore


class SessionHealth:
    """Read-only health checks for one IDE session."""

    def __init__(self, store: StateStore, frame: Frame):
        self.store = store
        self.frame = frame

    def hook_warnings(self, record: dict[str, Any]) -> list[str]:
        """Return hook/status warnings for live agent panes.

        The sidebar's ordering is driven by real hook/status writes. Repair,
        tmux focus, redraw, and mouse clicks must not be promoted to activity,
        so this method only diagnoses hook visibility; it never stamps
        ``last_active`` or reads agent transcripts.
        """
        driver = record.get("driver") or {}
        capabilities = set(driver.get("capabilities") or [])
        if "status" not in capabilities:
            return []

        session_id = str(record["id"])
        window = self.frame.windows().get(session_id)
        if not window:
            return []

        roles = self.frame.role_panes(session_id)
        if "agent" not in roles:
            return []

        status = self.store.read("status", session_id)
        if not status:
            return [
                "status hooks have not reported for this live agent pane; "
                "visible chat activity may not update sidebar status or order "
                "until the agent pane is restarted with the current hook environment"
            ]

        warnings: list[str] = []
        if status.get("session_id") != session_id:
            warnings.append("status hook wrote a mismatched session_id")
        driver_id = driver.get("id")
        if driver_id and status.get("driver") and status.get("driver") != driver_id:
            warnings.append(
                f"status hook driver mismatch: record={driver_id}, "
                f"status={status.get('driver')}"
            )
        if not status.get("observed_at"):
            warnings.append("status hook did not provide observed_at")
        elif self._is_before(status.get("observed_at"), record.get("last_active")):
            warnings.append(
                "status hook observed_at is older than session last_active; "
                "activity ordering may be stale until the agent emits a fresh status event"
            )
        resume = driver.get("resume") or {}
        if resume.get("reference") and not status.get("conversation_reference"):
            warnings.append(
                "status hook did not report the remembered conversation reference; "
                "resume still uses the session record, but status diagnostics are partial"
            )
        return warnings

    def _is_before(self, first: Any, second: Any) -> bool:
        first_dt = self._parse_timestamp(first)
        second_dt = self._parse_timestamp(second)
        if first_dt is None or second_dt is None:
            return False
        return first_dt < second_dt

    def _parse_timestamp(self, value: Any) -> datetime | None:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
