#!/usr/bin/env python3
"""Instant next/prev ide-session switching for the ide keybindings.

Bound to `<prefix> n` / `<prefix> p`. Stdlib only, for the same reason as
`ide_sidebar.py`: booting the foreground CLI on every keypress would be too expensive.
It reads the normalized registry directly.

usage: python3 -I ide_nav.py <skill_dir> <repo> <socket> next|prev
"""
from __future__ import annotations

import json
import argparse
import subprocess
import sys
import time
from pathlib import Path

from .state_compat import StateIO


class IdeNav:
    """One keypress: pick the neighbouring ide session and select its window."""

    # A rapid n/p burst reuses a snapshot of the order so a live agent bumping its
    # session to the top mid-walk can't re-sort the list under you. The snapshot
    # refreshes once the burst lapses or the set of open windows changes.
    BURST_SECONDS = 8

    @staticmethod
    def _tmux(socket: str, args: list[str]) -> str:
        r = subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    @staticmethod
    def _snapshot_path(skill_dir: Path, repo: str) -> Path:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in repo)
        return skill_dir / ".state_local" / "ide_nav" / f"{safe}.json"

    @staticmethod
    def _order(skill_dir: Path, repo: str, open_session_ids: list[str]) -> list[str]:
        """The nav order = the SIDEBAR's order (activity-sorted sessions, newest-active
        first — exactly what `StateIO.list_sessions` returns and the sidebar renders),
        filtered to the OPEN tmux windows. So pressing n always moves to the next session
        DOWN the sidebar, matching what you see.

        Snapshotted for a short burst so a live agent bumping its session to the top mid-
        walk can't re-sort under you (the Phase 30 trap that first pushed n/p onto window
        order). The snapshot is invalidated the moment the set of open windows changes, so
        a new/closed session is picked up at once."""
        openset = set(open_session_ids)
        snap = IdeNav._snapshot_path(skill_dir, repo)
        try:
            c = json.loads(snap.read_text(encoding="utf-8"))
            if (time.time() - float(c.get("at", 0)) < IdeNav.BURST_SECONDS
                    and set(c.get("order") or []) == openset):
                return list(c["order"])
        except (OSError, ValueError, TypeError):
            pass
        # Fresh: the sidebar's activity order, filtered to tagged protocol windows.
        # Untagged windows have no stable session identity and do not belong in this order.
        ordered = [
            s.get("id")
            for s in StateIO.list_sessions(skill_dir, repo)
            if s.get("id") in openset
        ]
        try:
            snap.parent.mkdir(parents=True, exist_ok=True)
            snap.write_text(json.dumps({"order": ordered, "at": time.time()}),
                            encoding="utf-8")
        except OSError:
            pass
        return ordered

    @staticmethod
    def main(argv: list[str]) -> int:
        raw = argv[1:]
        if "--state-home" in raw:
            parser = argparse.ArgumentParser(prog="python -m hive_ide.nav")
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--session-id", required=True)
            parser.add_argument("--tmux-socket", required=True)
            parser.add_argument("--direction", choices=("next", "prev"), required=True)
            parsed = parser.parse_args(raw)
            skill_dir, repo = Path(parsed.state_home), parsed.workspace_key
            socket, direction = parsed.tmux_socket, parsed.direction
            current_session_id = parsed.session_id
        else:
            if len(argv) != 5 or argv[4] not in ("next", "prev"):
                sys.stderr.write("usage: ide_nav.py <state_home> <workspace> <socket> next|prev\n")
                return 2
            skill_dir, repo, socket, direction = Path(argv[1]), argv[2], argv[3], argv[4]
            current_session_id = IdeNav._tmux(
                socket, ["display-message", "-p", "#{@hive_ide_session_id}"]
            )
            if not current_session_id:
                current_name = IdeNav._tmux(
                    socket, ["display-message", "-p", "#{window_name}"]
                )
                current = StateIO.read(skill_dir, repo, current_name)
                current_session_id = (current or {}).get("id") or ""
        raw = IdeNav._tmux(
            socket,
            [
                "list-windows",
                "-a",
                "-F",
                "#{window_id}\t#{@hive_ide_session_id}",
            ],
        )
        windows = {}
        for line in raw.splitlines():
            window_id, _, session_id = line.partition("\t")
            if window_id and session_id:
                windows[session_id] = window_id
        if not windows:
            return 0
        order = IdeNav._order(skill_dir, repo, list(windows)) or list(windows)
        # Land on the pane you were already using: switching from the agent should keep
        # you in the agent, from the sidebar keep you browsing.
        current_window = windows.get(current_session_id)
        pane = (
            IdeNav._tmux(
                socket,
                ["display-message", "-t", current_window, "-p", "#{pane_index}"],
            )
            if current_window
            else "0"
        ) or "0"
        idx = order.index(current_session_id) if current_session_id in order else 0
        target_session_id = order[(idx + (1 if direction == "next" else -1)) % len(order)]
        target = windows[target_session_id]
        IdeNav._tmux(socket, ["select-window", "-t", target])
        IdeNav._tmux(socket, ["select-pane", "-t", f"{target}.{pane}"])
        return 0


if __name__ == "__main__":
    sys.exit(IdeNav.main(sys.argv))
