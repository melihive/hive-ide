"""Bundled and extensible agent-driver registry."""

from __future__ import annotations

import os
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
        capabilities: tuple[str, ...] = ("launch",),
    ):
        self.id = driver_id
        self.label = label
        self.command = command
        self.resume_strategy = resume_strategy
        self.resume_argv = resume_argv or []
        self.resume_cwd_flag = resume_cwd_flag
        self.capabilities = capabilities

    def detect(self) -> DriverAvailability:
        executable = shutil.which(self.command[0])
        return DriverAvailability(bool(executable), executable, "" if executable else "not on PATH")

    def resolve(
        self, *, name: str, working_dir: str, conversation_reference: str | None
    ) -> dict[str, Any]:
        launch = list(self.command)
        if conversation_reference and self.resume_strategy == "conversation_id":
            launch = list(self.resume_argv)
            if self.resume_cwd_flag:
                launch.extend([self.resume_cwd_flag, working_dir])
            launch.append(conversation_reference)
        elif conversation_reference and self.resume_strategy == "workspace_continue":
            launch = list(self.resume_argv)
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

    def conversation_exists(self, reference: str, working_dir: str) -> bool | None:
        return None

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


def bundled_drivers() -> dict[str, AgentDriver]:
    shell = os.environ.get("SHELL") or "/bin/sh"
    return {
        "claude": CommandDriver(
            "claude",
            "Claude",
            ["claude"],
            resume_strategy="conversation_id",
            resume_argv=["claude", "--resume"],
            capabilities=("launch", "resume", "status", "conversation_check"),
        ),
        "codex": CommandDriver(
            "codex",
            "Codex",
            ["codex"],
            resume_strategy="conversation_id",
            resume_argv=["codex", "resume"],
            resume_cwd_flag="-C",
            capabilities=("launch", "resume", "status", "conversation_check"),
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
