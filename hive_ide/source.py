"""Per-session interpreter selection and compatibility handshake."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, SCHEMA_VERSION
from .errors import UsageError


def inspect_interpreter(interpreter: str | Path) -> dict[str, Any]:
    # Preserve a venv's executable path. Resolving its symlink to /usr/bin/python
    # discards the environment that contains hive_ide, so isolated import then fails.
    path = str(Path(interpreter).expanduser().absolute())
    if not os.access(path, os.X_OK):
        raise UsageError(f"Selected interpreter is not executable: {path}")
    result = subprocess.run(
        [path, "-I", "-m", "hive_ide.cli", "version"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UsageError(
            f"Interpreter {path} cannot import hive_ide in isolated mode. "
            "Install the package into that environment."
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise UsageError(f"Interpreter {path} returned an invalid handshake.") from exc
    if document.get("protocol_version") != PROTOCOL_VERSION:
        raise UsageError(
            f"Interpreter {path} uses protocol {document.get('protocol_version')}; "
            f"expected {PROTOCOL_VERSION}."
        )
    if document.get("schema_version") != SCHEMA_VERSION:
        raise UsageError(
            f"Interpreter {path} uses schema {document.get('schema_version')}; "
            f"expected {SCHEMA_VERSION}."
        )
    return document


def resolve_source(
    requested: str,
    config: dict[str, Any],
    *,
    default_interpreter: str,
) -> dict[str, str]:
    sources = config.get("sources") or {}
    if not isinstance(sources, dict):
        raise UsageError("Config field 'sources' must be an object.")
    if requested == "stable":
        configured = sources.get("stable")
        interpreter = (
            configured.get("interpreter")
            if isinstance(configured, dict)
            else configured
        )
        if not interpreter:
            from .environments import managed_interpreter

            managed = managed_interpreter("stable")
            interpreter = str(managed) if managed.is_file() else default_interpreter
        kind = "stable"
    elif requested == "dev":
        configured = sources.get("dev")
        interpreter = (
            configured.get("interpreter")
            if isinstance(configured, dict)
            else configured
        ) or os.environ.get("HIVE_IDE_DEV_PYTHON")
        if not interpreter:
            from .environments import managed_interpreter

            managed = managed_interpreter("dev")
            interpreter = str(managed) if managed.is_file() else None
        if not interpreter:
            raise UsageError(
                "No dev interpreter configured. Run environment-setup with "
                "--dev-checkout, set sources.dev.interpreter in config, or set "
                "HIVE_IDE_DEV_PYTHON."
            )
        kind = "dev"
    else:
        interpreter = requested
        kind = "explicit"
    handshake = inspect_interpreter(str(interpreter))
    return {
        "kind": kind,
        "interpreter": handshake["interpreter"],
        "version": handshake["package_version"],
    }
