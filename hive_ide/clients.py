#!/usr/bin/env python3
"""Elect the one ide client that sizes the frame — mirroring's enforcement point.

Invoked by the frame's `client-attached` and `client-detached` tmux hooks:

    python -m hive_ide.clients --state-home <state> --workspace-key <repo> \
        --tmux-socket <socket>

A tmux session can carry several clients, and `window-size latest` sizes the
window to whichever was active last. Two clients of different sizes therefore
flip the window's geometry and reflow every pane as focus moves between them --
reported as the frame freezing, and "fixed" in 1.0.94 by attaching with `-d` so
only one client could ever survive.

That cured the symptom by forbidding the thing people wanted. The cause is the
extra VOTE, so `Frame.elect_controller` leaves every client attached and flags
all but the most recently active one `ignore-size`: they mirror the frame,
truncated to their own terminal, with no say in its geometry. One voter, many
screens.

Hooked rather than decided once at `open` because the client set changes behind
this package's back -- someone runs a plain `tmux attach`, or closes a terminal.
On detach the election promotes the surviving screen, so it takes its own size
back rather than inheriting the departed controller's.

The election reads only the client set, never "who triggered me", so the several
elections these hooks can run concurrently all reach the same answer instead of
fighting over it.

Stdlib only, fail-open, fast: it runs on every attach and detach and must never
wedge the frame or make attaching feel slow.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .frame import Frame
from .store import StateStore


class IdeClients:
    """Keep exactly one attached client in charge of the frame's geometry."""

    @staticmethod
    def elect(state_home: Path, workspace_key: str, socket: str) -> str | None:
        """Elect the controller; returns it, or None when nothing is attached."""
        store = StateStore(state_home, workspace_key)
        frame = Frame(store, socket=socket)
        if not frame.exists():
            return None
        return frame.elect_controller()

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        args = argv if argv is not None else sys.argv[1:]
        try:
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--tmux-socket", required=True)
            parsed = parser.parse_args(args)
            IdeClients.elect(
                Path(parsed.state_home), parsed.workspace_key, parsed.tmux_socket
            )
        except Exception:  # noqa: BLE001 — a tmux hook must never break the frame
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(IdeClients.main())
