#!/usr/bin/env python3
"""Re-apply the ide frame's fixed columns across every window, responsively.

tmux rescales panes proportionally whenever the client resizes, which would drift the
sidebar/plan columns off their absolute widths. The ide session binds this to
`client-resized` so the columns snap back — and, when a window gets too narrow to hold
all three, it degrades in steps instead of crushing the agent:

    wide    W >= sw + apref + pw       [ sidebar | agent | plan  ]   plan at full width
    plan-   W >= sw + amin  + pmin     [ sidebar | agent | plan- ]   plan shrinks, agent kept
    mobile  anything narrower          [ focused column, zoomed  ]   one column, full window

The AGENT has priority for spare columns (see `_plan_width`): it is served `apref` before
the plan grows past its minimum. The plan is reference material — it should not squeeze
the pane you actually work in.

The old icon-"rail" band is GONE — mobile-zoom replaced it, because a 4-column sidebar
beside a squeezed plan is unusable on a phone. `rail_w` is still PARSED and ignored so
tmux hooks installed in a running server (which pass the old positional argv) keep
working; drop the slot only after a full cycle.

Pure Python (stdlib only, like `ide_nav.py`) so every installed package carries the
resize helper. Keeping it as a module also makes the isolated pane command portable.

usage: python3 ide_relayout.py <socket> <sidebar_w> <plan_w> [rail_w (ignored)] \
           [agent_min] [plan_min] [agent_pref] [mode] [state_file]
"""
from __future__ import annotations

import json
import argparse
import os
import subprocess
import sys
import time

from .layout import IdeLayout
from .store import StateStore



class IdeRelayout:
    """Snap the sidebar/plan columns back to fixed widths across every ide window."""

    @staticmethod
    def _tmux(socket: str, args: list[str]) -> str:
        r = subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    @staticmethod
    def _zoomed(socket: str, win: str) -> bool:
        return IdeRelayout._tmux(socket, ["display-message", "-p", "-t", f"{win}.1",
                                          "#{window_zoomed_flag}"]) == "1"

    @staticmethod
    def _set_zoom(socket: str, win: str, want: bool) -> None:
        # `resize-pane -Z` TOGGLES, so guard on the current flag — that is what stops
        # repeated resize events (tmux fires many) from flapping in and out of zoom.
        if IdeRelayout._zoomed(socket, win) != want:
            IdeRelayout._tmux(socket, ["resize-pane", "-t", f"{win}.1", "-Z"])

    @staticmethod
    def _plan_width(width: int, side: int, pw: int, pmin: int, amin: int, apref: int) -> int:
        """How wide the plan column gets — the AGENT has priority for the slack.

        The plan is reference material; the agent is where the work happens. So the agent
        is served its preferred width FIRST and the plan takes what is left, capped at its
        full width and floored at its minimum:

            plan = clamp(width - side - apref, pmin, pw)

        then the plan yields further if that would push the agent under its hard minimum.

        This reverses the original ladder, which pinned the agent at `amin` and handed ALL
        remaining space to the plan — so on any window between the floor and full width the
        agent sat crushed at its minimum while the plan ballooned. That is the "plan pane
        is too big on smaller screens" report, and it contradicted this module's own stated
        intent ("degrade in steps instead of crushing the agent").
        """
        # Geometry lives in ONE place (`ide_layout.IdeLayout`); this stays a thin shim so
        # the hook's positional argv keeps working while the math has a single owner.
        if (side, pw, pmin, amin, apref) == (IdeLayout.SIDEBAR_W, IdeLayout.PLAN_W,
                                             IdeLayout.PLAN_MIN, IdeLayout.AGENT_MIN,
                                             IdeLayout.AGENT_PREF):
            return IdeLayout.plan_width(width, side)
        plan = max(pmin, min(pw, width - side - apref))
        if width - side - plan < amin:        # never starve the agent below its minimum
            plan = max(pmin, width - side - amin)
        return plan

    # Circuit breaker. A layout hook that (directly or indirectly) triggers itself is
    # catastrophic, not merely slow: `after-resize-pane` once fed this script its own
    # `resize-pane` calls and spawned ~1000 processes that wedged the entire tmux server.
    # The hooks are now chosen so that cannot happen, but this is defence in depth — the
    # next person wiring a hook gets a bail-out instead of a dead machine.
    # Tuned to discriminate a LOOP from a legitimate burst, which is the whole difficulty:
    # dragging a terminal edge emits a flood of `client-resized` hooks, and those snaps are
    # legitimate. A threshold low enough to catch a burst would trip mid-drag and skip the
    # FINAL snap, leaving the columns at tmux's proportional widths — the breaker would
    # break the very thing it protects.
    #
    # The discriminator is DURATION, not instantaneous rate: a human drag stops after a
    # second or two, a self-feeding hook never stops. So the window is long and the count is
    # high — a drag cannot sustain 200 invocations across 30s, a runaway reaches it almost
    # immediately and is still caught long before it can wedge the server (the real incident
    # took minutes to become fatal).
    BREAKER_MAX = 200         # invocations allowed inside the window
    BREAKER_WINDOW = 30.0     # seconds — long on purpose; see above
    BREAKER_KEEP = 512        # ledger cap, so the file can't grow without bound

    @staticmethod
    def _breaker_hits(prev: list, now: float, window: float) -> list:
        """The invocation ledger pruned to `window` with this call appended.

        Split out as a pure function so the breaker is unit-testable without sleeping or
        touching the filesystem. Non-numeric junk in a corrupted ledger is dropped rather
        than raising — a bad file must never block a legitimate relayout.
        """
        kept = [t for t in prev if isinstance(t, (int, float)) and not isinstance(t, bool)
                and 0 <= now - t < window]
        return kept + [now]

    @staticmethod
    def _breaker_tripped(path: str, now: float | None = None) -> bool:
        """True when this script is being invoked far too often — i.e. something is feeding
        it. FAIL-OPEN by construction: no path, an unreadable ledger, or an unwritable dir
        all return False, because a broken breaker must never stop the layout working.
        """
        if not path:
            return False
        now = time.time() if now is None else now
        ledger = path + ".hits"
        try:
            os.makedirs(os.path.dirname(ledger) or ".", exist_ok=True)
            # APPEND-ONLY, one timestamp per line. A runaway fires many `run-shell -b`
            # relayouts CONCURRENTLY — the case this breaker exists for — so a
            # read-modify-write is the wrong shape: every process reads the same pre-limit
            # state and they all sail through (measured: 8 of 24 updates lost, even under
            # flock). A single short append has no update to lose, so every concurrent
            # caller is counted and the burst actually trips.
            with open(ledger, "a", encoding="utf-8") as fh:
                fh.write(f"{now}\n")
            with open(ledger, encoding="utf-8") as fh:
                raw = fh.read().split()
            stamps = []
            for tok in raw:
                try:
                    stamps.append(float(tok))
                except ValueError:
                    continue          # torn/corrupt line → skip it, never raise
            hits = [t for t in stamps if 0 <= now - t < IdeRelayout.BREAKER_WINDOW]
            # Bound the file. Racy by design: a lost prune costs a few stale lines, which
            # the window filter ignores anyway — correctness never depends on it.
            if len(stamps) > IdeRelayout.BREAKER_KEEP:
                try:
                    with open(ledger, "w", encoding="utf-8") as fh:
                        fh.write("".join(f"{t}\n" for t in hits[-IdeRelayout.BREAKER_KEEP:]))
                except OSError:
                    pass
            return len(hits) > IdeRelayout.BREAKER_MAX
        except (OSError, ValueError):
            return False                      # fail-open

    @staticmethod
    def _read_state(path: str) -> dict:
        """`{"win": <last active window id>, "side": int, "plan": int}` — best effort."""
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write_state(path: str, state: dict) -> None:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        except OSError:
            pass

    @staticmethod
    def _adopted(prev_side: int | None, prev_plan: int | None,
                 sw: int, plan_default: int, pmin: int, pw: int,
                 amin: int, width: int) -> tuple[int, int] | None:
        """Widths to propagate after a window switch, or None to leave the defaults alone.

        `prev_*` are the columns of the window you just LEFT — the only place a manual drag
        can have happened, which is what makes this unambiguous (no "odd one out" guessing).
        Adopt them only when they actually differ from the computed default and are still
        sane; a nonsense drag (plan below its minimum, agent starved) is ignored rather than
        propagated to every session.
        """
        if prev_side is None or prev_plan is None:
            return None
        if prev_side == sw and prev_plan == plan_default:
            return None                                  # nothing was dragged
        if not (pmin <= prev_plan <= pw):
            return None                                  # out of range — ignore
        if width - prev_side - prev_plan < amin:
            return None                                  # would starve the agent
        return prev_side, prev_plan

    @staticmethod
    def main(argv: list[str]) -> int:
        if "--state-home" in argv[1:]:
            parser = argparse.ArgumentParser(prog="python -m hive_ide.relayout")
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--tmux-socket", required=True)
            parser.add_argument("--mode", choices=("snap", "adopt"), default="snap")
            parsed = parser.parse_args(argv[1:])
            store = StateStore(parsed.state_home, parsed.workspace_key)
            return IdeRelayout.main(
                [
                    argv[0],
                    parsed.tmux_socket,
                    str(IdeLayout.SIDEBAR_W),
                    str(IdeLayout.PLAN_W),
                    "4",
                    str(IdeLayout.AGENT_MIN),
                    str(IdeLayout.PLAN_MIN),
                    str(IdeLayout.AGENT_PREF),
                    parsed.mode,
                    str(store.workspace_dir / "layout.json"),
                ]
            )
        if len(argv) < 4:
            return 0
        sock = argv[1]
        try:
            sw, pw = int(argv[2]), int(argv[3])
            int(argv[4]) if len(argv) > 4 else 4   # rail_w: parsed, unused (argv compat)
            amin = int(argv[5]) if len(argv) > 5 else 60
            pmin = int(argv[6]) if len(argv) > 6 else 40
            apref = int(argv[7]) if len(argv) > 7 else 90   # agent's preferred width
        except ValueError:
            return 0
        # mode: `snap` (client-resized) recomputes the defaults and RESETS every window —
        # a real terminal resize should land back on the canonical layout. `adopt`
        # (session-window-changed) carries a manual drag from the window you left to all
        # the others, so the sessions stay identical.
        # NOTE: this is deliberately NOT wired to `after-resize-pane`. That hook fires on
        # the very `resize-pane` calls below, so it re-entered itself and spawned ~1000
        # processes. `session-window-changed` cannot be triggered by a resize, so this
        # path is loop-free by construction. Do not "improve" it with a resize hook.
        mode = argv[8] if len(argv) > 8 else "snap"
        state_path = argv[9] if len(argv) > 9 else ""
        # Bail BEFORE touching any pane: if something is feeding this script, every
        # resize-pane below is more fuel. Stderr (not stdout) so a `run-shell -b` hook
        # doesn't paint the message over a pane.
        if IdeRelayout._breaker_tripped(state_path):
            sys.stderr.write(
                "ide_relayout: circuit breaker tripped — called more than "
                f"{IdeRelayout.BREAKER_MAX}x in {IdeRelayout.BREAKER_WINDOW:.0f}s. "
                "Something is re-triggering the layout (a resize-driven hook?). "
                "Skipping this run; check `ide verify`.\n")
            return 0
        state = IdeRelayout._read_state(state_path) if (mode == "adopt" and state_path) else {}
        prev_win = state.get("win")
        prev_side, prev_plan = state.get("side"), state.get("plan")
        if mode == "adopt" and prev_win:
            # Re-read the LEFT window live: the state may predate a drag made since.
            live = IdeRelayout._tmux(sock, ["display-message", "-p", "-t", f"{prev_win}.0",
                                            "#{pane_width}"])
            live_plan = IdeRelayout._tmux(sock, ["display-message", "-p", "-t", f"{prev_win}.2",
                                                 "#{pane_width}"])
            if live.isdigit() and live_plan.isdigit():
                prev_side, prev_plan = int(live), int(live_plan)
        for win in IdeRelayout._tmux(sock, ["list-windows", "-a", "-F", "#{window_id}"]).split():
            raw = IdeRelayout._tmux(sock, ["display-message", "-p", "-t", win, "#{window_width}"])
            if not raw.isdigit():
                continue
            width = int(raw)
            if width < sw + amin + pmin:
                # MOBILE: three columns cannot fit, so stop pretending. The FOCUSED column
                # owns the whole window (the `pane-focus-in` hook picks which one) — a
                # 4-column sidebar rail beside a squeezed plan is unusable on a phone.
                # The ladder must not unzoom here or it would fight that hook.
                IdeRelayout._set_zoom(sock, win, True)
                continue
            side = sw
            plan = IdeRelayout._plan_width(width, side, pw, pmin, amin, apref)
            if mode == "adopt":
                taken = IdeRelayout._adopted(prev_side, prev_plan, sw, plan, pmin, pw,
                                             amin, width)
                if taken:
                    side, plan = taken            # carry the manual drag to every window
            IdeRelayout._set_zoom(sock, win, False)
            # pane 0 = sidebar, pane 2 = plan; the agent (pane 1) absorbs the remainder.
            IdeRelayout._tmux(sock, ["resize-pane", "-t", f"{win}.0", "-x", str(side)])
            IdeRelayout._tmux(sock, ["resize-pane", "-t", f"{win}.2", "-x", str(plan)])
        # Remember which window is active NOW: on the next switch it is the one being LEFT,
        # so its columns are where a manual drag would live. A `snap` (real terminal resize)
        # also refreshes this, which is what makes a resize "reset to defaults" stick.
        if state_path:
            cur = IdeRelayout._tmux(sock, ["display-message", "-p", "#{window_id}"])
            if cur:
                IdeRelayout._write_state(state_path, {"win": cur, "side": sw,
                                                      "plan": pw if mode == "snap" else plan})
        return 0


if __name__ == "__main__":
    sys.exit(IdeRelayout.main(sys.argv))
