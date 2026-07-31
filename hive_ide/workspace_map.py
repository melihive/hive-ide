"""Read-only workspace/session map."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION


class WorkspaceMap:
    """Build a local map of every workspace known to the state store."""

    def __init__(self, state_home: str | Path):
        self.state_home = Path(state_home).expanduser().resolve()

    def build(
        self,
        *,
        root: str | Path | None = None,
        workspace: str | Path | None = None,
        archived: bool = False,
    ) -> dict[str, Any]:
        root_path = Path(root).expanduser().resolve() if root else None
        workspace_key = str(Path(workspace).expanduser().resolve()) if workspace else None
        items: list[dict[str, Any]] = []
        for workspace_dir in sorted((self.state_home / "workspaces").glob("*")):
            if not workspace_dir.is_dir():
                continue
            item = self._workspace(workspace_dir, archived=archived)
            key = item.get("workspace_key")
            if workspace_key and key != workspace_key:
                continue
            if root_path and (
                not isinstance(key, str) or not self._is_relative_to(Path(key), root_path)
            ):
                continue
            items.append(item)
        items.sort(key=lambda item: str(item.get("workspace_key") or "").casefold())
        return {
            "state_home": str(self.state_home),
            "root": str(root_path) if root_path else None,
            "workspace": workspace_key,
            "workspaces": items,
            "totals": self._totals(items),
        }

    def render(
        self,
        *,
        root: str | Path | None = None,
        workspace: str | Path | None = None,
        archived: bool = False,
    ) -> str:
        data = self.build(root=root, workspace=workspace, archived=archived)
        lines = [f"hive-ide workspaces ({data['totals']['workspaces']})"]
        if data["root"]:
            lines.append(f"root: {data['root']}")
        if data["workspace"]:
            lines.append(f"workspace: {data['workspace']}")
        if not data["workspaces"]:
            lines.append("  no workspaces found")
            return "\n".join(lines)
        for workspace_item in data["workspaces"]:
            marker = "!" if not workspace_item["exists"] else "-"
            label = workspace_item["label"]
            key = workspace_item["workspace_key"] or "<unknown>"
            active = workspace_item["counts"]["active"]
            archived_count = workspace_item["counts"]["archived"]
            missing = workspace_item["counts"]["missing_dirs"]
            suffix = f"{active} active"
            if archived:
                suffix += f", {archived_count} archived"
            if missing:
                suffix += f", {missing} missing-dir"
            lines.append(f"{marker} {label} ({key}) [{suffix}]")
            for session in workspace_item["sessions"]:
                session_marker = "!" if not session["working_dir_exists"] else " "
                driver = session["driver"] or "unknown"
                source = session["source"] or "unknown"
                state = "archived" if session["archived"] else "active"
                lines.append(
                    f"  {session_marker}- {session['name']} "
                    f"[{driver}, {source}, {state}] {session['id'][:8]}"
                )
                lines.append(f"     cwd: {session['working_dir']}")
                if session.get("plan"):
                    lines.append(f"     plan: {session['plan']}")
        return "\n".join(lines)

    def _workspace(self, workspace_dir: Path, *, archived: bool) -> dict[str, Any]:
        active_sessions = self._records(workspace_dir / "sessions", archived=False)
        archived_sessions = self._records(workspace_dir / "archive", archived=True)
        all_sessions = active_sessions + (archived_sessions if archived else [])
        workspace_key = self._workspace_key(active_sessions, archived_sessions)
        path = Path(workspace_key) if isinstance(workspace_key, str) else None
        sessions = [self._session(record) for record in all_sessions]
        return {
            "hash": workspace_dir.name,
            "workspace_key": workspace_key,
            "label": path.name if path and path.name else workspace_dir.name,
            "exists": bool(path and path.is_dir()),
            "sessions": sessions,
            "counts": {
                "active": len(active_sessions),
                "archived": len(archived_sessions),
                "shown": len(sessions),
                "missing_dirs": sum(
                    1 for session in sessions if not session["working_dir_exists"]
                ),
            },
        }

    def _records(self, directory: Path, *, archived: bool) -> list[dict[str, Any]]:
        records = []
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("schema_version") != SCHEMA_VERSION:
                continue
            data["_archived"] = archived
            records.append(data)
        records.sort(key=lambda item: item.get("last_active") or "", reverse=True)
        return records

    @staticmethod
    def _workspace_key(
        active_sessions: list[dict[str, Any]], archived_sessions: list[dict[str, Any]]
    ) -> str | None:
        for record in [*active_sessions, *archived_sessions]:
            key = record.get("workspace_key")
            if isinstance(key, str) and key:
                return key
        return None

    @staticmethod
    def _session(record: dict[str, Any]) -> dict[str, Any]:
        working_dir = record.get("working_dir")
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        driver = record.get("driver") if isinstance(record.get("driver"), dict) else {}
        plan = record.get("plan") if isinstance(record.get("plan"), dict) else {}
        return {
            "id": str(record.get("id") or ""),
            "name": str(record.get("name") or record.get("id") or "<unnamed>"),
            "driver": driver.get("id"),
            "source": " ".join(
                value
                for value in (source.get("kind"), source.get("version"))
                if isinstance(value, str) and value
            )
            or None,
            "archived": bool(record.get("_archived")),
            "last_active": record.get("last_active"),
            "working_dir": working_dir,
            "working_dir_exists": bool(
                isinstance(working_dir, str) and Path(working_dir).is_dir()
            ),
            "plan": plan.get("path"),
        }

    @staticmethod
    def _totals(items: list[dict[str, Any]]) -> dict[str, int]:
        return {
            "workspaces": len(items),
            "active_sessions": sum(item["counts"]["active"] for item in items),
            "archived_sessions": sum(item["counts"]["archived"] for item in items),
            "missing_dirs": sum(item["counts"]["missing_dirs"] for item in items),
        }

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root)
            return True
        except ValueError:
            return False
