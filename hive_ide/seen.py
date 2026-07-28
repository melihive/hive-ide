#!/usr/bin/env python3
"""Mark an ide session as SEEN — the sidebar's "unread" ack.

Invoked by the ide frame's `session-window-changed` tmux hook, which fires whenever
the repo workspace switches window:

    python -m hive_ide.seen <state_home> <repo> <window_name>

The cyan `waiting` dot is an UNREAD marker: an agent asked for you. Opening the
session is what answers it, so the dot must clear **and stay cleared** — not
merely hide while you happen to be standing there. Hiding it at render time has
no memory: switch away and it comes back, which reads as "you never looked".

Only `waiting` is cleared, and only to `idle`. `working` is a live status the
agent owns — stomping it would blank a busy dot the moment you glanced at the
window, and the agent would not rewrite it until its next turn boundary.

Event-driven on purpose: the alternative is every sidebar shelling out to tmux
once a tick to ask which window is active, which is a subprocess per pane per
1.5s forever to catch an event tmux already publishes.

Stdlib only, fail-open, fast — it runs on every window switch and must never
make switching feel slow or wedge the frame.
"""
from __future__ import annotations

import sys
import argparse
from datetime import datetime, timezone
from pathlib import Path

from .state_compat import StateIO
from .store import StateStore


class IdeSeen:
    """Clear an ide session's `waiting` status once its window is opened."""

    @staticmethod
    def mark(skill_dir: Path, repo: str, session_id: str) -> bool:
        """True when a `waiting` status was acked. Any other state is left alone."""
        store = StateStore(skill_dir, repo)
        with store.mutation_lock():
            rec = store.find_session(session_id)
            if rec is None:
                return False
            st = store.read("status", session_id)
            if not st or st.get("state") != "waiting":
                return False
            st["state"] = "idle"
            st["seen_ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            store.write("status", session_id, st)
            return True

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        args = argv if argv is not None else sys.argv[1:]
        if "--state-home" in args:
            try:
                parser = argparse.ArgumentParser(add_help=False)
                parser.add_argument("--state-home", required=True)
                parser.add_argument("--workspace-key", required=True)
                parser.add_argument("--session-id", required=True)
                parsed = parser.parse_args(args)
                IdeSeen.mark(
                    Path(parsed.state_home), parsed.workspace_key, parsed.session_id
                )
            except Exception:
                pass
            return 0
        if len(args) < 3:
            return 0  # fail-open: never wedge a window switch
        try:
            found = StateIO.find_by_identity(Path(args[0]), args[1], args[2])
            if found:
                IdeSeen.mark(Path(args[0]), args[1], found[2]["id"])
        except Exception:  # noqa: BLE001 — a tmux hook must never break the frame
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(IdeSeen.main())
