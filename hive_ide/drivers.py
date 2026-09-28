"""Bundled and extensible agent-driver registry."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol

from .errors import UsageError


@dataclass(frozen=True)
class DriverAvailability:
    available: bool
    executable: str | None
    detail: str = ""


class ConversationState:
    """Where a driver conversation stands, as seen from one working dir."""

    EXISTS = "exists"  # resumable from the working dir
    GONE = "gone"  # the driver's own store confirms it no longer exists
    UNKNOWN = "unknown"  # no answer; never evidence of absence
    ARCHIVED = "archived"  # exists, but the driver refuses to resume it as-is


@dataclass(frozen=True)
class ConversationStatus:
    state: str
    detail: str = ""

    def exists(self) -> bool | None:
        if self.state == ConversationState.EXISTS:
            return True
        if self.state == ConversationState.GONE:
            return False
        return None


class AgentDriver(Protocol):
    id: str
    label: str

    def detect(self) -> DriverAvailability: ...

    def resolve(
        self, *, name: str, working_dir: str, conversation_reference: str | None
    ) -> dict[str, Any]: ...

    def conversation_exists(self, reference: str, working_dir: str) -> bool | None: ...

    def translate_status(
        self, payload: dict[str, Any], requested_state: str
    ) -> dict[str, Any] | None: ...


class CommandDriver:
    def __init__(
        self,
        driver_id: str,
        label: str,
        command: list[str],
        *,
        resume_strategy: str = "none",
        resume_argv: list[str] | None = None,
        resume_cwd_flag: str | None = None,
        name_argv: list[str] | None = None,
        capabilities: tuple[str, ...] = ("launch",),
        conversation_probe: str | None = None,
    ):
        self.id = driver_id
        self.label = label
        self.command = command
        self.resume_strategy = resume_strategy
        self.resume_argv = resume_argv or []
        self.resume_cwd_flag = resume_cwd_flag
        self.name_argv = name_argv or []
        self.capabilities = capabilities
        self.conversation_probe = conversation_probe

    def detect(self) -> DriverAvailability:
        executable = shutil.which(self.command[0])
        return DriverAvailability(bool(executable), executable, "" if executable else "not on PATH")

    def resolve(
        self, *, name: str, working_dir: str, conversation_reference: str | None
    ) -> dict[str, Any]:
        launch = list(self.command)
        launch.extend(self._name_argv(name))
        if conversation_reference and self.resume_strategy == "conversation_id":
            launch = list(self.resume_argv)
            if self.resume_cwd_flag:
                launch.extend([self.resume_cwd_flag, working_dir])
            launch.append(conversation_reference)
            launch.extend(self._name_argv(name))
        elif conversation_reference and self.resume_strategy == "workspace_continue":
            launch = list(self.resume_argv)
            launch.extend(self._name_argv(name))
        return {
            "id": self.id,
            "label": self.label,
            "launch_argv": launch,
            "resume": {
                "strategy": self.resume_strategy,
                "reference": conversation_reference,
            },
            "capabilities": list(self.capabilities),
        }

    def _name_argv(self, name: str) -> list[str]:
        clean = " ".join(name.split())
        if not clean or not self.name_argv:
            return []
        return [*self.name_argv, clean]

    def conversation_exists(self, reference: str, working_dir: str) -> bool | None:
        """True resumable here, False confirmed gone, None for anything else.

        None is never evidence of absence, so a caller that drops references
        acts only on a confirmed False. `conversation_status` also tells an
        archived conversation apart from an unknown one.
        """
        return self.conversation_status(reference, working_dir).exists()

    def conversation_status(self, reference: str, working_dir: str) -> ConversationStatus:
        try:
            if self.conversation_probe == "claude":
                return ConversationProbe.claude(reference)
            if self.conversation_probe == "codex":
                return ConversationProbe.codex(reference)
        except OSError as exc:
            return ConversationStatus(ConversationState.UNKNOWN, detail=str(exc))
        return ConversationStatus(ConversationState.UNKNOWN)

    def translate_status(
        self, payload: dict[str, Any], requested_state: str
    ) -> dict[str, Any] | None:
        reference = None
        for scope in (payload, payload.get("payload"), payload.get("hookSpecificOutput")):
            if not isinstance(scope, dict):
                continue
            for key in ("session_id", "sessionId", "thread-id", "id"):
                if isinstance(scope.get(key), str) and scope[key]:
                    reference = scope[key]
                    break
        return {"state": requested_state, "conversation_reference": reference}


class ConversationProbe:
    """Read-only lookups against each agent CLI's own on-disk conversation store."""

    UUID_RE = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")

    @staticmethod
    def claude_root() -> Path:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        root = Path(configured).expanduser() if configured else Path.home() / ".claude"
        return root / "projects"

    @staticmethod
    def claude(reference: str) -> ConversationStatus:
        # `claude --resume <id>` finds a transcript from any working dir, not only
        # the project it was recorded under (verified with Claude Code 2026-09),
        # so any project dir holding it means it exists.
        unknown = ConversationStatus(ConversationState.UNKNOWN)
        if not ConversationProbe.UUID_RE.fullmatch(reference):
            return unknown
        root = ConversationProbe.claude_root()
        if not root.is_dir():
            return unknown
        if any(root.glob(f"*/{reference}.jsonl")):
            return ConversationStatus(ConversationState.EXISTS)
        return ConversationStatus(ConversationState.GONE)

    @staticmethod
    def codex_root() -> Path:
        configured = os.environ.get("CODEX_HOME")
        return Path(configured).expanduser() if configured else Path.home() / ".codex"

    @staticmethod
    def codex(reference: str) -> ConversationStatus:
        unknown = ConversationStatus(ConversationState.UNKNOWN)
        # `codex resume` also accepts a session name, which no file lookup can check.
        if not ConversationProbe.UUID_RE.fullmatch(reference):
            return unknown
        root = ConversationProbe.codex_root()
        sessions = root / "sessions"
        if not sessions.is_dir():
            return unknown
        pattern = f"rollout-*-{reference}.jsonl"
        if any(sessions.glob(f"*/*/*/{pattern}")):
            return ConversationStatus(ConversationState.EXISTS)
        if any((root / "archived_sessions").glob(pattern)):
            # Verified against codex-cli 0.157: resume refuses an archived
            # session until `codex unarchive <id>` restores it.
            return ConversationStatus(
                ConversationState.ARCHIVED,
                detail=f"run `codex unarchive {reference}` to resume it",
            )
        return ConversationStatus(ConversationState.GONE)


def bundled_drivers() -> dict[str, AgentDriver]:
    shell = os.environ.get("SHELL") or "/bin/sh"
    return {
        "claude": CommandDriver(
            "claude",
            "Claude",
            ["claude"],
            resume_strategy="conversation_id",
            resume_argv=["claude", "--resume"],
            name_argv=["--name"],
            capabilities=("launch", "resume", "status", "conversation_check"),
            conversation_probe="claude",
        ),
        "codex": CommandDriver(
            "codex",
            "Codex",
            ["codex"],
            resume_strategy="conversation_id",
            resume_argv=["codex", "resume"],
            resume_cwd_flag="-C",
            capabilities=("launch", "resume", "status", "conversation_check"),
            conversation_probe="codex",
        ),
        "antigravity": CommandDriver(
            "antigravity",
            "Antigravity",
            ["agy"],
            resume_strategy="workspace_continue",
            resume_argv=["agy", "-c"],
            capabilities=("launch", "resume"),
        ),
        "term": CommandDriver("term", "Terminal", [shell]),
    }


class DriverRegistry:
    def __init__(self, drivers: dict[str, AgentDriver] | None = None):
        self._drivers = drivers or bundled_drivers()

    def load_entry_points(self) -> None:
        selected = entry_points()
        plugins = selected.select(group="hive_ide.drivers") if hasattr(selected, "select") else []
        for item in plugins:
            driver = item.load()()
            if driver.id in self._drivers:
                raise UsageError(f"Driver plugin id {driver.id!r} conflicts with an existing driver.")
            self._drivers[driver.id] = driver

    def add_declarative(self, driver_id: str, config: dict[str, Any]) -> None:
        command = config.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(v, str) for v in command):
            raise UsageError(f"Declarative driver {driver_id!r} needs a non-empty command array.")
        self._drivers[driver_id] = CommandDriver(
            driver_id,
            str(config.get("label") or driver_id),
            command,
            resume_strategy=str(config.get("resume_strategy") or "none"),
            resume_argv=list(config.get("resume_argv") or []),
            capabilities=tuple(config.get("capabilities") or ("launch",)),
        )

    def get(self, driver_id: str) -> AgentDriver:
        try:
            return self._drivers[driver_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._drivers))
            raise UsageError(f"Unknown driver {driver_id!r}. Available: {known}.") from exc

    def ids(self) -> list[str]:
        return sorted(self._drivers)
