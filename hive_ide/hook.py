#!/usr/bin/env python3
"""Agent hook receiver for sidebar status and activity.

Installed into the agent CLIs' own hook config by `ide setup` and invoked by them
on turn boundaries:

    python3 -m hive_ide.hook --state-home <state-home> --state <state> --driver <agent>

Payload: claude sends its hook JSON on **stdin**; codex's `notify` passes it as a
single **argv** JSON string. Both carry `cwd` and a session/thread id.

The join is by **immutable session id**, not cwd or display name. The frame exports
`HIVE_IDE_WORKSPACE_KEY` / `HIVE_IDE_SESSION_ID` into the agent pane, and hooks
inherit the agent's environment, so an agent launched by the ide always identifies
the exact record.

Two writes per turn: the sidebar's status file (the dot), and the matching ide
session's `last_active` + `agents.resume_ids[<agent>]`. The id arrives here
first-hand, so `open` can resume the exact conversation instead of guessing the
newest one for the cwd; `last_active` is stamped here because a turn IS the
activity the sidebar's relative time and its activity sort both claim to show.
An agent running outside an ide session matches nothing and writes only the
status file.

Stdlib only, fail-open, and fast: a hook must never slow down or break the agent,
so every error is swallowed and we always exit 0.
"""
from __future__ import annotations

import json
import argparse
import os
import shlex
import subprocess
import sys

from .config import configured_registry, load_config
from .paths import config_path
from .python_cmd import PythonCommand
from .store import StateStore, utc_now


class IdeHook:
    """Parse an agent hook payload and stamp the ide session's state."""

    STATES = ("working", "waiting", "idle")
    ACTIVITIES = ("compacting", "clear")
    # Exported into the agent pane by `_build_window` (tmux `-e`). Hooks inherit the
    # agent's environment, so these arrive for free — no payload field needed.
    ENV_WORKSPACE = "HIVE_IDE_WORKSPACE_KEY"
    ENV_SESSION_ID = "HIVE_IDE_SESSION_ID"
    ENV_STATE_HOME = "HIVE_IDE_STATE_HOME"
    ENV_TMUX_SOCKET = "HIVE_IDE_TMUX_SOCKET"
    ENV_CONFIG = "HIVE_IDE_CONFIG"

    @staticmethod
    def _payload(args: list[str]) -> dict:
        """codex `notify` passes JSON as argv; claude sends it on stdin."""
        for raw in args:
            if raw.strip().startswith("{"):
                try:
                    return json.loads(raw)
                except ValueError:
                    pass
        if not sys.stdin.isatty():
            try:
                return json.loads(sys.stdin.read() or "{}")
            except ValueError:
                return {}
        return {}

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        args = argv if argv is not None else sys.argv[1:]
        if "--state-home" in args:
            return IdeHook._protocol_main(args)
        return 0  # fail-open for obsolete invocations

    @staticmethod
    def _protocol_main(args: list[str]) -> int:
        """Protocol-v1 receiver. Hooks are machine-global and always fail open."""
        try:
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--state-home", required=True)
            action = parser.add_mutually_exclusive_group(required=True)
            action.add_argument("--state", choices=IdeHook.STATES)
            action.add_argument("--activity", choices=IdeHook.ACTIVITIES)
            action.add_argument("--subagent", choices=("start", "stop"))
            parser.add_argument("--driver", required=True)
            parser.add_argument("--relayed", action="store_true")
            parser.add_argument("payload", nargs="?")
            parsed = parser.parse_args(args)
            workspace = os.environ.get(IdeHook.ENV_WORKSPACE)
            session_id = os.environ.get(IdeHook.ENV_SESSION_ID)
            if not workspace or not session_id:
                return 0
            payload = {}
            if not parsed.activity:
                payload_args = [parsed.payload] if parsed.payload else []
                payload = IdeHook._payload(payload_args)
            if not parsed.relayed and os.environ.get(IdeHook.ENV_TMUX_SOCKET):
                if IdeHook._relay(parsed, workspace, session_id, payload):
                    return 0
            store = StateStore(parsed.state_home, workspace)
            with store.mutation_lock():
                record = store.find_session(session_id)
                if record is None:
                    return 0
                if parsed.activity:
                    if parsed.activity == "clear":
                        store.delete("activity", session_id)
                    else:
                        store.write(
                            "activity",
                            session_id,
                            {
                                "schema_version": 1,
                                "session_id": session_id,
                                "workspace_key": store.workspace_key,
                                "kind": parsed.activity,
                                "state": "running",
                                "label": "Compacting context",
                                "observed_at": utc_now(),
                            },
                    )
                    return 0
                if parsed.subagent:
                    IdeHook._write_subagent_status(
                        store,
                        session_id,
                        parsed.driver,
                        parsed.subagent,
                        payload,
                    )
                    return 0

            registry = configured_registry(load_config(config_path()), plugins=True)
            driver = registry.get(parsed.driver)
            event = driver.translate_status(payload, parsed.state)
            if event is None:
                return 0
            reference = event.get("conversation_reference")
            status = {
                "schema_version": 1,
                "session_id": session_id,
                "workspace_key": store.workspace_key,
                "state": event["state"],
                "driver": parsed.driver,
                "conversation_reference": reference,
                "observed_at": utc_now(),
            }
            subagents_running = IdeHook._subagents_running(payload)
            if subagents_running is not None:
                status["subagents"] = {"running": subagents_running}
                status["subagents_running"] = subagents_running
            with store.mutation_lock():
                record = store.find_session(session_id)
                if record is None:
                    return 0
                store.write("status", session_id, status)
                record["last_active"] = status["observed_at"]
                current_driver = record.get("driver") or {}
                current_reference = (current_driver.get("resume") or {}).get("reference")
                if (
                    reference
                    and current_driver.get("id") == parsed.driver
                    and (not current_reference or current_reference == reference)
                ):
                    record["driver"] = driver.resolve(
                        name=record["name"],
                        working_dir=record["working_dir"],
                        conversation_reference=reference,
                    )
                store.write("sessions", session_id, record)
        except BaseException:
            pass
        return 0

    @staticmethod
    def _write_subagent_status(
        store: StateStore,
        session_id: str,
        driver: str,
        action: str,
        payload: dict,
    ) -> None:
        agent_id = payload.get("agent_id")
        if not isinstance(agent_id, str) or not agent_id:
            return
        status = store.read("status", session_id) or {}
        ids = (
            (status.get("subagents") or {}).get("ids")
            if isinstance(status.get("subagents"), dict)
            else []
        )
        active = {value for value in ids if isinstance(value, str) and value}
        if action == "start":
            active.add(agent_id)
        else:
            active.discard(agent_id)
        observed_at = utc_now()
        document = {
            **status,
            "schema_version": 1,
            "session_id": session_id,
            "workspace_key": store.workspace_key,
            "state": status.get("state") or ("working" if active else "waiting"),
            "driver": driver,
            "observed_at": observed_at,
            "subagents": {"running": len(active), "ids": sorted(active)},
            "subagents_running": len(active),
        }
        store.write("status", session_id, document)
        record = store.find_session(session_id)
        if record is not None:
            record["last_active"] = observed_at
            store.write("sessions", session_id, record)

    @staticmethod
    def _subagents_running(payload: dict) -> int | None:
        """Best-effort generic extraction for host/agent hook payloads.

        The package stays host-neutral: it does not know how a given agent names its
        internal workers. Hooks that can observe them may send either
        `subagents_running` or `subagents.running`, and the sidebar consumes the
        normalized status field.
        """
        candidates: list[object] = [payload.get("subagents_running")]
        subagents = payload.get("subagents")
        if isinstance(subagents, dict):
            candidates.append(subagents.get("running"))
        for value in candidates:
            if isinstance(value, bool):
                continue
            if isinstance(value, int):
                return max(0, value)
            if isinstance(value, str) and value.isdigit():
                return max(0, int(value))
        return None

    @staticmethod
    def _relay(
        parsed: argparse.Namespace,
        workspace: str,
        session_id: str,
        payload: dict,
    ) -> bool:
        """Ask the IDE tmux server to perform the write outside the agent sandbox."""
        socket = os.environ.get(IdeHook.ENV_TMUX_SOCKET)
        if not socket:
            return False
        action = (
            ["--subagent", parsed.subagent]
            if parsed.subagent
            else (
                ["--activity", parsed.activity]
                if parsed.activity
                else ["--state", parsed.state]
            )
        )
        env = [
            f"{IdeHook.ENV_WORKSPACE}={workspace}",
            f"{IdeHook.ENV_SESSION_ID}={session_id}",
            f"{IdeHook.ENV_STATE_HOME}={parsed.state_home}",
        ]
        if config := os.environ.get(IdeHook.ENV_CONFIG):
            env.append(f"{IdeHook.ENV_CONFIG}={config}")
        command = shlex.join(
            [
                "env",
                *env,
                *PythonCommand.module_argv(
                    "hook",
                    [
                        "--state-home",
                        parsed.state_home,
                        *action,
                        "--driver",
                        parsed.driver,
                        "--relayed",
                        json.dumps(payload, separators=(",", ":")),
                    ],
                    python=sys.executable,
                ),
            ]
        )
        try:
            result = subprocess.run(
                ["tmux", "-L", socket, "run-shell", "-b", command],
                capture_output=True,
                text=True,
            )
        except BaseException:
            return False
        return result.returncode == 0


if __name__ == "__main__":
    raise SystemExit(IdeHook.main())
