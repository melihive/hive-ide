"""Legacy renderer API backed by the protocol-v1 store.

The copied tmux UI is intentionally migrated behind this narrow facade. It keeps
rendering behavior stable while state identity and paths move to the public protocol.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import SCHEMA_VERSION
from .agents import AgentResumeState
from .store import StateStore, utc_now


class StateIO:
    NAME_MAX = 14
    NAME_RE = re.compile(r"[A-Z0-9][A-Z0-9 ]{0,13}")

    @staticmethod
    def _store(state_home: Path, workspace_key: str) -> StateStore:
        return StateStore(state_home, workspace_key)

    @staticmethod
    def valid_name(name: str) -> bool:
        return bool(name) and StateIO.NAME_RE.fullmatch(name) is not None

    @staticmethod
    def list_sessions(state_home: Path, workspace_key: str) -> list[dict]:
        return [
            StateIO._legacy(record)
            for record in StateIO._store(state_home, workspace_key).list("sessions")
        ]

    @staticmethod
    def list_archived(state_home: Path, workspace_key: str) -> list[dict]:
        return [
            StateIO._legacy(record)
            for record in StateIO._store(state_home, workspace_key).list("archive")
        ]

    @staticmethod
    def read_config_snapshot(state_home: Path, workspace_key: str) -> dict | None:
        store = StateIO._store(state_home, workspace_key)
        return store.read_path(store.config_snapshot_path())

    @staticmethod
    def read(state_home: Path, workspace_key: str, name: str) -> dict | None:
        record = StateIO._store(state_home, workspace_key).find_by_name(name)
        return StateIO._legacy(record) if record else None

    @staticmethod
    def find_by_id(
        state_home: Path, workspace_key: str, session_id: str
    ) -> tuple[str, str, dict] | None:
        record = StateIO._store(state_home, workspace_key).find_session(session_id)
        if record is None:
            return None
        legacy = StateIO._legacy(record)
        return workspace_key, legacy["name"], legacy

    @staticmethod
    def find_by_identity(
        state_home: Path, workspace_key: str, name: str
    ) -> tuple[str, str, dict] | None:
        record = StateIO.read(state_home, workspace_key, name)
        return (workspace_key, name, record) if record else None

    @staticmethod
    def read_session_status(state_home: Path, record: dict) -> dict | None:
        key = record.get("workspace_key") or record.get("repo")
        if not key or not record.get("id"):
            return None
        status = StateIO._store(state_home, key).read("status", record["id"])
        if status is None:
            return None
        return {
            **status,
            "agent": status.get("driver"),
            "session_id": status.get("conversation_reference"),
            "ts": status.get("observed_at"),
        }

    @staticmethod
    def write_session_status_update(
        state_home: Path, record: dict, update: dict
    ) -> None:
        key = record.get("workspace_key") or record.get("repo")
        session_id = record.get("id")
        if not key or not session_id:
            return
        store = StateIO._store(state_home, key)
        current = store.read("status", session_id) or {
            "schema_version": SCHEMA_VERSION,
            "workspace_key": store.workspace_key,
            "session_id": session_id,
        }
        current.update(update)
        current.setdefault("driver", (record.get("driver") or {}).get("id"))
        current["observed_at"] = utc_now()
        store.write("status", session_id, current)

    @staticmethod
    def read_session_activity(state_home: Path, record: dict) -> dict | None:
        key = record.get("workspace_key") or record.get("repo")
        session_id = record.get("id")
        if not key or not session_id:
            return None
        activity = StateIO._store(state_home, key).read("activity", session_id)
        if activity is None:
            return None
        return {**activity, "ts": activity.get("observed_at")}

    @staticmethod
    def read_error(state_home: Path, record: dict) -> dict | None:
        key = record.get("workspace_key") or record.get("repo")
        if not key or not record.get("id"):
            return None
        return StateIO._store(state_home, key).read("errors", record["id"])

    @staticmethod
    def _legacy(record: dict) -> dict:
        out = dict(record)
        driver = dict(out.get("driver") or {})
        driver_id = driver.get("id") or "term"
        reference = (driver.get("resume") or {}).get("reference")
        out["repo"] = out.get("workspace_key")
        out["cwd"] = out.get("working_dir")
        plan = out.get("plan")
        out["plan"] = plan.get("path") if isinstance(plan, dict) else plan
        agents = AgentResumeState(out)
        agents.remember(driver_id, reference)
        agents.mark_active(driver_id)
        out["agents"] = agents.as_legacy(driver_id)
        return out
