#!/usr/bin/env python3
"""Instant next/prev ide-session switching for the ide keybindings.

Bound to `<prefix> n` / `<prefix> p`. Stdlib only, for the same reason as
`ide_sidebar.py`: booting the foreground CLI on every keypress would be too expensive.
It reads the normalized registry directly.

usage: python -m hive_ide.nav <state_home> <repo> <socket> next|prev
"""
from __future__ import annotations

import json
import argparse
import subprocess
import sys
import time
from pathlib import Path

from .state_compat import StateIO


class _PrepaintOverBudget(BaseException):
    """The pre-switch repaint ran past its budget.

    Deliberately NOT an `Exception`. `TimeoutError` is a subclass of `OSError`,
    and the renderer's own handlers catch `OSError` (the git probes) and
    `Exception` (the sidebar provider wrapper) — so a `TimeoutError` raised from
    the alarm was swallowed inside the renderer, which consumed the one-shot
    timer and let every probe after it run unbounded. Measured: a 50ms budget
    still took 351ms. Deriving from `BaseException` means nothing on the way out
    catches it by accident.
    """


class IdeNav:
    """One keypress: pick the neighbouring ide session and select its window."""

    # A rapid n/p burst reuses a snapshot of the order so a live agent bumping its
    # session to the top mid-walk can't re-sort the list under you. The snapshot
    # refreshes once the burst lapses or the set of open windows changes.
    BURST_SECONDS = 8

    #: Ceiling on the pre-switch sidebar repaint. Generous next to the ~40ms a
    #: 17-session frame measures across real git checkouts, and far below the
    #: seconds a stalled `git status` could otherwise cost a keypress.
    PREPAINT_BUDGET_SECONDS = 0.2

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
        # Paint the destination BEFORE revealing it.
        #
        # Only the displayed window runs a sidebar process, so a backgrounded
        # pane holds the frame it painted when you last left it -- and the list
        # is ordered by activity, which moves whenever any agent takes a turn.
        # The order is therefore stale on practically every switch, which is why
        # stepping through sessions flashed the wrong order each time.
        #
        # The `session-window-changed` hook already repaints (`wake`), but it
        # necessarily fires AFTER the switch and pays a cold interpreter start,
        # so the stale frame is on screen for that whole time: 61ms measured on
        # an idle machine, and far longer on a loaded frame with many sessions.
        # Painting here, in a process that is already running, costs ~6ms of
        # extra import plus ~7ms to render a 17-session sidebar -- and the switch
        # then reveals a pane that is already correct, so there is no stale frame
        # to race rather than a shorter one.
        #
        # Imported in the function, not at module scope, to keep the promise in
        # this module's docstring: importing `hive_ide.nav` stays stdlib-only and
        # cheap for callers that just want the ordering.
        #
        # BUDGETED, because rendering is not reliably fast. The frame carries
        # checkout decorations, and `git_status` shells out to git with a 2s
        # timeout per call and keeps no cache, so a handful of slow or contended
        # worktrees could stall a keypress for seconds. Measured here: ~40ms cold
        # across five real checkouts, which is fine — it is the tail that is not,
        # and navigation must never feel stuck. Over budget we simply abandon the
        # repaint and switch; the `session-window-changed` hook still repaints
        # exactly as it did before, so the worst case is the old behaviour.
        #
        # The whole frame is one `os.write` at the very end and the git probes
        # dominate the time before it, so the timer lands in the probes. If it
        # ever landed in the write, `paint_fd` reports the short write and the
        # hook's repaint lands immediately after.
        try:
            import signal

            from .wake import IdeWake

            def _over_budget(_signum, _stack):
                raise _PrepaintOverBudget()

            previous = signal.signal(signal.SIGALRM, _over_budget)
            signal.setitimer(signal.ITIMER_REAL, IdeNav.PREPAINT_BUDGET_SECONDS)
            try:
                IdeWake.wake(skill_dir, repo, target_session_id, socket)
            except _PrepaintOverBudget:
                pass
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)
        except Exception:  # noqa: BLE001 — a repaint must never block navigation
            pass
        IdeNav._tmux(socket, ["select-window", "-t", target])
        IdeNav._tmux(socket, ["select-pane", "-t", f"{target}.{pane}"])
        return 0


if __name__ == "__main__":
    sys.exit(IdeNav.main(sys.argv))
