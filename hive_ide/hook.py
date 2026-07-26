#!/usr/bin/env python3
"""Agent hook receiver for sidebar status and activity.

Installed into the agent CLIs' own hook config by `ide setup` and invoked by them
on turn boundaries:

    python3 -I .skills/_lib/ide_hook.py <skill_dir> <state> <agent>

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
import sys

from .config import configured_registry, load_config
from .paths import config_path
from .store import StateStore, utc_now


class IdeHook:
    """Parse an agent hook payload and stamp the ide session's state."""

    STATES = ("working", "waiting", "idle")
    ACTIVITIES = ("compacting", "clear")
    # Exported into the agent pane by `_build_window` (tmux `-e`). Hooks inherit the
    # agent's environment, so these arrive for free — no payload field needed.
    ENV_WORKSPACE = "HIVE_IDE_WORKSPACE_KEY"
    ENV_SESSION_ID = "HIVE_IDE_SESSION_ID"

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
            parser.add_argument("--driver", required=True)
            parser.add_argument("payload", nargs="?")
            parsed = parser.parse_args(args)
            workspace = os.environ.get("HIVE_IDE_WORKSPACE_KEY")
            session_id = os.environ.get("HIVE_IDE_SESSION_ID")
            if not workspace or not session_id:
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

            payload_args = [parsed.payload] if parsed.payload else []
            payload = IdeHook._payload(payload_args)
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
            with store.mutation_lock():
                record = store.find_session(session_id)
                if record is None:
                    return 0
                store.write("status", session_id, status)
                record["last_active"] = status["observed_at"]
                if reference and (record.get("driver") or {}).get("id") == parsed.driver:
                    record["driver"] = driver.resolve(
                        name=record["name"],
                        working_dir=record["working_dir"],
                        conversation_reference=reference,
                    )
                store.write("sessions", session_id, record)
        except BaseException:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(IdeHook.main())
