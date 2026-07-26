"""Config, state, and workspace identity resolution."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if value := os.environ.get("HIVE_IDE_CONFIG"):
        return Path(value).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "hive-ide" / "config.json"


def state_home(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if value := os.environ.get("HIVE_IDE_STATE_HOME"):
        return Path(value).expanduser().resolve()
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return (root / "hive-ide").resolve()


def environment_home(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if value := os.environ.get("HIVE_IDE_ENV_HOME"):
        return Path(value).expanduser().resolve()
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (root / "hive-ide" / "environments").resolve()


def workspace_key(directory: str | Path | None = None) -> str:
    return str(Path(directory or Path.cwd()).expanduser().resolve())


def workspace_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
