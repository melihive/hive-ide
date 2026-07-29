"""Extensible semantic providers for sidebar state and icon slots."""

from __future__ import annotations

import time
import os
import re
import subprocess
from datetime import datetime, timezone
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal, Protocol

from .errors import UsageError
from .git_status import inspect_linked_checkout
from .state_compat import StateIO


SidebarRegion = Literal["state", "slot"]


class SidebarProvider(Protocol):
    """A provider that can be normalized before isolated panes start."""

    id: str
    region: SidebarRegion
    default_icons: dict[str, str]

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None: ...
    def snapshot(self) -> dict[str, Any]: ...


def _age_seconds(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        observed = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return float("inf")
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - observed).total_seconds())


class ActivityProvider:
    id = "activity"
    region: SidebarRegion = "state"
    default_icons = {
        "task": "📋",
        "default": "📋",
        "release": "🚀",
        "blocked": "⛔",
        "compacting": "🧠",
    }
    stale_seconds = 1800

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "kind": "builtin",
            "default_icons": dict(self.default_icons),
        }

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None:
        activity = StateIO.read_session_activity(state_home, session)
        if not activity or _age_seconds(activity.get("ts")) > self.stale_seconds:
            return None
        if activity.get("state") == "blocked":
            return "blocked"
        kind = activity.get("kind")
        return kind if isinstance(kind, str) and kind else None


class PlanProvider:
    id = "plan"
    region: SidebarRegion = "slot"
    default_icons = {"active": "📝", "done": "📦"}

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "kind": "builtin",
            "default_icons": dict(self.default_icons),
        }

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None:
        plan = session.get("plan")
        path = plan.get("path") if isinstance(plan, dict) else plan
        if not path:
            return None
        hive = ((session.get("host") or {}).get("hive") or {})
        legacy = hive.get("legacy_record") or {}
        status = (
            session.get("plan_status")
            or hive.get("plan_status")
            or legacy.get("plan_status")
            or ""
        ).lower()
        return "done" if status in {"merged", "done"} or "/_archive/" in path else "active"


def _subagent_count(session: dict[str, Any], status: dict[str, Any] | None = None) -> int:
    candidates: list[Any] = []
    if status:
        candidates.extend(
            [
                status.get("subagents_running"),
                ((status.get("subagents") or {}) if isinstance(status.get("subagents"), dict) else {}).get("running"),
            ]
        )
    subagents = session.get("subagents")
    if isinstance(subagents, dict):
        candidates.append(subagents.get("running"))
    hive = ((session.get("host") or {}).get("hive") or {})
    hive_subagents = hive.get("subagents")
    if isinstance(hive_subagents, dict):
        candidates.append(hive_subagents.get("running"))
    legacy = hive.get("legacy_record") or {}
    legacy_subagents = legacy.get("subagents")
    if isinstance(legacy_subagents, dict):
        candidates.append(legacy_subagents.get("running"))
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str) and value.isdigit():
            return max(0, int(value))
    return 0


class SubagentsProvider:
    id = "subagents"
    region: SidebarRegion = "slot"
    default_icons: dict[str, str] = {}
    cache_seconds = 2
    ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    AGENT_ROW_RE = re.compile(r"^\s*[○◦]\s+(?:codex|claude)\b", re.IGNORECASE)
    STATUS_BAR_AGENTS_RE = re.compile(r"←\s+(\d+)\s+agents?\b", re.IGNORECASE)
    WAITING_RE = re.compile(r"\bWaiting for\s+(\d+)\s+background agents?\b", re.IGNORECASE)
    CLAUDE_BACKGROUND_RE = re.compile(
        r"\bsession\b.*\bis currently running as a background agent\b",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        self._live_cache: dict[tuple[str, str], tuple[float, int | None]] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "kind": "builtin",
            "default_icons": dict(self.default_icons),
        }

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None:
        status = StateIO.read_session_status(state_home, session)
        recorded_count = _subagent_count(session, status)
        live_count = self._live_pane_count_observed(session)
        count = live_count if live_count is not None else recorded_count
        if live_count is not None and live_count != recorded_count:
            StateIO.write_session_status_update(
                state_home,
                session,
                {
                    "driver": (session.get("driver") or {}).get("id"),
                    "state": (status or {}).get("state") or "idle",
                    "subagents": {"running": live_count},
                },
            )
        if count <= 0:
            return None
        return f"count:{min(count, 99)}"

    def _live_pane_count(self, session: dict[str, Any]) -> int:
        return self._live_pane_count_observed(session) or 0

    def _live_pane_count_observed(self, session: dict[str, Any]) -> int | None:
        socket = os.environ.get("HIVE_IDE_TMUX_SOCKET") or ""
        session_id = str(session.get("id") or "")
        if not socket or not session_id:
            return None
        key = (socket, session_id)
        now = time.monotonic()
        cached = self._live_cache.get(key)
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]
        count = self._read_live_pane_count_observed(socket, session_id)
        self._live_cache[key] = (now, count)
        return count

    @classmethod
    def _read_live_pane_count(cls, socket: str, session_id: str) -> int:
        return cls._read_live_pane_count_observed(socket, session_id) or 0

    @classmethod
    def _read_live_pane_count_observed(cls, socket: str, session_id: str) -> int | None:
        window_id = cls._tmux_field(
            socket,
            [
                "list-windows",
                "-F",
                "#{@hive_ide_session_id}\t#{window_id}",
            ],
            session_id,
        )
        if not window_id:
            return None
        pane_id = cls._tmux_field(
            socket,
            [
                "list-panes",
                "-t",
                window_id,
                "-F",
                "#{@hive_ide_pane}\t#{pane_id}",
            ],
            "agent",
        )
        if not pane_id:
            return None
        try:
            captured = subprocess.run(
                [
                    "tmux",
                    "-L",
                    socket,
                    "capture-pane",
                    "-p",
                    "-e",
                    "-S",
                    "-80",
                    "-t",
                    pane_id,
                ],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if captured.returncode != 0:
            return None
        return cls._parse_live_pane_count(captured.stdout)

    @staticmethod
    def _tmux_field(socket: str, args: list[str], needle: str) -> str:
        try:
            result = subprocess.run(
                ["tmux", "-L", socket, *args],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if result.returncode != 0:
            return ""
        for line in result.stdout.splitlines():
            left, _, right = line.partition("\t")
            if left == needle:
                return right
        return ""

    @classmethod
    def _parse_live_pane_count(cls, text: str) -> int:
        rows = 0
        waiting = 0
        status_bar = 0
        for raw in text.splitlines():
            line = cls.ANSI_RE.sub("", raw)
            if match := cls.STATUS_BAR_AGENTS_RE.search(line):
                status_bar = max(status_bar, int(match.group(1)))
            if cls.AGENT_ROW_RE.search(line):
                rows += 1
            if cls.CLAUDE_BACKGROUND_RE.search(line):
                rows += 1
            if match := cls.WAITING_RE.search(line):
                waiting = max(waiting, int(match.group(1)))
        return max(status_bar, rows, waiting)


class CheckoutProvider:
    id = "checkout"
    region: SidebarRegion = "slot"
    default_icons = {
        "live": "🔀",
        "shipped": "🚢",
        "busy": "⏳",
        "missing": "✅",
        "unknown": "🔀",
    }
    cache_seconds = 5

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, str | None]] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "kind": "builtin",
            "default_icons": dict(self.default_icons),
        }

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None:
        hive = ((session.get("host") or {}).get("hive") or {})
        legacy = hive.get("legacy_record") or {}
        working_dir = session.get("working_dir") or session.get("cwd")
        checkout_state = None
        if working_dir:
            now = time.monotonic()
            cached = self._cache.get(str(working_dir))
            if cached is None or now - cached[0] >= self.cache_seconds:
                status = inspect_linked_checkout(working_dir)
                checkout_state = status.state if status else None
                self._cache[str(working_dir)] = (now, checkout_state)
            else:
                checkout_state = cached[1]
            if checkout_state is not None:
                return checkout_state
            if Path(str(working_dir)).expanduser().is_dir():
                return None

        running_subagents = _subagent_count(
            session, StateIO.read_session_status(state_home, session)
        )
        live_subagents = (
            SubagentsProvider()._live_pane_count_observed(session)
            if os.environ.get("HIVE_IDE_TMUX_SOCKET")
            else running_subagents
        )
        if live_subagents is not None and live_subagents != running_subagents:
            running_subagents = live_subagents
        if (
            session.get("worktree_merged")
            or hive.get("worktree_merged")
            or legacy.get("worktree_merged")
        ):
            if running_subagents:
                return "busy"
            return "missing"
        ahead = (
            session.get("worktree_ahead")
            if "worktree_ahead" in session
            else hive.get("worktree_ahead", legacy.get("worktree_ahead"))
        )
        if ahead is False:
            return "shipped"
        if not working_dir:
            return None
        return None


class FieldProvider:
    """Pane-safe provider that reads a semantic value from normalized JSON."""

    def __init__(
        self,
        provider_id: str,
        region: SidebarRegion,
        *,
        source: str,
        path: list[str],
        default_icons: dict[str, str],
    ) -> None:
        if source not in {"session", "activity"}:
            raise UsageError(
                f"Sidebar provider {provider_id!r} source must be session or activity."
            )
        if not path or not all(isinstance(part, str) and part for part in path):
            raise UsageError(
                f"Sidebar provider {provider_id!r} needs a non-empty string path."
            )
        self.id = provider_id
        self.region = region
        self.source = source
        self.path = list(path)
        self.default_icons = dict(default_icons)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "kind": "field",
            "source": self.source,
            "path": list(self.path),
            "default_icons": dict(self.default_icons),
        }

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None:
        value: Any
        if self.source == "activity":
            value = StateIO.read_session_activity(state_home, session)
        else:
            value = session
        for part in self.path:
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value if isinstance(value, str) and value else None


class SidebarProviderRegistry:
    """Bundled providers plus `hive_ide.sidebar_providers` entry points."""

    def __init__(self) -> None:
        self._providers: dict[str, SidebarProvider] = {
            provider.id: provider
            for provider in (
                ActivityProvider(),
                PlanProvider(),
                SubagentsProvider(),
                CheckoutProvider(),
            )
        }

    def load_entry_points(self) -> None:
        discovered = entry_points()
        selected = (
            discovered.select(group="hive_ide.sidebar_providers")
            if hasattr(discovered, "select")
            else []
        )
        for item in selected:
            self.register(item.load()())

    def register(self, provider: SidebarProvider) -> None:
        if provider.id in self._providers:
            raise UsageError(
                f"Sidebar provider plugin id {provider.id!r} conflicts with an existing provider."
            )
        if provider.region not in {"state", "slot"}:
            raise UsageError(
                f"Sidebar provider {provider.id!r} has invalid region {provider.region!r}."
            )
        snapshot = provider.snapshot()
        if snapshot.get("kind") != "field":
            raise UsageError(
                f"External sidebar provider {provider.id!r} must normalize to a field provider."
            )
        self._providers[provider.id] = provider

    def register_field(self, provider_id: str, definition: dict[str, Any]) -> None:
        if not isinstance(definition, dict):
            raise UsageError(
                f"Sidebar provider definition {provider_id!r} must be an object."
            )
        region = definition.get("region")
        if region not in {"state", "slot"}:
            raise UsageError(
                f"Sidebar provider {provider_id!r} has invalid region {region!r}."
            )
        icons = definition.get("icons") or {}
        if not isinstance(icons, dict):
            raise UsageError(
                f"Sidebar provider {provider_id!r} icons must be an object."
            )
        self.register(
            FieldProvider(
                provider_id,
                region,
                source=definition.get("source") or "session",
                path=definition.get("path") or [],
                default_icons=icons,
            )
        )

    def get(self, provider_id: str, region: SidebarRegion | None = None) -> SidebarProvider:
        try:
            provider = self._providers[provider_id]
        except KeyError as exc:
            known = ", ".join(sorted(self._providers))
            raise UsageError(
                f"Unknown sidebar provider {provider_id!r}. Available: {known}."
            ) from exc
        if region is not None and provider.region != region:
            raise UsageError(
                f"Sidebar provider {provider_id!r} belongs in {provider.region}, not {region}."
            )
        return provider

    def ids(self) -> list[str]:
        return sorted(self._providers)

    def snapshot(self, provider_ids: list[str]) -> dict[str, dict[str, Any]]:
        return {
            provider_id: self.get(provider_id).snapshot()
            for provider_id in provider_ids
        }

    @classmethod
    def from_snapshot(
        cls, definitions: dict[str, Any]
    ) -> "SidebarProviderRegistry":
        registry = cls()
        for provider_id, definition in definitions.items():
            if not isinstance(definition, dict):
                raise UsageError(
                    f"Sidebar provider snapshot {provider_id!r} must be an object."
                )
            if definition.get("kind") == "builtin":
                registry.get(provider_id, definition.get("region"))
                continue
            if definition.get("kind") != "field":
                raise UsageError(
                    f"Sidebar provider {provider_id!r} has unknown snapshot kind."
                )
            if provider_id in registry._providers:
                raise UsageError(
                    f"Sidebar provider snapshot {provider_id!r} conflicts with a builtin."
                )
            registry.register(
                FieldProvider(
                    provider_id,
                    definition.get("region"),
                    source=definition.get("source"),
                    path=definition.get("path") or [],
                    default_icons=definition.get("default_icons") or {},
                )
            )
        return registry
