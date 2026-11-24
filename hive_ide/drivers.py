"""Bundled and extensible agent-driver registry."""

from __future__ import annotations

import json
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


#: Where a launch will land. `resume` names the conversation it joins, `fresh`
#: starts a new one, and `unknown` is a command whose destination cannot be
#: established — never folded into either of the other two.
LAUNCH_RESUME = "resume"
LAUNCH_FRESH = "fresh"
LAUNCH_UNKNOWN = "unknown"


def rendered_destination(rendered: dict[str, Any]) -> tuple[str, str | None]:
    """Where a command THIS installation rendered will land.

    The authority is the `resume` block `resolve()` returns plus WHICH BRANCH
    produced the command, never the shape of the argv, because the argv cannot
    be read for this:

    - the reference can be in the argv without being resumed. A session NAME
      equal to the conversation id puts it there (`claude --name <id>`), and a
      driver whose `resume_strategy` is `none` renders only a fresh launch while
      still carrying the reference in its metadata.
    - the reference can be resumed without being in the argv. `agy -c` continues
      whatever conversation that workspace considers current, which may or may
      not be the one the record names.
    - and the difference between the two renderings cannot be read either. A
      declarative driver with `command = ["claude", "--resume", X]` and
      `resume_argv = ["claude", "--resume"]` renders byte-identical argv with and
      without the reference, and genuinely resumes X in both. Comparing them was
      tried and dropped: only the two strategies below render differently when
      given a reference, so which branch produced a command is exactly what the
      strategy already says.

    So the strategy decides, and the argv only corroborates. A command carrying
    the reference under a strategy that cannot resume it is UNKNOWN rather than
    fresh: a configured `command` of `["claude", "--resume", X]` with the default
    `resume_strategy: none` resumes X while declaring nothing, and calling that
    fresh told both collision guards there was nothing to collide over.

    A session named exactly after a conversation reaches the same UNKNOWN by the
    same route, and is refused where it used to launch. That direction is
    deliberate: the cost is one pathological name, and the cost of the other
    direction is a second driver on a live conversation.
    """
    resume = rendered.get("resume") if isinstance(rendered, dict) else None
    strategy = resume.get("strategy") if isinstance(resume, dict) else None
    reference = resume.get("reference") if isinstance(resume, dict) else None
    if not isinstance(reference, str) or not reference:
        # Nothing was rendered to resume, whatever the driver can do.
        return (LAUNCH_FRESH, None)
    if strategy == "workspace_continue":
        # It joins a conversation it cannot name. Bare `agy` (no reference) is a
        # known fresh launch and took the branch above; only the continuation is
        # unknowable, so the DRIVER is never classified, only the command.
        return (LAUNCH_UNKNOWN, None)
    argv = rendered.get("launch_argv") if isinstance(rendered, dict) else None
    carries = any(reference in str(part) for part in argv or [])
    if strategy == "conversation_id":
        # Declared resume-by-id. The argv still has to carry the id: a driver
        # that says it resumes by id and renders a command without one may be
        # resuming through something we cannot see, so neither answer is
        # evidence.
        return (LAUNCH_RESUME, reference) if carries else (LAUNCH_UNKNOWN, None)
    # `none`, or a strategy this version does not know. Fresh only if the command
    # does not carry the reference at all — a session NAMED after a conversation
    # and a configured command with the id baked in are the same two facts, and
    # no rule separates them, so both take the UNKNOWN that makes a collision
    # guard ask rather than assume.
    return (LAUNCH_UNKNOWN, None) if carries else (LAUNCH_FRESH, None)


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
        launch_flags: list[str] | None = None,
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
        # Flags every launch must carry, fresh or resumed. They go straight after
        # the command (and after a resume subcommand) so they bind to the TUI, and
        # are skipped when the configured command already has them.
        self.launch_flags = list(launch_flags or [])

    def detect(self) -> DriverAvailability:
        executable = shutil.which(self.command[0])
        return DriverAvailability(bool(executable), executable, "" if executable else "not on PATH")

    def resolve(
        self, *, name: str, working_dir: str, conversation_reference: str | None
    ) -> dict[str, Any]:
        launch = self._with_launch_flags(self.command)
        launch.extend(self._name_argv(name))
        if conversation_reference and self.resume_strategy == "conversation_id":
            launch = self._with_launch_flags(self.resume_argv)
            if self.resume_cwd_flag:
                launch.extend([self.resume_cwd_flag, working_dir])
            launch.append(conversation_reference)
            launch.extend(self._name_argv(name))
        elif conversation_reference and self.resume_strategy == "workspace_continue":
            launch = self._with_launch_flags(self.resume_argv)
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

    def _with_launch_flags(self, base: list[str]) -> list[str]:
        missing = [flag for flag in self.launch_flags if flag not in base]
        return [*base, *missing]

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

    def conversation_origin(self, reference: str) -> str | None:
        """The working directory a conversation was STARTED in, or None if unknown.

        Used to tell a misattributed event from a real one. None is not evidence
        of anything — a caller must treat it as "cannot check", never as "foreign".
        """
        try:
            if self.conversation_probe == "codex":
                return ConversationProbe.codex_origin(reference)
        except OSError:
            return None
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
    def codex_origin(reference: str) -> str | None:
        """The `cwd` recorded in a codex rollout's header line.

        codex writes the originating working directory into the first JSONL record
        of every rollout. That is per-conversation and written by codex itself, so
        it survives an identity that arrived through an inherited environment.
        """
        if not ConversationProbe.UUID_RE.fullmatch(reference):
            return None
        sessions = ConversationProbe.codex_root() / "sessions"
        if not sessions.is_dir():
            return None
        matches = sorted(sessions.glob(f"*/*/*/rollout-*-{reference}.jsonl"))
        if not matches:
            return None
        try:
            with matches[0].open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    break
                else:
                    return None
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict):
            return None
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        cwd = payload.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None

    # Rollout records that end a turn: `task_complete` follows a finished turn AND
    # one that failed (an API error is written as an `error` event, then
    # `task_complete`); `turn_aborted` follows an interrupt.
    CODEX_TURN_END_TYPES = frozenset({"task_complete", "turn_aborted"})
    CODEX_TURN_START_TYPE = "task_started"
    # The end of the current turn is the last record codex writes until the next
    # prompt, so the tail is enough. Bounded: rollouts reach tens of MB.
    CODEX_TAIL_BYTES = 256 * 1024

    @staticmethod
    def codex_turn_ended(reference: str, turn_id: str) -> bool | None:
        """Did codex record the end of turn `turn_id` in this conversation's rollout?

        True: a `task_complete` / `turn_aborted` for that turn is on disk. False:
        the turn's `task_started` is the latest record of it, so it is still open.
        None: anything else — no rollout, the turn is not in the tail, unreadable.
        None is never evidence the turn ended.

        This exists because codex runs no hook when a turn ends in an error (no
        `Stop`; its hook events have no failure event), so the hook state left by
        `UserPromptSubmit` would say `working` forever.
        """
        if not isinstance(reference, str) or not ConversationProbe.UUID_RE.fullmatch(reference):
            return None
        if not isinstance(turn_id, str) or not turn_id:
            return None
        sessions = ConversationProbe.codex_root() / "sessions"
        if not sessions.is_dir():
            return None
        matches = sorted(sessions.glob(f"*/*/*/rollout-*-{reference}.jsonl"))
        if not matches:
            return None
        try:
            with matches[-1].open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start = max(0, size - ConversationProbe.CODEX_TAIL_BYTES)
                handle.seek(start)
                data = handle.read()
        except OSError:
            return None
        lines = data.split(b"\n")
        if start > 0:
            lines = lines[1:]  # the first line of a mid-file read is partial
        needle = turn_id.encode("utf-8")
        for line in reversed(lines):
            if needle not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict) or payload.get("turn_id") != turn_id:
                continue
            kind = payload.get("type")
            if kind in ConversationProbe.CODEX_TURN_END_TYPES:
                return True
            if kind == ConversationProbe.CODEX_TURN_START_TYPE:
                return False
        return None

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
            # By default every codex TUI attaches to one shared app-server daemon,
            # and hooks run inside that daemon with the env of whichever pane
            # started it — so every pane reports that pane's HIVE_IDE_SESSION_ID /
            # TMUX_PANE. --no-daemon runs the pane's own server, so its hooks carry
            # its own identity; `codex queue` still reaches it (shared queue db).
            # Both `codex` and `codex resume` accept the flag (codex-cli 0.163).
            launch_flags=["--no-daemon"],
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
        # A configured command that replaces a bundled driver keeps that driver's
        # required launch flags (deduplicated against what it already carries).
        replaced = self._drivers.get(driver_id)
        self._drivers[driver_id] = CommandDriver(
            driver_id,
            str(config.get("label") or driver_id),
            command,
            resume_strategy=str(config.get("resume_strategy") or "none"),
            resume_argv=list(config.get("resume_argv") or []),
            capabilities=tuple(config.get("capabilities") or ("launch",)),
            launch_flags=list(getattr(replaced, "launch_flags", None) or []),
            # A probe is opt-IN for a configured driver, never inherited.
            # Inheriting it (tried on 2026-11-23) reads the IDE's own
            # environment to find the store, so a wrapper that sets its own
            # `CLAUDE_CONFIG_DIR` or `CODEX_HOME` would have its live
            # conversations reported GONE and their references deleted — the
            # exact loss this phase exists to prevent, and worse than the
            # UNKNOWN it replaced, which only ever declines to act. A wrapper
            # that shares the store it wraps can say so with this key.
            conversation_probe=(
                str(config.get("conversation_probe"))
                if config.get("conversation_probe")
                else None
            ),
        )

    def get(self, driver_id: str) -> AgentDriver:
        try:
            return self._drivers[driver_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._drivers))
            raise UsageError(f"Unknown driver {driver_id!r}. Available: {known}.") from exc

    def ids(self) -> list[str]:
        return sorted(self._drivers)
