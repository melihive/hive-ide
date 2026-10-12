#!/usr/bin/env python3
"""Repaint a session's sidebar the moment its window is switched to.

Invoked by the ide frame's `session-window-changed` tmux hook, alongside `seen`
and `relayout --mode=adopt`:

    python -m hive_ide.wake --state-home <state> --workspace-key <repo> \
        --session-id <id> --tmux-socket <socket>

Only the DISPLAYED window runs a sidebar process: the render loop returns as soon
as its window stops being current, and the keep-alive shell then re-probes just
once a second. A backgrounded pane therefore keeps the frame it last painted — and
the list is ordered by activity, which changes every time any agent takes a turn.
So the frame goes out of date while you are away, and switching back showed the
order from the last time you looked at that window. Measured at ~1500ms before the
keep-alive repainted, which is long enough to read as the sidebar sorting itself
differently for every session. Waking it on the switch cuts that to ~40ms.

The respawn goes through `Frame.respawn_role_pane`, never a bare tmux
`respawn-pane`, and that is the whole point of this module being more than one
line. That helper resolves the pane by its `@hive_ide_pane` ROLE (pane order
drifts, and `relayout --mode=adopt` re-sorts panes on this very hook), spawns it
with THIS record's `HIVE_IDE_*` variables instead of the tmux session environment
— which holds whichever identity the frame's first window happened to leave there
— honours an outstanding pane lease, and refuses any pane it cannot prove is not
the caller's own.

Stdlib only, fail-open, fast — it runs on every window switch and must never make
switching feel slow or wedge the frame.
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

from .frame import Frame
from .store import StateStore


class IdeWake:
    """Respawn one session's sidebar pane so it repaints without waiting a tick."""

    ROLE = "sidebar"

    @staticmethod
    def wake(skill_dir: Path, repo: str, session_id: str, socket: str) -> bool:
        """True when the sidebar pane was respawned."""
        store = StateStore(skill_dir, repo)
        record = store.find_session(session_id)
        if record is None:
            return False
        frame = Frame(store, socket=socket)
        pane_id = (frame.role_panes(record["id"]) or {}).get(IdeWake.ROLE)
        if not pane_id:
            return False      # no sidebar role in this window: nothing to wake
        return frame.respawn_role_pane(record, IdeWake.ROLE, pane_id)

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        args = argv if argv is not None else sys.argv[1:]
        try:
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--session-id", required=True)
            parser.add_argument("--tmux-socket", required=True)
            parsed = parser.parse_args(args)
            IdeWake.wake(
                Path(parsed.state_home),
                parsed.workspace_key,
                parsed.session_id,
                parsed.tmux_socket,
            )
        except Exception:  # noqa: BLE001 — a tmux hook must never break the frame
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(IdeWake.main())
