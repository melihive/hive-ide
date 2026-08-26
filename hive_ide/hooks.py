"""Machine-global Claude and Codex status-hook installation."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from .environments import managed_interpreter
from .errors import StateError, UsageError
from .paths import config_path, state_home
from .python_cmd import PythonCommand
from .source import inspect_interpreter


def _configured_stable_python() -> Path | None:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    sources = data.get("sources") if isinstance(data, dict) else None
    stable = sources.get("stable") if isinstance(sources, dict) else None
    interpreter = stable.get("interpreter") if isinstance(stable, dict) else stable
    return Path(interpreter).expanduser() if isinstance(interpreter, str) and interpreter else None


class HookInstaller:
    CLAUDE_EVENTS = {
        "UserPromptSubmit": ("state", "working"),
        "Stop": ("state", "waiting"),
        "Notification": ("state", "waiting"),
        "SubagentStart": ("subagent", "start"),
        "SubagentStop": ("subagent", "stop"),
        "PreCompact": ("activity", "compacting"),
        "PostCompact": ("activity", "clear"),
    }
    CODEX_EVENTS = {
        "UserPromptSubmit": ("state", "working"),
        "Stop": ("state", "waiting"),
        "SubagentStart": ("subagent", "start"),
        "SubagentStop": ("subagent", "stop"),
        "PreCompact": ("activity", "compacting"),
        "PostCompact": ("activity", "clear"),
    }
    COMMAND_MARKER = "-m hive_ide.hook"

    def __init__(
        self,
        *,
        home: str | Path | None = None,
        stable_python: str | Path | None = None,
        selected_state_home: str | Path | None = None,
    ):
        self.home = Path(home or Path.home()).expanduser().resolve()
        selected_python = stable_python or _configured_stable_python() or managed_interpreter("stable")
        self.stable_python = Path(selected_python).expanduser().absolute()
        self.state_home = state_home(selected_state_home)

    def command(self, action: tuple[str, str], driver: str) -> str:
        flag, value = action
        argv = PythonCommand.module_argv(
            "hook",
            [
                "--state-home",
                str(self.state_home),
                f"--{flag}",
                value,
                "--driver",
                driver,
            ],
            python=str(self.stable_python),
        )
        # A missing stable environment must never turn a status decoration into a
        # prompt-blocking failure.
        return f"{shlex.join(argv)} || true"

    @staticmethod
    def _load(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise UsageError(f"Hook config is not valid JSON: {path}: {exc}") from exc
        except OSError as exc:
            raise StateError(f"Cannot read hook config {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise UsageError(f"Hook config must contain a JSON object: {path}")
        return data

    def merge(
        self,
        data: dict[str, Any],
        events: dict[str, tuple[str, str]],
        driver: str,
    ) -> bool:
        hooks = data.setdefault("hooks", {})
        if not isinstance(hooks, dict):
            raise UsageError("Hook config field 'hooks' must be an object.")
        changed = False
        for event, action in events.items():
            groups = hooks.setdefault(event, [])
            if not isinstance(groups, list):
                raise UsageError(f"Hook event {event!r} must contain a list.")
            command = self.command(action, driver)
            found = False
            for group in groups:
                if not isinstance(group, dict):
                    raise UsageError(f"Hook event {event!r} contains a non-object group.")
                handlers = group.setdefault("hooks", [])
                if not isinstance(handlers, list):
                    raise UsageError(f"Hook event {event!r} has a non-list handlers field.")
                kept = []
                for handler in handlers:
                    if not isinstance(handler, dict):
                        raise UsageError(
                            f"Hook event {event!r} contains a non-object handler."
                        )
                    existing_command = handler.get("command") or ""
                    installed = self.COMMAND_MARKER in existing_command
                    if installed and handler.get("command") != command:
                        changed = True
                        continue
                    if handler.get("command") == command:
                        found = True
                    kept.append(handler)
                if kept != handlers:
                    group["hooks"] = kept
            groups[:] = [group for group in groups if group.get("hooks")]
            if not found:
                groups.append(
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": command,
                                "timeout": 10,
                            }
                        ]
                    }
                )
                changed = True
        return changed

    def plan(self) -> list[tuple[Path, dict[str, Any]]]:
        inspect_interpreter(self.stable_python)
        targets = [
            (
                self.home / ".claude" / "settings.json",
                self.CLAUDE_EVENTS,
                "claude",
            ),
            (
                self.home / ".codex" / "hooks.json",
                self.CODEX_EVENTS,
                "codex",
            ),
        ]
        changes = []
        for path, events, driver in targets:
            document = self._load(path)
            if self.merge(document, events, driver):
                changes.append((path, document))
        return changes

    @staticmethod
    def _write(path: Path, document: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            shutil.copy2(path, path.with_suffix(f"{path.suffix}.hive-ide.bak"))
        payload = json.dumps(document, indent=2) + "\n"
        try:
            fd, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        except OSError as exc:
            raise StateError(f"Cannot write hook config {path}: {exc}") from exc

    def setup(self, *, apply: bool) -> dict[str, Any]:
        changes = self.plan()
        if apply:
            for path, document in changes:
                self._write(path, document)
        return {
            "applied": apply,
            "stable_python": str(self.stable_python),
            "state_home": str(self.state_home),
            "changes": [str(path) for path, _ in changes],
            "codex_trust_required": any(
                path.name == "hooks.json" for path, _ in changes
            ),
        }

    def verify(self) -> list[str]:
        findings = []
        try:
            inspect_interpreter(self.stable_python)
        except UsageError as exc:
            findings.append(str(exc))
            return findings
        for path, events, driver in (
            (
                self.home / ".claude" / "settings.json",
                self.CLAUDE_EVENTS,
                "claude",
            ),
            (
                self.home / ".codex" / "hooks.json",
                self.CODEX_EVENTS,
                "codex",
            ),
        ):
            document = self._load(path)
            hooks = document.get("hooks") or {}
            missing = []
            for event, action in events.items():
                command = self.command(action, driver)
                groups = (hooks.get(event) or []) if isinstance(hooks, dict) else []
                installed = {
                    handler.get("command")
                    for group in groups
                    if isinstance(group, dict)
                    for handler in (group.get("hooks") or [])
                    if isinstance(handler, dict)
                }
                if command not in installed:
                    missing.append(event)
            if missing:
                findings.append(
                    f"{path}: hooks missing or stale for {', '.join(missing)}"
                )
        return findings
