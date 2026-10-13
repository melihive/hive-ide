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
order from the last time you looked at that window.

**This module used to respawn the pane, and that was the wrong fix.** Respawning
makes the pane repaint, but `respawn-pane -k` CLEARS it first and then waits on a
shell probe and an interpreter, so every window switch showed an empty sidebar for
~400ms. Measured on one machine: 1492ms of stale order before, 401ms of blank
after. The test written at the time measured only how long the stale ORDER stayed
readable, and a blank pane contains no order at all — so it scored the blank as a
near-perfect pass. Both numbers were bad; the second was just bad in a way the
test could not see.

So now it PAINTS instead. It renders the canonical frame for that session and
writes it to the pane's tty, which is where the pane's own process writes, so tmux
renders it as that pane's output — drawn over the stale frame, clearing nothing,
starting nothing in the pane. The pane's own sidebar still comes up on its usual
schedule and repaints the same bytes.

Stdlib only, fail-open, fast — it runs on every window switch and must never make
switching feel slow or wedge the frame.
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

from .frame import Frame
from .sidebar import IdeSidebar
from .store import StateStore


class IdeWake:
    """Paint one session's sidebar pane so it shows the right frame at once."""

    ROLE = "sidebar"

    @staticmethod
    def wake(skill_dir: Path, repo: str, session_id: str, socket: str) -> bool:
        """True when the sidebar pane was painted."""
        store = StateStore(skill_dir, repo)
        record = store.find_session(session_id)
        if record is None:
            return False
        frame = Frame(store, socket=socket)
        # By ROLE, never by index: pane order drifts, and `relayout --mode=adopt`
        # re-sorts panes on this very hook, so an index-based target races it.
        pane_id = (frame.role_panes(record["id"]) or {}).get(IdeWake.ROLE)
        if not pane_id:
            return False      # no sidebar role in this window: nothing to paint
        tty = frame.pane_tty(pane_id)
        if not tty:
            return False      # cannot address the pane; leave the stale frame alone
        return IdeSidebar.paint_tty(skill_dir, repo, record["id"], tty)

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
