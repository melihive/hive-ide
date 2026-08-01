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
    def _debug_enabled(path: str) -> bool:
        if os.environ.get("HIVE_IDE_RELAYOUT_DEBUG", "").lower() in {"1", "true", "yes"}:
            return True
        if not path:
            return False
        if os.path.exists(path + ".debug.enable"):
            return True
        try:
            with open(os.path.join(os.path.dirname(path), "config.json"), encoding="utf-8") as fh:
                config = json.load(fh)
        except (OSError, ValueError):
            return False
        diagnostics = config.get("diagnostics") if isinstance(config, dict) else None
        return bool(isinstance(diagnostics, dict) and diagnostics.get("relayout_trace"))

    @staticmethod
    def _debug_write(path: str, event: dict) -> None:
        if not IdeRelayout._debug_enabled(path):
            return
        payload = {
            "ts": time.time(),
            "pid": os.getpid(),
            **event,
        }
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path + ".debug.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True) + "\n")
        except OSError:
            pass

    @staticmethod
    def _tmux(socket: str, args: list[str]) -> str:
        r = subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""

    @staticmethod
    def _zoomed(socket: str, win: str) -> bool:
        return IdeRelayout._tmux(socket, ["display-message", "-p", "-t", f"{win}.1",
                                          "#{window_zoomed_flag}"]) == "1"

    @staticmethod
    def _set_zoom(socket: str, win: str, want: bool, target: str | None = None) -> None:
        # `resize-pane -Z` TOGGLES, so guard on the current flag — that is what stops
        # repeated resize events (tmux fires many) from flapping in and out of zoom.
        target = target or f"{win}.1"
        zoomed = IdeRelayout._zoomed(socket, win)
        if want and zoomed:
            active = IdeRelayout._tmux(socket, ["display-message", "-p", "-t", win, "#{pane_id}"])
            if active != target:
                IdeRelayout._tmux(socket, ["resize-pane", "-t", active or f"{win}.1", "-Z"])
                IdeRelayout._tmux(socket, ["select-pane", "-t", target])
                IdeRelayout._tmux(socket, ["resize-pane", "-t", target, "-Z"])
            return
        if zoomed != want:
            IdeRelayout._tmux(socket, ["resize-pane", "-t", target, "-Z"])

    @staticmethod
    def _role_panes(socket: str, win: str) -> dict[str, str]:
        rows = IdeRelayout._tmux(
            socket,
            ["list-panes", "-t", win, "-F", "#{@hive_ide_pane}\t#{pane_id}"],
        )
        return {
            role: pane_id
            for line in rows.splitlines()
            for role, _, pane_id in [line.partition("\t")]
            if role and pane_id
        }

    @staticmethod
    def _pane_indices(socket: str, win: str) -> dict[int, str]:
        rows = IdeRelayout._tmux(
            socket,
            ["list-panes", "-t", win, "-F", "#{pane_index}\t#{pane_id}"],
        )
        out: dict[int, str] = {}
        for line in rows.splitlines():
            index, _, pane_id = line.partition("\t")
            if index.isdigit() and pane_id:
                out[int(index)] = pane_id
        return out

    @staticmethod
    def _order_role_panes(socket: str, win: str) -> dict[str, str]:
        desired = ("sidebar", "agent", "plan")
        roles = IdeRelayout._role_panes(socket, win)
        if set(desired) - set(roles):
            return roles
        for index, role in enumerate(desired):
            roles = IdeRelayout._role_panes(socket, win)
            indices = IdeRelayout._pane_indices(socket, win)
            role_pane = roles.get(role)
            index_pane = indices.get(index)
            if role_pane and index_pane and role_pane != index_pane:
                IdeRelayout._tmux(socket, ["swap-pane", "-s", role_pane, "-t", index_pane])
        return IdeRelayout._role_panes(socket, win)

    @staticmethod
    def _latest_client_geometry(socket: str) -> tuple[int, int] | None:
        rows = IdeRelayout._tmux(
            socket,
            [
                "list-clients",
                "-F",
                "#{client_activity}\t#{client_width}\t#{client_height}",
            ],
        )
        latest: tuple[int, int, int] | None = None
        for line in rows.splitlines():
            activity, width, height = line.split("\t") if line.count("\t") == 2 else ("", "", "")
            if not (activity.isdigit() and width.isdigit() and height.isdigit()):
                continue
            candidate = (int(activity), int(width), int(height))
            if latest is None or candidate[0] > latest[0]:
                latest = candidate
        return (latest[1], latest[2]) if latest else None

    @staticmethod
    def _client_geometries(socket: str) -> list[dict]:
        rows = IdeRelayout._tmux(
            socket,
            [
                "list-clients",
                "-F",
                "#{client_activity}\t#{client_width}\t#{client_height}\t"
                "#{client_tty}\t#{client_session}",
            ],
        )
        clients = []
        for line in rows.splitlines():
            activity, width, height, tty, session = (
                line.split("\t") if line.count("\t") == 4 else ("", "", "", "", "")
            )
            clients.append(
                {
                    "activity": int(activity) if activity.isdigit() else activity,
                    "width": int(width) if width.isdigit() else width,
                    "height": int(height) if height.isdigit() else height,
                    "tty": tty,
                    "session": session,
                }
            )
        return clients

    @staticmethod
    def _pane_geometries(socket: str, win: str) -> list[dict]:
        rows = IdeRelayout._tmux(
            socket,
            [
                "list-panes",
                "-t",
                win,
                "-F",
                "#{pane_id}\t#{pane_index}\t#{@hive_ide_pane}\t#{pane_width}\t"
                "#{pane_height}\t#{pane_top}\t#{pane_bottom}\t#{pane_active}\t"
                "#{pane_current_command}",
            ],
        )
        panes = []
        for line in rows.splitlines():
            fields = line.split("\t")
            if len(fields) != 9:
                continue
            pane_id, index, role, width, height, top, bottom, active, command = fields
            panes.append(
                {
                    "pane": pane_id,
                    "index": int(index) if index.isdigit() else index,
                    "role": role,
                    "width": int(width) if width.isdigit() else width,
                    "height": int(height) if height.isdigit() else height,
                    "top": int(top) if top.isdigit() else top,
                    "bottom": int(bottom) if bottom.isdigit() else bottom,
                    "active": active == "1",
                    "command": command,
                }
            )
        return panes

    @staticmethod
    def _tmux_options(socket: str) -> dict[str, str]:
        options: dict[str, str] = {}
        for scope, command, names in [
            (
                "server",
                "show-options",
                ["status", "status-position", "status-interval"],
            ),
            (
                "window",
                "show-window-options",
                ["window-size", "pane-border-status", "aggressive-resize"],
            ),
        ]:
            for name in names:
                value = IdeRelayout._tmux(socket, [command, "-gv", name])
                if value:
                    options[f"{scope}.{name}"] = value
        return options

    @staticmethod
    def _geometry_source(
        latest: tuple[int, int] | None,
        forced: tuple[int, int] | None,
        active: tuple[int, int] | None,
    ) -> str:
        if latest:
            return "latest-client"
        if forced:
            return "hook-client"
        if active:
            return "active-window"
        return "unknown"

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
    SNAP_DEBOUNCE_SECONDS = 0.08

    @staticmethod
    def _coalesced_by_newer_snap(
        path: str,
        *,
        sleep=time.sleep,
        now: float | None = None,
    ) -> bool:
        """True when a newer snap arrived during the short resize debounce.

        tmux can report intermediate client heights during terminal chrome/status
        transitions (`67 → 68 → 69` within ~100ms). Applying every intermediate size
        makes all windows visibly jump by one row. Snap relayout is idempotent and
        client-size driven, so the right behavior is last event wins.
        """
        if not path:
            return False
        token = f"{now if now is not None else time.time()}:{os.getpid()}"
        marker = path + ".pending"
        try:
            os.makedirs(os.path.dirname(marker) or ".", exist_ok=True)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(token)
            sleep(IdeRelayout.SNAP_DEBOUNCE_SECONDS)
            with open(marker, encoding="utf-8") as fh:
                return fh.read() != token
        except OSError:
            return False

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
            parser.add_argument("--client-width")
            parser.add_argument("--client-height")
            parsed = parser.parse_args(argv[1:])
            store = StateStore(parsed.state_home, parsed.workspace_key)
            relayout_argv = [
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
            if parsed.client_width and parsed.client_height:
                relayout_argv.extend([parsed.client_width, parsed.client_height])
            return IdeRelayout.main(relayout_argv)
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
        forced_geometry: tuple[int, int] | None = None
        if len(argv) > 11 and argv[10].isdigit() and argv[11].isdigit():
            forced_geometry = (int(argv[10]), int(argv[11]))
        if mode == "snap" and IdeRelayout._coalesced_by_newer_snap(state_path):
            IdeRelayout._debug_write(
                state_path,
                {
                    "event": "relayout-skipped",
                    "reason": "newer-snap",
                    "socket": sock,
                    "mode": mode,
                    "forced_geometry": list(forced_geometry) if forced_geometry else None,
                    "clients": IdeRelayout._client_geometries(sock),
                    "tmux_options": IdeRelayout._tmux_options(sock),
                },
            )
            return 0
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
            prev_roles = IdeRelayout._order_role_panes(sock, prev_win)
            live = IdeRelayout._tmux(
                sock,
                ["display-message", "-p", "-t", prev_roles.get("sidebar", f"{prev_win}.0"),
                 "#{pane_width}"],
            )
            live_plan = IdeRelayout._tmux(
                sock,
                ["display-message", "-p", "-t", prev_roles.get("plan", f"{prev_win}.2"),
                 "#{pane_width}"],
            )
            if live.isdigit() and live_plan.isdigit():
                prev_side, prev_plan = int(live), int(live_plan)
        active_geometry = IdeRelayout._tmux(
            sock,
            ["display-message", "-p", "#{window_width}\t#{window_height}"],
        ).split("\t")
        active_tuple = (
            (int(active_geometry[0]), int(active_geometry[1]))
            if len(active_geometry) == 2
            and active_geometry[0].isdigit()
            and active_geometry[1].isdigit()
            else None
        )
        latest_geometry = IdeRelayout._latest_client_geometry(sock)
        canonical = latest_geometry or forced_geometry or active_tuple
        debug_enabled = IdeRelayout._debug_enabled(state_path)
        debug_options = IdeRelayout._tmux_options(sock) if debug_enabled else {}
        debug_windows = []
        remembered_plan = pw
        for win in IdeRelayout._tmux(sock, ["list-windows", "-a", "-F", "#{window_id}"]).split():
            before_panes = IdeRelayout._pane_geometries(sock, win) if debug_enabled else []
            raw_geometry = IdeRelayout._tmux(
                sock,
                ["display-message", "-p", "-t", win, "#{window_width}\t#{window_height}"],
            ).split("\t")
            if (
                len(raw_geometry) != 2
                or not raw_geometry[0].isdigit()
                or not raw_geometry[1].isdigit()
            ):
                continue
            width = int(raw_geometry[0])
            height = int(raw_geometry[1])
            before = (width, height)
            resized_to = None
            if canonical and (width, height) != canonical:
                IdeRelayout._tmux(
                    sock,
                    [
                        "resize-window",
                        "-t",
                        win,
                        "-x",
                        str(canonical[0]),
                        "-y",
                        str(canonical[1]),
                    ],
                )
                width = canonical[0]
                height = canonical[1]
                resized_to = canonical
            if width < sw + amin + pmin:
                roles = IdeRelayout._order_role_panes(sock, win)
                active_pane = IdeRelayout._tmux(
                    sock,
                    ["display-message", "-p", "-t", win, "#{pane_id}"],
                )
                # MOBILE: three columns cannot fit, so stop pretending. The FOCUSED column
                # owns the whole window (the `pane-focus-in` hook picks which one) — a
                # 4-column sidebar rail beside a squeezed plan is unusable on a phone.
                # The ladder must not unzoom here or it would fight that hook.
                IdeRelayout._set_zoom(sock, win, True, active_pane or roles.get("agent"))
                debug_windows.append(
                    {
                        "window": win,
                        "before": list(before),
                        "after": [width, height],
                        "resized_to": list(resized_to) if resized_to else None,
                        "mode": "mobile-zoom",
                        "panes_before": before_panes,
                        "panes_after": (
                            IdeRelayout._pane_geometries(sock, win) if debug_enabled else []
                        ),
                    }
                )
                continue
            side = sw
            plan = IdeRelayout._plan_width(width, side, pw, pmin, amin, apref)
            if mode == "adopt":
                taken = IdeRelayout._adopted(prev_side, prev_plan, sw, plan, pmin, pw,
                                             amin, width)
                if taken:
                    side, plan = taken            # carry the manual drag to every window
            remembered_plan = plan
            IdeRelayout._set_zoom(sock, win, False)
            roles = IdeRelayout._order_role_panes(sock, win)
            # The role tag is authoritative; pane indices can drift after manual swaps.
            IdeRelayout._tmux(
                sock,
                ["resize-pane", "-t", roles.get("sidebar", f"{win}.0"), "-x", str(side)],
            )
            IdeRelayout._tmux(
                sock,
                ["resize-pane", "-t", roles.get("plan", f"{win}.2"), "-x", str(plan)],
            )
            debug_windows.append(
                {
                    "window": win,
                    "before": list(before),
                    "after": [width, height],
                    "resized_to": list(resized_to) if resized_to else None,
                    "mode": "three-pane",
                    "sidebar": side,
                    "plan": plan,
                    "panes_before": before_panes,
                    "panes_after": (
                        IdeRelayout._pane_geometries(sock, win) if debug_enabled else []
                    ),
                }
            )
        if debug_enabled:
            IdeRelayout._debug_write(
                state_path,
                {
                    "event": "relayout",
                    "socket": sock,
                    "mode": mode,
                    "forced_geometry": list(forced_geometry) if forced_geometry else None,
                    "active_geometry": list(active_tuple) if active_tuple else None,
                    "latest_geometry": list(latest_geometry) if latest_geometry else None,
                    "geometry_source": IdeRelayout._geometry_source(
                        latest_geometry, forced_geometry, active_tuple
                    ),
                    "tmux_options": debug_options,
                    "clients": IdeRelayout._client_geometries(sock),
                    "windows": debug_windows,
                },
            )
        # Remember which window is active NOW: on the next switch it is the one being LEFT,
        # so its columns are where a manual drag would live. A `snap` (real terminal resize)
        # also refreshes this, which is what makes a resize "reset to defaults" stick.
        if state_path:
            cur = IdeRelayout._tmux(sock, ["display-message", "-p", "#{window_id}"])
            if cur:
                IdeRelayout._write_state(state_path, {"win": cur, "side": sw,
                                                      "plan": pw if mode == "snap"
                                                      else remembered_plan})
        return 0


if __name__ == "__main__":
    sys.exit(IdeRelayout.main(sys.argv))
