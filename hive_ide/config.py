"""User config loading and pane-safe snapshot normalization."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, SCHEMA_VERSION, __version__
from .drivers import DriverRegistry
from .errors import StateError, UsageError
from .layout import IdeLayout
from .python_cmd import PythonCommand
from .sidebar_grid import SidebarGrid
from .sidebar_plugins import SidebarProviderRegistry


DEFAULT_THEME = {
    "agents": {
        "claude": {"icon": "🔆", "label": "Claude"},
        "codex": {"icon": "🌀", "label": "Codex"},
        "antigravity": {"icon": "🔷", "label": "Antigravity"},
        "term": {"icon": "💻", "label": "Terminal"},
    },
    "status": {"working": "working", "waiting": "waiting", "error": "error"},
    "labels": {"sessions": "Sessions", "archive": "Archive"},
}

DEFAULT_KEYS = {
    "prefix": None,
    "bindings": {
        "next": "n",
        "previous": "p",
        "sidebar": "l",
        "chat": "c",
        "plan": "e",
        "agent": "a",
        "card": "i",
        "options": "o",
        "jump_plan": "g",
        "reset": "r",
        "help": "k",
        "new": "+",
        "error": "x",
    },
}

DEFAULT_SIDEBAR = {
    "state": "activity",
    "slots": ["plan", "checkout"],
    "icons": {
        "drivers": {
            "claude": "🔆",
            "codex": "🌀",
            "antigravity": "🔷",
            "term": "💻",
            "terminal": "💻",
            "default": "•",
        },
        "status": {"working": "●", "waiting": "●", "error": "!"},
        "controls": {"create": "+", "archive": "▾"},
        "providers": {
            "checkout": {"busy": "…"},
            "subagents": {"running": "◼"},
        },
    },
}


def _icon(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageError(f"Config field {field!r} must be a non-empty string.")
    width = SidebarGrid.cell_width(value)
    if width not in {1, 2}:
        raise UsageError(
            f"Config field {field!r} must occupy one or two terminal cells; got {width}."
        )
    return value


def _icon_map(
    defaults: dict[str, str], incoming: Any, field: str
) -> dict[str, str]:
    if incoming is None:
        incoming = {}
    if not isinstance(incoming, dict):
        raise UsageError(f"Config field {field!r} must be an object.")
    merged = dict(defaults)
    for key, value in incoming.items():
        if not isinstance(key, str) or not key:
            raise UsageError(f"Every key in config field {field!r} must be a string.")
        merged[key] = _icon(value, f"{field}.{key}")
    return merged


def _sidebar_config(
    config: dict[str, Any], registry: SidebarProviderRegistry
) -> dict[str, Any]:
    incoming = config.get("sidebar") or {}
    if not isinstance(incoming, dict):
        raise UsageError("Config field 'sidebar' must be an object.")

    custom_providers = incoming.get("providers") or {}
    if not isinstance(custom_providers, dict):
        raise UsageError("Config field 'sidebar.providers' must be an object.")
    for provider_id, definition in custom_providers.items():
        if not isinstance(provider_id, str) or not provider_id:
            raise UsageError("Every sidebar provider id must be a non-empty string.")
        registry.register_field(provider_id, definition)

    state = incoming.get("state", DEFAULT_SIDEBAR["state"])
    if state is not None and not isinstance(state, str):
        raise UsageError("Config field 'sidebar.state' must be a provider id or null.")
    if state is not None:
        registry.get(state, "state")

    slots = incoming.get("slots", DEFAULT_SIDEBAR["slots"])
    if not isinstance(slots, list) or not all(
        isinstance(provider_id, str) and provider_id for provider_id in slots
    ):
        raise UsageError("Config field 'sidebar.slots' must be an array of provider ids.")
    if len(slots) != len(set(slots)):
        raise UsageError("Config field 'sidebar.slots' cannot contain duplicate providers.")
    for provider_id in slots:
        registry.get(provider_id, "slot")

    icons = incoming.get("icons") or {}
    if not isinstance(icons, dict):
        raise UsageError("Config field 'sidebar.icons' must be an object.")
    legacy_agents = ((config.get("theme") or {}).get("agents") or {})
    legacy_driver_icons = {
        driver_id: definition["icon"]
        for driver_id, definition in legacy_agents.items()
        if isinstance(definition, dict) and definition.get("icon")
    }
    incoming_drivers = icons.get("drivers") or {}
    if not isinstance(incoming_drivers, dict):
        raise UsageError("Config field 'sidebar.icons.drivers' must be an object.")
    driver_overrides = {**legacy_driver_icons, **incoming_drivers}
    providers_incoming = icons.get("providers") or {}
    if not isinstance(providers_incoming, dict):
        raise UsageError("Config field 'sidebar.icons.providers' must be an object.")

    provider_ids = [provider_id for provider_id in [state, *slots] if provider_id]
    provider_icons = {
        provider_id: _icon_map(
            registry.get(provider_id).default_icons,
            providers_incoming.get(provider_id),
            f"sidebar.icons.providers.{provider_id}",
        )
        for provider_id in provider_ids
    }
    unknown_icon_providers = sorted(set(providers_incoming) - set(provider_ids))
    if unknown_icon_providers:
        raise UsageError(
            "Sidebar icon overrides reference providers that are not enabled: "
            + ", ".join(unknown_icon_providers)
            + "."
        )

    return {
        "state": state,
        "slots": list(slots),
        "providers": registry.snapshot(provider_ids),
        "icons": {
            "drivers": _icon_map(
                DEFAULT_SIDEBAR["icons"]["drivers"],
                driver_overrides,
                "sidebar.icons.drivers",
            ),
            "status": _icon_map(
                DEFAULT_SIDEBAR["icons"]["status"],
                icons.get("status"),
                "sidebar.icons.status",
            ),
            "controls": _icon_map(
                DEFAULT_SIDEBAR["icons"]["controls"],
                icons.get("controls"),
                "sidebar.icons.controls",
            ),
            "providers": provider_icons,
        },
    }


def _editor_argv(config: dict[str, Any]) -> list[str]:
    configured = config.get("editor") or os.environ.get("HIVE_IDE_EDITOR")
    if isinstance(configured, str):
        argv = shlex.split(configured)
    elif isinstance(configured, list) and all(
        isinstance(value, str) for value in configured
    ):
        argv = list(configured)
    elif configured is None:
        argv = ["micro"] if shutil.which("micro") else ["less"]
    else:
        raise UsageError("Config field 'editor' must be a command string or string array.")
    if not argv:
        raise UsageError("Config field 'editor' cannot be empty.")
    return argv


def _key_config(config: dict[str, Any]) -> dict[str, Any]:
    incoming = config.get("keys") or {}
    if not isinstance(incoming, dict):
        raise UsageError("Config field 'keys' must be an object.")
    prefix = incoming.get("prefix")
    if prefix is not None and (not isinstance(prefix, str) or not prefix.strip()):
        raise UsageError("Config field 'keys.prefix' must be a non-empty string.")
    overrides = incoming.get("bindings") or {}
    if not isinstance(overrides, dict):
        raise UsageError("Config field 'keys.bindings' must be an object.")
    unknown = sorted(set(overrides) - set(DEFAULT_KEYS["bindings"]))
    if unknown:
        raise UsageError(f"Unknown key binding actions: {', '.join(unknown)}.")
    bindings = dict(DEFAULT_KEYS["bindings"])
    for action, key in overrides.items():
        if key is not None and (not isinstance(key, str) or not key.strip()):
            raise UsageError(f"Key binding {action!r} must be a string or null.")
        bindings[action] = key
    return {"prefix": prefix, "bindings": bindings}


def load_config(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StateError(f"Cannot read config {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"Invalid JSON in config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise UsageError(f"Config {path} must contain a JSON object.")
    return data


def configured_registry(config: dict[str, Any], *, plugins: bool = True) -> DriverRegistry:
    registry = DriverRegistry()
    custom = config.get("drivers") or {}
    if not isinstance(custom, dict):
        raise UsageError("Config field 'drivers' must be an object.")
    for driver_id, definition in custom.items():
        if not isinstance(driver_id, str) or not isinstance(definition, dict):
            raise UsageError("Every declarative driver must be an object keyed by its id.")
        registry.add_declarative(driver_id, definition)
    if plugins:
        registry.load_entry_points()
    return registry


def normalized_snapshot(
    *,
    state_home: Path,
    workspace_key: str,
    workspace_hash: str,
    socket: str,
    registry: DriverRegistry,
    config: dict[str, Any],
) -> dict[str, Any]:
    theme = config.get("theme") or DEFAULT_THEME
    if not isinstance(theme, dict):
        raise UsageError("Config field 'theme' must be an object.")
    drivers: dict[str, Any] = {}
    for driver_id in registry.ids():
        driver = registry.get(driver_id)
        available = driver.detect()
        drivers[driver_id] = {
            "id": driver_id,
            "label": driver.label,
            "available": available.available,
            "executable": available.executable,
            "detail": available.detail,
        }
    sidebar_registry = SidebarProviderRegistry()
    sidebar_registry.load_entry_points()
    return {
        "schema_version": SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "package_version": __version__,
        "workspace_key": workspace_key,
        "workspace_hash": workspace_hash,
        "state_home": str(state_home),
        "command_argv": PythonCommand.cli_argv(python=sys.executable),
        "tmux": {
            "socket": socket,
            "sidebar_width": IdeLayout.SIDEBAR_W,
            "plan_width": IdeLayout.PLAN_W,
        },
        "theme": theme,
        "sidebar": _sidebar_config(config, sidebar_registry),
        "keys": _key_config(config),
        "editor": {"argv": _editor_argv(config)},
        "drivers": drivers,
        "sources": config.get("sources") or {},
    }
