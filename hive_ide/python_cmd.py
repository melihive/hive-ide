"""Internal Python command construction for hive-ide helper modules."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Iterable


class PythonCommand:
    """Build internal `python -m hive_ide...` commands in one place."""

    @staticmethod
    def module_argv(
        module: str,
        args: Iterable[str] = (),
        *,
        python: str | None = None,
    ) -> list[str]:
        return [python or sys.executable, "-m", f"hive_ide.{module}", *args]

    @classmethod
    def module_command(
        cls,
        module: str,
        args: Iterable[str] = (),
        *,
        python: str | None = None,
    ) -> str:
        return shlex.join(cls.module_argv(module, args, python=python))

    @classmethod
    def cli_argv(
        cls, args: Iterable[str] = (), *, python: str | None = None
    ) -> list[str]:
        return cls.module_argv("cli", args, python=python)
