#!/usr/bin/env python3
"""Agent hook receiver for sidebar status and activity.

Installed into the agent CLIs' own hook config by `ide setup` and invoked by them
on turn boundaries:

    python3 -m hive_ide.hook --state-home <state-home> --state <state> --driver <agent>

Payload: claude sends its hook JSON on **stdin**; codex's `notify` passes it as a
single **argv** JSON string. Both carry `cwd` and a session/thread id.

The join is by **immutable session id**, not cwd or display name. The frame exports
`HIVE_IDE_WORKSPACE_KEY` / `HIVE_IDE_SESSION_ID` into the agent pane, and hooks
inherit the agent's environment.

Inheriting is the weakness: an environment can be carried by a process that is not
the pane it came from. Codex runs every TUI's commands and hooks as children of one
shared `app-server --managed-daemon`, which keeps the environment of whichever pane
first started it, so every Codex session's events can arrive wearing that one pane's
identity. The **conversation reference** does not have that problem — it is carried
per event and minted by the agent — so it outranks the environment: an event for a
conversation some session already owns is routed to that owner, in whatever
workspace it lives, and a new conversation whose recorded origin lies outside the
named session's workspace is refused rather than claimed.

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
from pathlib import Path
from typing import Any

from .agents import AgentResumeState
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
            except (OSError, ValueError):
                return {}
        return {}

    @staticmethod
    def _tmux_server_matches_marker(socket: str) -> bool:
        """Is this process attached to the tmux server named by the IDE marker?

        `$TMUX` is `<socket-path>,<pid>,<session>`. A pane id like `%5` is only
        unique WITHIN one tmux server, and every IDE workspace runs its own server
        (`hive-ide-next-<hash>`). So a `TMUX_PANE` inherited from a different
        server's pane names a real-but-unrelated window here, and resolving it
        would attribute the event to another workspace's session. Treat a pane id
        as addressable only when the server it came from is the marker's server.
        """
        tmux = os.environ.get("TMUX")
        if not tmux:
            return False
        socket_path = tmux.split(",", 1)[0]
        return bool(socket_path) and os.path.basename(socket_path) == socket

    @staticmethod
    def _ide_context_from_environment(*, relayed: bool = False) -> tuple[str | None, str | None]:
        """Resolve the IDE workspace/session for a hook event.

        New agent panes inherit explicit `HIVE_IDE_*` variables from the frame, but
        tmux also has a session-level environment that can stay pinned to the first
        window created in that server. When the IDE tmux socket marker is present
        AND the pane id is addressable on that server, the tagged tmux window is the
        authority. Otherwise, explicit environment values are trusted so tests,
        relayed writes, and one-off invocations running under an unrelated outer
        tmux pane are not misdirected. This keeps `/clear` and restarted-in-place
        chats attached to the visible IDE session without rebuilding the pane.

        A relayed event never rediscovers: `_relay` already resolved identity on the
        originating hop and passes it explicitly. The relayed hop runs under
        `tmux run-shell` on the IDE server, where the server's own environment can
        carry an unrelated `TMUX_PANE`; rediscovering there lets that stale pane
        override the identity the relay was told to write.
        """
        workspace = os.environ.get(IdeHook.ENV_WORKSPACE)
        session_id = os.environ.get(IdeHook.ENV_SESSION_ID)
        if relayed:
            return workspace, session_id
        pane = os.environ.get("TMUX_PANE")
        socket = os.environ.get(IdeHook.ENV_TMUX_SOCKET)
        if workspace and session_id and not socket:
            return workspace, session_id
        if pane and (not socket or IdeHook._tmux_server_matches_marker(socket)):
            try:
                result = subprocess.run(
                    [
                        "tmux",
                        *(["-L", socket] if socket else []),
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
            workspace, session_id = IdeHook._ide_context_from_environment(
                relayed=parsed.relayed
            )
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
            store, session_id, refused = IdeHook._retarget_by_conversation(
                store, parsed, session_id, reference, driver
            )
            if refused:
                return 0
            status["session_id"] = session_id
            status["workspace_key"] = store.workspace_key
            with store.mutation_lock():
                record = store.find_session(session_id)
                if record is None:
                    return 0
                store.write("status", session_id, status)
                record["last_active"] = status["observed_at"]
                current_driver = record.get("driver") or {}
                agents = AgentResumeState(record)
                current_driver_matches = current_driver.get("id") == parsed.driver
                owner = (
                    store.find_conversation_owner(
                        driver_id=parsed.driver,
                        reference=reference,
                        exclude_session_id=session_id,
                    )
                    if reference
                    else None
                )
                reference_available = owner is None
                if reference and reference_available:
                    agents.remember(parsed.driver, reference)
                if (
                    reference
                    and reference_available
                    and current_driver_matches
                ):
                    agents.mark_active(parsed.driver)
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
    def _retarget_by_conversation(
        store: StateStore,
        parsed: argparse.Namespace,
        session_id: str,
        reference: str | None,
        driver: Any,
    ) -> tuple[StateStore, str, bool]:
        """Trust the conversation over the environment it arrived in.

        A hook's `HIVE_IDE_*` identity can be INHERITED rather than observed. Codex
        runs every TUI's commands and hooks as children of one shared
        `codex app-server --managed-daemon`, which keeps the environment of
        whichever pane first started it — so every Codex session's events arrive
        wearing that one pane's identity, in that one pane's workspace.

        A conversation reference does not have that problem: it is carried per
        event and minted by the agent itself. So:

        - If some session already owns this conversation, that session IS the
          target, in whatever workspace it lives. An inherited identity cannot
          steal an established conversation or divert its activity.
        - If nobody owns it, the conversation is new, and only its recorded origin
          can place it. When the agent records one and it falls outside the
          candidate session's workspace, refuse: a skipped event is recoverable,
          a conversation claimed by the wrong session is not.
        - When the origin is unknown, leave the event alone. Unknown is not a
          verdict, and the drivers that record nothing never had this problem.

        Returns the store and session to write through, plus whether to refuse.
        """
        if not reference:
            return store, session_id, False
        owner = store.find_conversation_owner(
            driver_id=parsed.driver, reference=reference, exclude_session_id=None
        )
        if owner is not None:
            if owner.get("id") == session_id:
                return store, session_id, False
            owner_workspace = owner.get("workspace_key")
            if not isinstance(owner_workspace, str) or not owner_workspace:
                return store, session_id, False
            if owner_workspace != store.workspace_key:
                store = StateStore(parsed.state_home, owner_workspace)
            return store, str(owner["id"]), False
        probe = getattr(driver, "conversation_origin", None)
        origin = probe(reference) if callable(probe) else None
        if not origin:
            return store, session_id, False
        record = store.find_session(session_id)
        if record is None:
            return store, session_id, False
        workspace = Path(store.workspace_key).resolve()
        try:
            Path(origin).resolve().relative_to(workspace)
        except ValueError:
            return store, session_id, True
        return store, session_id, False

    @staticmethod
    def _write_subagent_status(
        store: StateStore,
        session_id: str,
        driver: str,
        action: str,
        payload: dict,
    ) -> None:
        status = store.read("status", session_id) or {}
        subagents = status.get("subagents") if isinstance(status.get("subagents"), dict) else {}
        ids = subagents.get("ids") or []
        active = {value for value in ids if isinstance(value, str) and value}
        anonymous = subagents.get("anonymous_running")
        anonymous_running = anonymous if isinstance(anonymous, int) and anonymous > 0 else 0
        agent_id = payload.get("agent_id")
        if action == "start":
            if isinstance(agent_id, str) and agent_id:
                active.add(agent_id)
            else:
                anonymous_running += 1
        else:
            if isinstance(agent_id, str) and agent_id:
                active.discard(agent_id)
            else:
                anonymous_running = max(0, anonymous_running - 1)
        running = len(active) + anonymous_running
        observed_at = utc_now()
        subagent_status: dict[str, object] = {
            "running": running,
            "ids": sorted(active),
        }
        if anonymous_running:
            subagent_status["anonymous_running"] = anonymous_running
        document = {
            **status,
            "schema_version": 1,
            "session_id": session_id,
            "workspace_key": store.workspace_key,
            "state": status.get("state") or ("working" if running else "waiting"),
            "driver": driver,
            "observed_at": observed_at,
            "subagents": subagent_status,
            "subagents_running": running,
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
