"""Optional host integration contracts.

The standalone core uses these neutral defaults. A host may provide richer behavior
to foreground commands; spawned panes receive only the resulting JSON state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Workspace:
    key: str
    working_dir: str
    metadata: dict[str, object]


class WorkspaceAdapter(Protocol):
    def resolve(self, directory: Path) -> Workspace: ...


class PlanAdapter(Protocol):
    def resolve(self, reference: str | None, workspace: Workspace) -> dict[str, object]: ...

    def active_task(self, plan: dict[str, object]) -> str | None: ...


class CommandSurface(Protocol):
    def before_create(self, workspace: Workspace, name: str) -> None: ...

    def after_archive(self, session: dict[str, object]) -> None: ...


class DefaultWorkspaceAdapter:
    def resolve(self, directory: Path) -> Workspace:
        resolved = str(directory.expanduser().resolve())
        return Workspace(key=resolved, working_dir=resolved, metadata={})


class DefaultPlanAdapter:
    def resolve(self, reference: str | None, workspace: Workspace) -> dict[str, object]:
        return {"path": reference, "active_task": None}

    def active_task(self, plan: dict[str, object]) -> str | None:
        value = plan.get("active_task")
        return value if isinstance(value, str) else None


class DefaultCommandSurface:
    def before_create(self, workspace: Workspace, name: str) -> None:
        return None

    def after_archive(self, session: dict[str, object]) -> None:
        return None
