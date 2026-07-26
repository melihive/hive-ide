"""Small package-native session and shortcut popups."""

from __future__ import annotations

import argparse
import sys

from .config import DEFAULT_KEYS
from .store import StateStore


def _card(record: dict) -> list[str]:
    driver = (record.get("driver") or {}).get("id") or "unknown"
    plan = (record.get("plan") or {}).get("path") or "not linked"
    source = record.get("source") or {}
    return [
        record.get("name") or record["id"],
        "",
        f"ID       {record['id']}",
        f"Driver   {driver}",
        f"Folder   {record.get('working_dir') or 'unknown'}",
        f"Plan     {plan}",
        f"Source   {source.get('kind') or 'unknown'} {source.get('version') or ''}".rstrip(),
    ]


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
        "card": "session card",
        "jump_plan": "first unfinished plan task",
        "reset": "reset columns",
        "help": "shortcut map",
        "new": "new session",
        "error": "latest error",
    }
    lines = ["Hive IDE shortcuts", ""]
    lines.extend(
        f"{key:>6}  {labels[action]}"
        for action, key in bindings.items()
        if key
    )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hive_ide.info")
    parser.add_argument("--kind", choices=("card", "keys"), required=True)
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--workspace-key", required=True)
    parser.add_argument("--session-id", required=True)
    args = parser.parse_args(argv)
    store = StateStore(args.state_home, args.workspace_key)
    if args.kind == "card":
        record = store.find_session(args.session_id)
        lines = _card(record) if record else ["Session record not found."]
    else:
        lines = _keys(store.read_path(store.config_snapshot_path()) or {})
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
