"""`ide_relayout` column ladder — who wins the spare columns when the window shrinks.

The agent pane is where the work happens; the plan is reference. So the agent is served
its preferred width BEFORE the plan grows past its minimum. The original ladder did the
opposite (agent pinned at its minimum, plan absorbing every spare column), which is the
"plan pane is too big on smaller screens" report. The tmux plumbing is verified by hand.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hive_ide.relayout import IdeRelayout  # noqa: E402

SW, PW, AMIN, PMIN, APREF = 20, 86, 60, 40, 90


def _plan(width: int, side: int = SW) -> int:
    return IdeRelayout._plan_width(width, side, PW, PMIN, AMIN, APREF)


def test_wide_window_gives_the_plan_its_full_width():
    """Plenty of room — the plan gets PW and the agent still clears its preferred width."""
    assert _plan(200) == PW
    assert 200 - SW - _plan(200) >= APREF


def test_agent_gets_its_preferred_width_before_the_plan_grows():
    """THE REGRESSION: at 166 cols the old ladder pinned the agent at AMIN (60) and handed
    the plan its full 86. The agent must get APREF and the plan take only what is left."""
    assert _plan(166) == 166 - SW - APREF == 56
    assert 166 - SW - _plan(166) == APREF          # agent comfortable, not crushed
    # across the whole mid band the agent holds its preferred width
    for w in (150, 160, 166, 180):
        assert w - SW - _plan(w) >= APREF, w


def test_plan_never_drops_below_its_minimum():
    """Below the minimum the plan is not worth showing, so it floors rather than vanishing."""
    for w in (120, 130, 140, 150):
        assert _plan(w) >= PMIN, w
    assert _plan(120) == PMIN


def test_agent_minimum_wins_over_the_plan_floor():
    """When even AMIN + PMIN is tight, the plan yields first — the agent's hard minimum is
    the last thing to give."""
    assert 120 - SW - _plan(120) == AMIN           # exactly at the floor
    # the plan never grows so far that the agent would fall under its minimum
    for w in range(120, 210, 5):
        assert w - SW - _plan(w) >= AMIN, w


def test_plan_is_capped_and_monotonic_in_width():
    """Never wider than PW, and a wider window never yields a narrower plan."""
    widths = list(range(120, 240, 4))
    plans = [_plan(w) for w in widths]
    assert all(p <= PW for p in plans)
    assert plans == sorted(plans), "plan width must not shrink as the window grows"


def test_rail_mode_reclaims_the_sidebar_columns_for_the_agent():
    """On the icon rail the freed sidebar columns go to the AGENT, not the plan."""
    rail_agent = 110 - 4 - _plan(110, side=4)
    assert _plan(110, side=4) == PMIN
    assert rail_agent >= AMIN


def _adopt(prev_side, prev_plan, width=200, plan_default=None):
    if plan_default is None:
        plan_default = IdeRelayout._plan_width(width, SW, PW, PMIN, AMIN, APREF)
    return IdeRelayout._adopted(prev_side, prev_plan, SW, plan_default,
                                PMIN, PW, AMIN, width)


def test_adopt_ignores_an_untouched_window():
    """No drag happened — the window you left still matches the computed default, so the
    defaults stand and nothing is propagated."""
    default = IdeRelayout._plan_width(200, SW, PW, PMIN, AMIN, APREF)
    assert _adopt(SW, default) is None
    assert _adopt(None, None) is None          # no prior state (first switch)


def test_adopt_carries_a_real_drag_to_every_window():
    """Dragged the plan narrower in one session — that width becomes everyone's.
    (At width 200 the computed default is already PW, so PW itself reads as untouched;
    a drag is by definition a width that DIFFERS from the default.)"""
    assert _adopt(SW, 60) == (SW, 60)
    assert _adopt(SW, PMIN) == (SW, PMIN)
    # a widened plan on a window whose default is narrower (default at 160 is 50; 70 still
    # leaves the agent 70, clear of its minimum)
    assert _adopt(SW, 70, width=160) == (SW, 70)
    # a narrowed sidebar counts too
    assert _adopt(12, 60) == (12, 60)


def test_adopt_rejects_a_nonsense_drag():
    """A drag that would starve the agent or push the plan out of range must NOT be
    propagated — one bad drag would otherwise wreck every session at once."""
    assert _adopt(SW, PMIN - 1) is None        # below the plan minimum
    assert _adopt(SW, PW + 10) is None         # wider than the plan ever gets
    # plan so wide the agent would drop under its hard minimum
    assert _adopt(SW, 200 - SW - AMIN + 5) is None


def test_adopt_respects_the_agent_minimum_exactly_at_the_boundary():
    """Boundary is inclusive: a drag leaving the agent at exactly its minimum is allowed;
    one column further is rejected. (Width 160 — at 200 the PW cap bites first.)"""
    w = 160
    edge = w - SW - AMIN                        # 80 → agent == AMIN exactly, and <= PW
    assert edge <= PW
    assert _adopt(SW, edge, width=w) == (SW, edge)
    assert _adopt(SW, edge + 1, width=w) is None    # one past → agent under its minimum


def test_adopt_with_only_mobile_windows_does_not_crash(tmp_path, monkeypatch):
    state = tmp_path / "layout.json"

    def fake_tmux(_socket, args):
        if args[:2] == ["list-windows", "-a"]:
            return "@0\t80\t24\n@1\t80\t24"
        if args[-1] == "#{window_width}\t#{window_height}":
            return "80\t24"
        if args[-1] == "#{window_width}":
            return "80"
        if args[-1] == "#{window_zoomed_flag}":
            return "1"
        if args[-1] == "#{window_id}":
            return "@0"
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "adopt",
            str(state),
        ]
    ) == 0
    assert json.loads(state.read_text(encoding="utf-8"))["plan"] == PW


def test_snap_uses_explicit_window_size_when_tmux_window_is_stale(tmp_path, monkeypatch):
    state = tmp_path / "layout.json"
    calls: list[list[str]] = []

    def fake_tmux(_socket, args):
        calls.append(args)
        if args[:2] == ["list-windows", "-a"]:
            return "@0\t254\t66\n@1\t254\t66"
        if args[-1] == "#{window_width}\t#{window_height}":
            return "254\t66"
        if args[-1] == "#{window_width}":
            return "254"
        if args[-1] == "#{window_zoomed_flag}":
            return "0"
        if args[-1] == "#{window_id}":
            return "@0"
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            str(state),
            "58",
            "24",
        ]
    ) == 0
    assert [
        "resize-window",
        "-t",
        "@0",
        "-x",
        "58",
        "-y",
        "24",
    ] in calls
    assert ["set-window-option", "-u", "-t", "@0", "window-size"] in calls
    assert [
        "resize-window",
        "-t",
        "@1",
        "-x",
        "58",
        "-y",
        "24",
    ] in calls
    assert ["set-window-option", "-u", "-t", "@1", "window-size"] in calls


def test_resize_hook_snap_uses_latest_client_without_scanning_windows(tmp_path, monkeypatch):
    state = tmp_path / "layout.json"
    calls: list[list[str]] = []

    def fake_tmux(_socket, args):
        calls.append(args)
        if args[:2] == ["list-clients", "-F"]:
            return "100\t254\t69"
        if args[:2] == ["show-options", "-gv"] and args[-1] == "status":
            return "off"
        if args[:2] == ["list-windows", "-a"]:
            raise AssertionError(f"targeted resize-hook snap must not scan windows: {args}")
        if args[-1] == "#{window_width}\t#{window_height}":
            raise AssertionError(f"resize-hook snap must not query active geometry: {args}")
        if args[-1] == "#{window_width}":
            return "254"
        if args[-1] == "#{window_zoomed_flag}":
            return "1"
        if args[-1] == "#{window_id}":
            return "@0"
        if args[-1] == "#{pane_id}":
            return "%1"
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            str(state),
            "254",
            "67",
            "@0",
        ]
    ) == 0
    assert ["resize-pane", "-t", "@0.0", "-x", "20"] in calls
    assert ["resize-pane", "-t", "@0.2", "-x", "86"] in calls
    assert ["resize-window", "-t", "@0", "-x", "254", "-y", "69"] in calls


def test_manual_snap_without_hook_geometry_can_use_latest_client_geometry(tmp_path, monkeypatch):
    state = tmp_path / "layout.json"
    calls: list[list[str]] = []

    def fake_tmux(_socket, args):
        calls.append(args)
        if args[:2] == ["show-options", "-gv"] and args[-1] == "status":
            return "on"
        if args[:2] == ["show-options", "-g"] and args[-1] == "status-format":
            return "status-format[0] default"
        if args[:2] == ["list-clients", "-F"]:
            return "100\t220\t65"
        if args[:2] == ["list-windows", "-a"]:
            return "@0\t220\t65"
        if args[-1] == "#{window_width}\t#{window_height}":
            return "220\t65"
        if args[-1] == "#{window_width}":
            return "220"
        if args[-1] == "#{window_zoomed_flag}":
            return "0"
        if args[-1] == "#{window_id}":
            return "@0"
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)

    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            str(state),
        ]
    ) == 0

    assert [
        "resize-window",
        "-t",
        "@0",
        "-x",
        "220",
        "-y",
        "64",
    ] in calls
    assert ["set-window-option", "-u", "-t", "@0", "window-size"] in calls


def test_debug_trace_records_relayout_geometry_decision(tmp_path, monkeypatch):
    state = tmp_path / "layout.json"
    (tmp_path / "layout.json.debug.enable").write_text("1", encoding="utf-8")

    def fake_tmux(_socket, args):
        if args[:2] == ["list-clients", "-F"]:
            if args[-1].endswith("#{client_session}"):
                return (
                    "100\t254\t67\t/dev/pts/1\thive-ide\n"
                    "200\t58\t24\t/dev/pts/2\thive-ide"
                )
            return "100\t254\t67\n200\t58\t24"
        if args[:2] == ["list-panes", "-t"]:
            return "%1\t0\tsidebar\t24\t67\t0\t66\t1\tpython3"
        if args[:2] == ["list-windows", "-a"]:
            return "@0\t254\t67"
        if args[:2] == ["show-options", "-gv"] and args[-1] == "status":
            return "on"
        if args[:2] == ["show-window-options", "-gv"] and args[-1] == "window-size":
            return "latest"
        if args[-1] == "#{window_width}\t#{window_height}":
            return "254\t67"
        if args[-1] == "#{window_width}":
            return "254"
        if args[-1] == "#{window_zoomed_flag}":
            return "0"
        if args[-1] == "#{window_id}":
            return "@0"
        if args[-1] == "#{pane_id}":
            return "%1"
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            str(state),
            "254",
            "67",
        ]
    ) == 0

    events = [
        json.loads(line)
        for line in (tmp_path / "layout.json.debug.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "relayout"
    assert event["geometry_source"] == "latest-client"
    assert event["forced_geometry"] == [254, 67]
    assert event["latest_geometry"] == [58, 23]
    assert event["latest_client_geometry"] == [58, 24]
    assert event["clients"][1]["tty"] == "/dev/pts/2"
    assert event["clients"][1]["session"] == "hive-ide"
    assert event["tmux_options"]["server.status"] == "on"
    assert event["tmux_options"]["window.window-size"] == "latest"
    assert event["windows"][0]["resized_to"] == [58, 23]
    assert event["windows"][0]["panes_before"][0]["role"] == "sidebar"


def test_debug_trace_can_be_enabled_from_config_snapshot(tmp_path, monkeypatch):
    state = tmp_path / "layout.json"
    (tmp_path / "config.json").write_text(
        json.dumps({"diagnostics": {"relayout_trace": True}}),
        encoding="utf-8",
    )

    def fake_tmux(_socket, args):
        if args[:2] == ["list-windows", "-a"]:
            return "@0\t180\t40"
        if args[-1] == "#{window_width}\t#{window_height}":
            return "180\t40"
        if args[-1] == "#{window_width}":
            return "180"
        if args[-1] == "#{window_zoomed_flag}":
            return "0"
        if args[-1] == "#{window_id}":
            return "@0"
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            str(state),
        ]
    ) == 0

    events = [
        json.loads(line)
        for line in (tmp_path / "layout.json.debug.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert events[0]["event"] == "relayout"


def test_debug_trace_is_silent_without_enable_file(tmp_path, monkeypatch):
    state = tmp_path / "layout.json"

    def fake_tmux(_socket, args):
        if args[:2] == ["list-windows", "-a"]:
            return "@0\t180\t40"
        if args[-1] == "#{window_width}\t#{window_height}":
            return "180\t40"
        if args[-1] == "#{window_width}":
            return "180"
        if args[-1] == "#{window_zoomed_flag}":
            return "0"
        if args[-1] == "#{window_id}":
            return "@0"
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            str(state),
        ]
    ) == 0
    assert not (tmp_path / "layout.json.debug.jsonl").exists()


def test_snap_coalescer_allows_the_latest_resize_event(tmp_path):
    state = str(tmp_path / "layout.json")
    slept = []

    assert not IdeRelayout._coalesced_by_newer_snap(
        state,
        sleep=lambda seconds: slept.append(seconds),
        now=123.0,
    )
    assert slept == [IdeRelayout.SNAP_DEBOUNCE_SECONDS]


def test_snap_coalescer_skips_an_event_superseded_during_debounce(tmp_path):
    state = str(tmp_path / "layout.json")

    def newer_snap(_seconds):
        Path(state + ".pending").write_text("newer", encoding="utf-8")

    assert IdeRelayout._coalesced_by_newer_snap(
        state,
        sleep=newer_snap,
        now=123.0,
    )


def test_superseded_snap_skip_does_not_query_tmux_for_debug(tmp_path, monkeypatch):
    state = str(tmp_path / "layout.json")
    (tmp_path / "layout.json.debug.enable").write_text("1", encoding="utf-8")

    def fail_tmux(_socket, args):
        raise AssertionError(f"superseded snap should not query tmux: {args}")

    monkeypatch.setattr(IdeRelayout, "_tmux", fail_tmux)
    monkeypatch.setattr(IdeRelayout, "_coalesced_by_newer_snap", lambda _path: True)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            state,
            "203",
            "70",
        ]
    ) == 0

    events = [
        json.loads(line)
        for line in (tmp_path / "layout.json.debug.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert events[-1]["reason"] == "newer-snap"
    assert "clients" not in events[-1]
    assert "tmux_options" not in events[-1]


def test_repeated_snap_geometry_still_checks_tmux_panes(tmp_path, monkeypatch):
    state = str(tmp_path / "layout.json")
    (tmp_path / "layout.json.last-snap").write_text(
        json.dumps({"ts": time.time(), "geometry": [203, 70]}),
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_tmux(_socket, args):
        calls.append(args)
        return ""

    monkeypatch.setattr(IdeRelayout, "_tmux", fake_tmux)
    monkeypatch.setattr(IdeRelayout, "_breaker_tripped", lambda _path: False)
    assert IdeRelayout.main(
        [
            "relayout",
            "test-socket",
            str(SW),
            str(PW),
            "4",
            str(AMIN),
            str(PMIN),
            str(APREF),
            "snap",
            state,
            "203",
            "70",
        ]
    ) == 0

    assert calls


def test_tmux_timeout_returns_empty_instead_of_hanging(monkeypatch):
    def timeout_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["tmux"], timeout=IdeRelayout.TMUX_TIMEOUT_SECONDS)

    monkeypatch.setattr("hive_ide.relayout.subprocess.run", timeout_run)

    assert IdeRelayout._tmux("test-socket", ["display-message", "-p", "#{window_width}"]) == ""


# ---- circuit breaker: defence in depth against a self-feeding layout hook ----

def test_breaker_ledger_prunes_to_the_window():
    """Only invocations inside the window count; older ones age out."""
    now = 1000.0
    prev = [990.0, 995.0, 999.0]          # 10s window → 990.0 is exactly at the edge
    hits = IdeRelayout._breaker_hits(prev, now, 10.0)
    assert 990.0 not in hits, "an entry exactly one window old must age out"
    assert hits == [995.0, 999.0, now]


def test_breaker_ledger_survives_a_corrupt_file():
    """Junk in the ledger is dropped, never raised — a bad file must not block layout."""
    hits = IdeRelayout._breaker_hits(["x", None, True, 999.0], 1000.0, 10.0)
    assert hits == [999.0, 1000.0]


def test_breaker_trips_on_a_burst_and_resets_after_the_window(tmp_path, monkeypatch):
    """A runaway hook trips it; once the burst ages out, normal operation resumes.
    The production threshold is deliberately high (a drag must not trip it), so this
    patches it down rather than spinning 200+ iterations."""
    monkeypatch.setattr(IdeRelayout, "BREAKER_MAX", 12)
    state = str(tmp_path / "layout.json")
    t = 5000.0
    tripped = [IdeRelayout._breaker_tripped(state, t + i * 0.1)
               for i in range(IdeRelayout.BREAKER_MAX + 3)]
    assert not any(tripped[:IdeRelayout.BREAKER_MAX]), "must not trip under the limit"
    assert tripped[-1], "a burst past the limit must trip"
    # ...and a call after the window has passed is clean again
    assert not IdeRelayout._breaker_tripped(state, t + IdeRelayout.BREAKER_WINDOW + 60)


def test_breaker_is_fail_open(tmp_path):
    """No state path, or an unwritable location, must NEVER block a legitimate relayout."""
    assert IdeRelayout._breaker_tripped("") is False
    assert IdeRelayout._breaker_tripped("/proc/nonexistent-dir/deny/layout.json") is False


def test_breaker_ledger_survives_concurrent_writers(tmp_path):
    """CONCURRENCY — the case the breaker exists for.

    A runaway hook fires many `run-shell -b` relayouts AT ONCE. With an unlocked
    read-modify-write every process reads the same pre-limit ledger and they all sail
    through, so the breaker misses the very burst it was added to contain (found in
    review). Locking must make the updates serialize: N concurrent callers must leave
    N recorded hits, with none lost.
    """
    import concurrent.futures as cf

    state = str(tmp_path / "layout.json")
    workers = 24
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        # real wall-clock `now` so nothing is aged out mid-run
        list(pool.map(lambda _: IdeRelayout._breaker_tripped(state), range(workers)))

    with open(state + ".hits", encoding="utf-8") as fh:
        recorded = fh.read().split()          # append-only ledger: one timestamp per line
    kept = min(workers, IdeRelayout.BREAKER_KEEP)
    assert len(recorded) == kept, (
        f"lost updates under concurrency: {len(recorded)} of {kept} hits recorded — "
        "the read-modify-write is not serialized")


def test_breaker_trips_under_concurrent_burst(tmp_path, monkeypatch):
    """The behavioural half: a concurrent burst past the limit must actually trip."""
    import concurrent.futures as cf

    monkeypatch.setattr(IdeRelayout, "BREAKER_MAX", 12)
    state = str(tmp_path / "layout.json")
    n = IdeRelayout.BREAKER_MAX + 8
    with cf.ThreadPoolExecutor(max_workers=n) as pool:
        results = list(pool.map(lambda _: IdeRelayout._breaker_tripped(state), range(n)))
    assert any(results), "a concurrent burst past the limit must trip the breaker"


def test_breaker_does_not_trip_on_a_realistic_resize_drag(tmp_path):
    """REGRESSION (review finding): dragging a terminal edge emits a flood of legitimate
    `client-resized` snaps. If the breaker trips on that it skips the FINAL snap and leaves
    the columns at tmux's proportional widths — the safety net breaking what it protects.

    A generous drag: 60 resize events over ~2 seconds. Must never trip.
    """
    state = str(tmp_path / "layout.json")
    t = 9000.0
    tripped = [IdeRelayout._breaker_tripped(state, t + i * (2.0 / 60)) for i in range(60)]
    assert not any(tripped), (
        "a normal terminal drag must not trip the breaker — tripping mid-drag skips the "
        "final snap and leaves the layout broken")


def test_breaker_still_catches_a_sustained_runaway(tmp_path):
    """The other half: a self-feeding hook never stops, so sustained hammering MUST trip
    even with the drag-tolerant threshold."""
    state = str(tmp_path / "layout.json")
    t = 9000.0
    # a loop: far more than any human drag, inside the window
    tripped = [IdeRelayout._breaker_tripped(state, t + i * 0.02)
               for i in range(IdeRelayout.BREAKER_MAX + 25)]
    assert any(tripped), "a sustained runaway must still be caught"
