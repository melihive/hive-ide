"""Managed stable and development Python environments."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .errors import UsageError
from .paths import environment_home
from .source import inspect_interpreter


def managed_interpreter(kind: str, home: str | Path | None = None) -> Path:
    if kind not in {"stable", "dev"}:
        raise ValueError(f"Unknown managed environment: {kind}")
    return environment_home(home) / kind / "bin" / "python"


class EnvironmentManager:
    def __init__(self, home: str | Path | None = None):
        self.home = environment_home(home)

    @staticmethod
    def _run(argv: list[str], *, purpose: str) -> None:
        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise UsageError(f"Could not {purpose}{suffix}")

    def _ensure_venv(self, kind: str) -> Path:
        interpreter = managed_interpreter(kind, self.home)
        if not interpreter.is_file():
            interpreter.parent.parent.mkdir(parents=True, exist_ok=True)
            self._run(
                [sys.executable, "-m", "venv", str(interpreter.parent.parent)],
                purpose=f"create the {kind} environment",
            )
        return interpreter

    def install(self, kind: str, requirement: str, *, editable: bool = False) -> dict[str, Any]:
        interpreter = self._ensure_venv(kind)
        argv = [str(interpreter), "-m", "pip", "install", "--upgrade"]
        if not editable and Path(requirement).expanduser().exists():
            argv.append("--force-reinstall")
        if editable:
            argv.append("--editable")
        argv.append(requirement)
        self._run(argv, purpose=f"install hive-ide into the {kind} environment")
        handshake = inspect_interpreter(interpreter)
        return {
            "kind": kind,
            "interpreter": handshake["interpreter"],
            "version": handshake["package_version"],
            "editable": editable,
        }

    def setup(
        self, *, stable_spec: str, dev_checkout: str | Path | None
    ) -> dict[str, Any]:
        checkout = None
        if dev_checkout is not None:
            checkout = Path(dev_checkout).expanduser().resolve()
            if not (checkout / "pyproject.toml").is_file():
                raise UsageError(f"Dev checkout has no pyproject.toml: {checkout}")
        stable = self.install("stable", stable_spec)
        dev = None
        if checkout is not None:
            dev = self.install("dev", str(checkout), editable=True)
        return {"environment_home": str(self.home), "stable": stable, "dev": dev}
