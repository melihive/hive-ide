"""Per-session interpreter selection and compatibility handshake."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from . import PROTOCOL_VERSION, SCHEMA_VERSION
from .errors import UsageError
from .python_cmd import PythonCommand


#: Handshakes already answered by this process, keyed by interpreter path.
#:
#: The handshake costs a full interpreter start — and `repair_all` asks it once
#: per session, which on a 15-session workspace was 15 identical questions to the
#: same `/usr/bin/python3`: 1.35s of a 4.06s `open`, measured 2026-11-12.
#:
#: Safe for the life of a process because an interpreter does not change version
#: underneath one — EXCEPT where this process is what changed it. `install()`
#: upgrades an environment and then inspects the same path, so it would be handed
#: the version from before its own upgrade (Codex review, 2026-11-12); such a
#: caller passes `refresh=True`. The assumption is sound for every caller that
#: only reads, which is all the rest of them.
#:
#: A FAILED handshake is not cached at all, so a broken environment is re-checked
#: rather than written off for the rest of the run.
_HANDSHAKES: dict[str, dict[str, Any]] = {}


def inspect_interpreter(
    interpreter: str | Path, *, refresh: bool = False
) -> dict[str, Any]:
    """Handshake one interpreter. `refresh=True` re-asks even if it is cached.

    Pass `refresh` whenever THIS process has just changed what lives at that
    path — installing or upgrading into the environment — because the cached
    answer predates the change and would report the version being replaced.
    """
    # Preserve a venv's executable path. Resolving its symlink to /usr/bin/python
    # discards the environment that contains hive_ide.
    path = str(Path(interpreter).expanduser().absolute())
    if refresh:
        _HANDSHAKES.pop(path, None)
    # Checked on EVERY call, cache hit or not: it is a stat, not a subprocess, so
    # it costs nothing — and skipping it let an interpreter that had since been
    # deleted keep answering from the cache as though it were still there.
    if not os.access(path, os.X_OK):
        _HANDSHAKES.pop(path, None)
        raise UsageError(f"Selected interpreter is not executable: {path}")
    cached = _HANDSHAKES.get(path)
    if cached is not None:
        # A copy per caller: the cached dict is shared, and handing out the same
        # object lets one caller's edit rewrite what every later caller reads.
        return dict(cached)
    result = subprocess.run(
        PythonCommand.cli_argv(["version"], python=path),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise UsageError(
            f"Interpreter {path} cannot import hive_ide. Install the package into "
            "that environment."
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
    # Only a handshake that passed every check is remembered — the raises above
    # all leave the cache untouched, so a failure is re-tested next time rather
    # than condemning the interpreter for the rest of the run.
    _HANDSHAKES[path] = dict(document)
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
            interpreter = default_interpreter
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
