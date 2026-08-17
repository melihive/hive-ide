"""Extensible semantic providers for sidebar state and icon slots."""

from __future__ import annotations

import hashlib
import json
import time
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
    priority = {
        "blocked": 30,
        "release": 20,
        "compacting": 10,
        "task": 5,
        "default": 5,
    }

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "kind": "builtin",
            "default_icons": dict(self.default_icons),
        }

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None:
        values = [
            value
            for value in (
                self._activity_value(StateIO.read_session_activity(state_home, session)),
                self._activity_value(self._legacy_activity(session)),
            )
            if value
        ]
        if not values:
            return None
        values.sort(key=lambda value: self.priority.get(value, 0), reverse=True)
        return values[0]

    def _activity_value(self, activity: dict[str, Any] | None) -> str | None:
        if not activity or _age_seconds(activity.get("ts")) > self.stale_seconds:
            return None
        if activity.get("state") == "blocked":
            return "blocked"
        kind = activity.get("kind")
        return kind if isinstance(kind, str) and kind else None

    @classmethod
    def _legacy_activity(cls, session: dict[str, Any]) -> dict[str, Any] | None:
        workspace = session.get("workspace_key") or session.get("repo")
        if not isinstance(workspace, str) or not workspace:
            return None
        skill_dir = Path(workspace) / ".skills" / "hive-ide"
        directory = skill_dir / ".state_local" / "activity"
        hive = ((session.get("host") or {}).get("hive") or {})
        legacy = hive.get("legacy_record") if isinstance(hive.get("legacy_record"), dict) else {}
        repo = hive.get("repo") or legacy.get("repo") or Path(workspace).name
        names = [session.get("name"), legacy.get("name")]
        for name in names:
            if not isinstance(repo, str) or not repo or not isinstance(name, str) or not name:
                continue
            path = directory / f"{cls._legacy_identity_key(repo, name)}.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return {**data, "ts": data.get("ts") or data.get("observed_at")}
        return None

    @staticmethod
    def _legacy_identity_key(repo: str, name: str) -> str:
        return hashlib.sha1(f"{repo}\x00{name}".encode("utf-8")).hexdigest()[:16]


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

    def __init__(self) -> None:
        pass

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "region": self.region,
            "kind": "builtin",
            "default_icons": dict(self.default_icons),
        }

    def value(self, state_home: Path, session: dict[str, Any]) -> str | None:
        status = StateIO.read_session_status(state_home, session)
        count = _subagent_count(session, status)
        if count <= 0:
            return None
        return f"count:{min(count, 99)}"


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
