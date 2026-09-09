"""Fail-open activity markers for processes running inside a session."""

from __future__ import annotations

import os
import subprocess

from .store import StateStore, utc_now


class IdeActivity:
    KIND_TASK = "task"
    KIND_RELEASE = "release"
    STATE_RUNNING = "running"
    STATE_BLOCKED = "blocked"
    ENV_WORKSPACE = "HIVE_IDE_WORKSPACE_KEY"
    ENV_SESSION_ID = "HIVE_IDE_SESSION_ID"
    ENV_STATE_HOME = "HIVE_IDE_STATE_HOME"
    ENV_TMUX_SOCKET = "HIVE_IDE_TMUX_SOCKET"

    @staticmethod
    def _ide_context_from_environment() -> tuple[str | None, str | None]:
        workspace = os.environ.get(IdeActivity.ENV_WORKSPACE)
        session_id = os.environ.get(IdeActivity.ENV_SESSION_ID)
        pane = os.environ.get("TMUX_PANE")
        if os.environ.get(IdeActivity.ENV_TMUX_SOCKET) and pane:
            try:
                result = subprocess.run(
                    [
                        "tmux",
                        "display-message",
                        "-p",
                        "-t",
                        pane,
                        "#{@hive_ide_workspace_key}\t#{@hive_ide_session_id}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1.0,
                )
            except (OSError, subprocess.SubprocessError):
                result = None
            if result is not None and result.returncode == 0:
                observed_workspace, _, observed_session_id = (
                    result.stdout.strip().partition("\t")
                )
                if observed_workspace and observed_session_id:
                    return observed_workspace, observed_session_id
                workspace = workspace or observed_workspace or None
                session_id = session_id or observed_session_id or None
        return workspace, session_id

    @staticmethod
    def _target() -> tuple[StateStore, str] | None:
        state_home = os.environ.get(IdeActivity.ENV_STATE_HOME)
        workspace, session_id = IdeActivity._ide_context_from_environment()
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
