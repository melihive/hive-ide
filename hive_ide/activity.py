"""Fail-open activity markers for processes running inside a session."""

from __future__ import annotations

import os

from .store import StateStore, utc_now


class IdeActivity:
    KIND_TASK = "task"
    STATE_RUNNING = "running"
    STATE_BLOCKED = "blocked"

    @staticmethod
    def _target() -> tuple[StateStore, str] | None:
        state_home = os.environ.get("HIVE_IDE_STATE_HOME")
        workspace = os.environ.get("HIVE_IDE_WORKSPACE_KEY")
        session_id = os.environ.get("HIVE_IDE_SESSION_ID")
        if not state_home or not workspace or not session_id:
            return None
        return StateStore(state_home, workspace), session_id

    @staticmethod
    def mark(kind: str, *, label: str = "", state: str = "running") -> bool:
        try:
            target = IdeActivity._target()
            if target is None:
                return False
            store, session_id = target
            with store.mutation_lock():
                if store.find_session(session_id) is None:
                    return False
                store.write(
                    "activity",
                    session_id,
                    {
                        "schema_version": 1,
                        "session_id": session_id,
                        "workspace_key": store.workspace_key,
                        "kind": kind,
                        "state": state,
                        "label": label,
                        "observed_at": utc_now(),
                    },
                )
            return True
        except Exception:
            return False

    @staticmethod
    def blocked(kind: str, *, label: str = "") -> bool:
        return IdeActivity.mark(kind, label=label, state=IdeActivity.STATE_BLOCKED)

    @staticmethod
    def clear() -> bool:
        try:
            target = IdeActivity._target()
            if target is None:
                return False
            store, session_id = target
            with store.mutation_lock():
                return store.delete("activity", session_id)
        except Exception:
            return False
