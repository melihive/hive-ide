#!/usr/bin/env python3
"""Elect the one ide client that sizes the frame — mirroring's enforcement point.

Invoked by the frame's `client-attached`, `client-detached` and
`client-focus-in` tmux hooks:

    python -m hive_ide.clients --state-home <state> --workspace-key <repo> \
        --tmux-socket <socket> [--relayout-on-change]

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

`--relayout-on-change` exists for the focus hook. Attach and detach chain a
relayout in the tmux hook itself, unconditionally, because the client set
changing always changes the frame. Focus does not: it fires on every alt-tab, so
relaying out every time is the hook storm this package removed in 1.0.86. Here
the election decides -- the frame is re-laid out only when the controller
actually moved, which is also the only case where the geometry can be wrong.
Promoting a wide screen that a narrow one had zoomed for mobile needs it: tmux
preserves a zoom across an automatic resize, so the window grows and stays
zoomed to one pane until something restores its columns.

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
    def elect(
        state_home: Path,
        workspace_key: str,
        socket: str,
        *,
        relayout_on_change: bool = False,
    ) -> str | None:
        """Elect the controller; returns it, or None when nothing is attached."""
        store = StateStore(state_home, workspace_key)
        frame = Frame(store, socket=socket)
        if not frame.exists():
            return None
        before = (
            [c["client"] for c in frame.attached_clients() if c["controller"]]
            if relayout_on_change
            else None
        )
        controller = frame.elect_controller()
        if before is not None and controller is not None and before != [controller]:
            IdeClients._relayout(store, socket)
        return controller

    @staticmethod
    def _relayout(store: StateStore, socket: str) -> None:
        """Snap every window back onto the new controller's geometry.

        In-process rather than a second interpreter: this runs from a tmux hook
        and the whole point of the change-gate above is that the hook stays cheap.

        EVERY window, not just the focused one -- a controller swap changes the
        canonical geometry for all of them, and a window nobody is looking at
        keeps the old size until it is next shown. Imported here rather than at
        module scope for the same reason: the settled path, which is nearly every
        firing, should not pay to load it.

        After `elect_controller` returns, never during it: both take the
        workspace mutation lock.
        """
        from .relayout import IdeRelayout

        IdeRelayout.main(
            [
                "hive_ide.relayout",
                "--state-home",
                str(store.home),
                "--workspace-key",
                str(store.workspace_key),
                "--tmux-socket",
                socket,
                "--mode",
                "snap",
            ]
        )

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        args = argv if argv is not None else sys.argv[1:]
        try:
            parser = argparse.ArgumentParser(add_help=False)
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--tmux-socket", required=True)
            parser.add_argument("--relayout-on-change", action="store_true")
            parsed = parser.parse_args(args)
            IdeClients.elect(
                Path(parsed.state_home),
                parsed.workspace_key,
                parsed.tmux_socket,
                relayout_on_change=parsed.relayout_on_change,
            )
        except Exception:  # noqa: BLE001 — a tmux hook must never break the frame
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(IdeClients.main())
