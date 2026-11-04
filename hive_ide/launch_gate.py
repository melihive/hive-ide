#!/usr/bin/env python3
"""Hold a deferred agent launch until its turn, then exec the driver.

`open` builds every window in the frame, and every non-sleeping window launches
its driver. On a workspace that has grown, that means the whole fleet of agents
starts in the same second: measured on a 17-session workspace, 14
`claude --resume` over 438 MB of transcript plus 28 Hive session-start hooks (one
Hive-runtime call measured at 1.55s alone, fourteen at once at 8.61s), all
competing while the user waits on the ONE window they actually switched to. The
frame was on screen in nine seconds and unusable for a minute.

So a deferred launch waits. Each gated pane is given a `--delay` by `Frame.open`,
which staggers the queue in batches; the pane the user lands on is never gated at
all, so the window that matters starts instantly. **Selecting a gated window
jumps its queue**: the gate watches its own window and execs the moment that
window becomes the active one, so a wait is never something the user can be stuck
behind — the worst case is one poll interval.

Deliberately dependency-free and fail-open. It stands between the user and their
agent, so every failure mode — an unreadable tmux, a missing environment, a bad
delay, a signal — ends in the driver starting anyway. A gate that could refuse to
exec would be a way to lose an agent, and no stagger is worth that.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

__all__ = ["IdeLaunchGate", "PROCESS_MARKER", "main"]

#: How a gate that has not released yet appears in a process table. `verify`
#: needs it to tell a pane that is WAITING from a pane whose agent died: both are
#: a pane with no driver under it. Kept here, as a plain string, so this module
#: still imports nothing but the standard library — it is the first thing that
#: runs in an agent pane, and every import is paid once per pane.
PROCESS_MARKER = "hive_ide.launch_gate"


class IdeLaunchGate:
    """Wait out a deferred launch's turn, or the user's arrival, whichever first."""

    #: How often a waiting pane re-checks whether its window became active. This
    #: is the user's worst-case wait when they switch to a queued window, so it is
    #: short; it costs one tmux query per waiting pane per tick, and the number of
    #: waiters only ever shrinks.
    POLL_SECONDS = 0.5

    #: A delay this long or longer is treated as a mistake and ignored. The gate
    #: exists to spread a burst over seconds, not to park an agent; without a cap,
    #: a bad `--delay` (a unit mix-up, a corrupted command) would strand a pane.
    MAX_DELAY_SECONDS = 120.0

    def __init__(
        self,
        *,
        socket: str | None = None,
        pane: str | None = None,
        sleep=time.sleep,
        now=time.monotonic,
        run=subprocess.run,
    ):
        self.socket = socket or os.environ.get("HIVE_IDE_TMUX_SOCKET") or ""
        self.pane = pane or os.environ.get("TMUX_PANE") or ""
        self._sleep = sleep
        self._now = now
        self._run = run

    def window_is_active(self) -> bool:
        """True when this pane's window is the one tmux is displaying.

        Unknown is NOT active: a tmux that cannot be read must not be taken as the
        user's arrival, or every gated pane would start at once again — the exact
        burst the gate exists to prevent. The delay still bounds the wait, so an
        unreadable tmux costs a stagger, never a launch.
        """
        if not self.socket or not self.pane:
            return False
        try:
            result = self._run(
                [
                    "tmux",
                    "-L",
                    self.socket,
                    "display-message",
                    "-p",
                    "-t",
                    self.pane,
                    "#{window_active}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:
            return False
        if getattr(result, "returncode", 1) != 0:
            return False
        return (result.stdout or "").strip() == "1"

    def will_wait(self, delay) -> bool:
        """True when `delay` is a real wait — the only case worth telling the user about."""
        return self._delay_seconds(delay) > 0

    @classmethod
    def _delay_seconds(cls, delay) -> float:
        """`delay` as seconds, or 0 for anything that is not a sane wait.

        A bad value resolves to "do not wait" rather than to an error: the gate's
        job is to start the driver, and an unparsable delay is a reason to start it
        sooner, never a reason to fail.
        """
        try:
            seconds = float(delay)
        except (TypeError, ValueError):
            return 0.0
        if not seconds > 0 or seconds >= cls.MAX_DELAY_SECONDS:
            return 0.0
        return seconds

    def wait(self, delay: float) -> str:
        """Block until this launch's turn. Returns why it stopped waiting.

        `selected` means the user switched to this window and the queue was
        jumped; `queued` means the delay ran out; `immediate` means there was
        nothing to wait for.
        """
        seconds = self._delay_seconds(delay)
        if not seconds:
            return "immediate"
        deadline = self._now() + seconds
        while True:
            if self.window_is_active():
                return "selected"
            remaining = deadline - self._now()
            if remaining <= 0:
                return "queued"
            self._sleep(min(self.POLL_SECONDS, remaining))


def main(argv: list[str] | None = None) -> int:
    """Wait, then REPLACE this process with the driver command.

    Never returns on success: the pane's process has to become the driver itself,
    so the frame's pane inspection, the pane lease and every hook see exactly what
    they would have seen with no gate in front of them.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--delay", default="0")
    parser.add_argument("--label", default=os.environ.get("HIVE_IDE_SESSION") or "")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("hive-ide: the launch gate was given no command to run.", file=sys.stderr)
        return 2
    try:
        gate = IdeLaunchGate()
        if gate.will_wait(args.delay):
            # One line, so a queued pane is never a blank pane the user has to
            # guess about, and it says the way out.
            label = args.label or "this session"
            print(
                f"hive-ide: {label} is queued to start, so the window you opened "
                "is not competing with the rest of the frame.\n"
                "          Select this window to start it now.",
                flush=True,
            )
        gate.wait(args.delay)
    except (Exception, KeyboardInterrupt):
        # Fail open, and `KeyboardInterrupt` explicitly: a user who hits Ctrl-C
        # in a queued pane means "start it now", not "throw the agent away" —
        # and it is not an `Exception`, so it used to escape this handler and
        # close the pane.
        pass
    os.execvp(command[0], command)
    return 1  # unreachable: execvp either replaces this process or raises


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
