"""Stepping to the next session must not flash the previous order.

Only the displayed window runs a sidebar process, so a backgrounded pane keeps
the frame it last painted, and the list is ordered by activity — which moves
every time any agent takes a turn. So the order was stale on essentially every
switch. `wake` repaints on the `session-window-changed` hook, but a hook fires
AFTER the switch and pays a cold interpreter start, so the wrong order is on
screen for that whole window (61ms measured idle; far longer on a loaded frame).

`nav` already knows where it is going and is already running, so it paints first
and switches second. Then there is no stale frame to race.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hive_ide.nav import IdeNav


def test_the_destination_is_painted_before_the_window_is_selected(monkeypatch):
    """Ordering is the whole feature: painting after the switch is what it replaces."""
    events: list[str] = []

    def fake_tmux(_socket, args):
        if args[:2] == ["list-windows", "-a"]:
            return "@1\tsid-alpha\n@2\tsid-bravo"
        if args[:1] == ["select-window"]:
            events.append(f"select-window:{args[-1]}")
            return ""
        if args[:1] == ["select-pane"]:
            events.append("select-pane")
            return ""
        if "#{pane_index}" in args:
            return "0"
        return ""

    monkeypatch.setattr(IdeNav, "_tmux", staticmethod(fake_tmux))
    monkeypatch.setattr(
        IdeNav, "_order", staticmethod(lambda *a, **k: ["sid-alpha", "sid-bravo"])
    )

    painted: list[str] = []

    def fake_wake(skill_dir, repo, session_id, socket):
        events.append(f"paint:{session_id}")
        painted.append(session_id)
        return True

    import hive_ide.wake as wake_module

    monkeypatch.setattr(wake_module.IdeWake, "wake", staticmethod(fake_wake))

    rc = IdeNav.main(
        [
            "nav",
            "--state-home",
            "/tmp/state",
            "--workspace-key",
            "/tmp/ws",
            "--session-id",
            "sid-alpha",
            "--tmux-socket",
            "sock",
            "--direction",
            "next",
        ]
    )

    assert rc == 0
    assert painted == ["sid-bravo"], f"painted the wrong session: {painted}"
    assert events.index("paint:sid-bravo") < events.index("select-window:@2"), (
        f"the switch happened before the repaint, so the stale order is still "
        f"on screen while it lands: {events}"
    )


def test_a_failed_repaint_still_switches(monkeypatch):
    """Fail-open. A repaint that raises must never strand you on the old window."""
    events: list[str] = []

    def fake_tmux(_socket, args):
        if args[:2] == ["list-windows", "-a"]:
            return "@1\tsid-alpha\n@2\tsid-bravo"
        if args[:1] == ["select-window"]:
            events.append("select-window")
            return ""
        if "#{pane_index}" in args:
            return "0"
        return ""

    monkeypatch.setattr(IdeNav, "_tmux", staticmethod(fake_tmux))
    monkeypatch.setattr(
        IdeNav, "_order", staticmethod(lambda *a, **k: ["sid-alpha", "sid-bravo"])
    )

    def boom(*_a, **_k):
        raise RuntimeError("tty vanished")

    import hive_ide.wake as wake_module

    monkeypatch.setattr(wake_module.IdeWake, "wake", staticmethod(boom))

    assert (
        IdeNav.main(
            [
                "nav",
                "--state-home",
                "/tmp/state",
                "--workspace-key",
                "/tmp/ws",
                "--session-id",
                "sid-alpha",
                "--tmux-socket",
                "sock",
                "--direction",
                "next",
            ]
        )
        == 0
    )
    assert "select-window" in events, "a failed repaint blocked the switch"


def test_a_slow_repaint_does_not_delay_the_switch(monkeypatch):
    """The budget, pinned.

    The frame carries checkout decorations, and `git_status` shells out to git
    with a 2s timeout per call and no cache, so several slow worktrees could
    stall a keypress for seconds. Navigation has to stay responsive: over budget
    the repaint is abandoned and the switch happens anyway, which is exactly the
    behaviour that shipped before the repaint existed.
    """
    import time

    events: list[str] = []

    def fake_tmux(_socket, args):
        if args[:2] == ["list-windows", "-a"]:
            return "@1\tsid-alpha\n@2\tsid-bravo"
        if args[:1] == ["select-window"]:
            events.append("select-window")
            return ""
        if "#{pane_index}" in args:
            return "0"
        return ""

    monkeypatch.setattr(IdeNav, "_tmux", staticmethod(fake_tmux))
    monkeypatch.setattr(
        IdeNav, "_order", staticmethod(lambda *a, **k: ["sid-alpha", "sid-bravo"])
    )
    monkeypatch.setattr(IdeNav, "PREPAINT_BUDGET_SECONDS", 0.05)

    def slow(*_a, **_k):
        time.sleep(5)  # a wedged `git status`, in effect

    import hive_ide.wake as wake_module

    monkeypatch.setattr(wake_module.IdeWake, "wake", staticmethod(slow))

    started = time.monotonic()
    rc = IdeNav.main(
        [
            "nav",
            "--state-home",
            "/tmp/state",
            "--workspace-key",
            "/tmp/ws",
            "--session-id",
            "sid-alpha",
            "--tmux-socket",
            "sock",
            "--direction",
            "next",
        ]
    )
    elapsed = time.monotonic() - started

    assert rc == 0
    assert "select-window" in events, "the budget stopped the switch entirely"
    assert elapsed < 2.0, (
        f"navigation waited {elapsed:.1f}s on the repaint; the budget is not "
        "bounding it and a stalled checkout will hang a keypress"
    )
