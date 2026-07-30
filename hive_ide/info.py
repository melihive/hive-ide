"""Small package-native session and shortcut popups."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import DEFAULT_KEYS, DEFAULT_SIDEBAR
from .sidebar_grid import SidebarGrid
from .store import StateStore


def _clean_meta(value: str) -> str:
    return value.strip().strip("*").strip()


def _driver_icon(snapshot: dict, driver: str) -> str:
    sidebar = snapshot.get("sidebar") or {}
    icons = ((sidebar.get("icons") or {}).get("drivers") or {})
    defaults = DEFAULT_SIDEBAR["icons"]["drivers"]
    return icons.get(driver) or defaults.get(driver) or defaults["default"]


def _plan_title(path: str | None, working_dir: str | None) -> str | None:
    if not path:
        return None
    plan = Path(path)
    candidates = [plan]
    if not plan.is_absolute() and working_dir:
        candidates.insert(0, Path(working_dir) / plan)
    for candidate in candidates:
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines()[:25]:
                stripped = line.strip()
                if stripped.startswith("# "):
                    return stripped[2:].strip()
        except OSError:
            continue
    return None


def _plan_meta(path: str | None, working_dir: str | None) -> tuple[str | None, str | None]:
    if not path:
        return None, None
    plan = Path(path)
    candidates = [plan]
    if not plan.is_absolute() and working_dir:
        candidates.insert(0, Path(working_dir) / plan)
    for candidate in candidates:
        try:
            started = None
            kind = None
            done = 0
            total = 0
            for line in candidate.read_text(encoding="utf-8").splitlines()[:120]:
                stripped = line.strip()
                if stripped.startswith("> - **Started:**"):
                    started = _clean_meta(stripped.split(":", 1)[1])
                elif stripped.startswith("> - **Kind:**"):
                    kind = _clean_meta(stripped.split(":", 1)[1])
                elif stripped.startswith("- ["):
                    total += 1
                    if stripped.startswith("- [x]") or stripped.startswith("- [X]"):
                        done += 1
            progress = f"{round(done * 100 / total)}%" if total else None
            status = kind or started
            return status, progress
        except OSError:
            continue
    return None, None


def _box(lines: list[str]) -> list[str]:
    width = max(SidebarGrid.cell_width(line) for line in lines) if lines else 0
    top = f"╭{'─' * (width + 2)}╮"
    bottom = f"╰{'─' * (width + 2)}╯"
    return [top, *[f"│ {SidebarGrid.pad(line, width)} │" for line in lines], bottom]


def _row(icon: str, label: str, value: str) -> str:
    return f"{icon} {label:<9}{value}"


def _card(record: dict, snapshot: dict, error: dict | None = None) -> list[str]:
    driver = (record.get("driver") or {}).get("id") or "unknown"
    working_dir = record.get("working_dir") or ""
    folder = Path(working_dir).name if working_dir else "unknown"
    plan = (record.get("plan") or {}).get("path")
    source = record.get("source") or {}
    title = _plan_title(plan, working_dir)
    plan_status, plan_progress = _plan_meta(plan, working_dir)
    source_label = f"{source.get('kind') or 'unknown'} {source.get('version') or ''}".rstrip()
    rows = [
        f"Hive IDE  {record.get('name') or record['id']}",
        "",
        _row(_driver_icon(snapshot, driver), "agent", driver),
        _row("📁", "folder", folder),
        _row("🧬", "source", source_label),
        _row("🆔", "id", record["id"]),
    ]
    if plan:
        rows.extend(
            [
                "",
                _row("📝", "plan", title or Path(plan).stem),
                _row("  ", "status", " · ".join(
                    part for part in [plan_status or "?", plan_progress] if part
                )),
                _row("  ", "path", plan),
            ]
        )
    else:
        rows.extend(["", _row("· ", "plan", "(none)")])
    rows.append(_row("🕐", "active", record.get("last_active") or "?"))
    if error:
        rows.extend(
            [
                "",
                _row("!", "error", error.get("summary") or "session error"),
                _row(" ", "source", error.get("component") or "unknown"),
            ]
        )
        if error.get("recovery"):
            rows.append(_row(" ", "fix", error["recovery"]))
    return _box(rows)


def _keys(snapshot: dict) -> list[str]:
    configured = (snapshot.get("keys") or {}).get("bindings") or {}
    bindings = {**DEFAULT_KEYS["bindings"], **configured}
    labels = {
        "next": "next session",
        "previous": "previous session",
        "sidebar": "focus sidebar",
        "chat": "focus chat",
        "plan": "focus plan",
        "agent": "change agent",
        "options": "session options",
        "card": "session card",
        "jump_plan": "first unfinished plan task",
        "reset": "reset columns",
        "help": "shortcut map",
        "new": "new session",
        "error": "latest error",
    }
    lines = ["Hive IDE shortcuts   (under your tmux prefix)", ""]
    width = max(len(key or "") for key in bindings.values())
    lines.extend(
        f"<prefix> {key:<{width}}   {labels[action]}"
        for action, key in bindings.items()
        if key
    )
    return _box(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hive_ide.info")
    parser.add_argument("--kind", choices=("card", "keys"), required=True)
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--workspace-key", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)
    store = StateStore(args.state_home, args.workspace_key)
    snapshot = store.read_path(store.config_snapshot_path()) or {}
    if args.kind == "card":
        record = store.find_session(args.session_id)
        error = store.read("errors", args.session_id)
        lines = (
            _card(record, snapshot, error)
            if record
            else _box(["Session record not found."])
        )
    else:
        lines = _keys(snapshot)
    print("\n  " + "\n  ".join(lines))
    if sys.stdin.isatty():
        print("\n  (press Enter to close)", end="", flush=True)
        try:
            sys.stdin.readline()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
