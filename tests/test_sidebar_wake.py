"""The sidebar of the window tmux just switched to must repaint at once.

Only the displayed window runs a sidebar process, so a backgrounded pane keeps the
frame it last painted — and because the list is activity-ordered, that frame goes
stale while you are away. Switching to the window then shows a wrong ORDER until the
keep-alive shell next probes, up to a second later. The frame's
`session-window-changed` hook calls `--wake` to respawn that pane immediately.

These cover the resolution rule, which is the part that can do damage: the pane is
found by its `@hive_ide_pane` role, never by index, because `relayout --mode=adopt`
re-sorts panes on this same hook and index 0 holding the agent would make the wake
kill the agent the user is talking to.
"""
from __future__ import annotations

import pytest

from hive_ide.sidebar import IdeSidebar


@pytest.fixture
def tmux_calls(monkeypatch):
    """Record tmux calls; `rows` is what `list-panes` returns."""
    calls: list[list[str]] = []
    state: dict[str, str | None] = {"rows": ""}

    def fake(socket: str, args: list[str]) -> str | None:
        calls.append(args)
        if args and args[0] == "list-panes":
            return state["rows"]
        return ""

    monkeypatch.setattr(IdeSidebar, "_tmux_capture", staticmethod(fake))
    return calls, state


def _respawned(calls: list[list[str]]) -> list[str]:
    return [args[-1] for args in calls if args and args[0] == "respawn-pane"]


def test_wake_respawns_the_pane_holding_the_sidebar_role(tmux_calls, monkeypatch):
    calls, state = tmux_calls
    monkeypatch.delenv("TMUX_PANE", raising=False)
    # The sidebar is deliberately NOT first here: resolution must not use order.
    state["rows"] = "agent\t%7\nsidebar\t%8\nplan\t%9\n"

    assert IdeSidebar._wake_sidebar_pane("sock", "@3") == 0
    assert _respawned(calls) == ["%8"]


def test_wake_never_respawns_a_pane_that_is_not_the_sidebar(tmux_calls, monkeypatch):
    """Index 0 is the agent here. An index-based wake would kill the live agent."""
    calls, state = tmux_calls
    monkeypatch.delenv("TMUX_PANE", raising=False)
    state["rows"] = "agent\t%1\nplan\t%2\n"

    assert IdeSidebar._wake_sidebar_pane("sock", "@3") == 0
    assert _respawned(calls) == []


def test_wake_does_nothing_when_the_window_cannot_be_observed(monkeypatch):
    """`None` from tmux is "could not observe", which is not "pane 0 then"."""
    calls: list[list[str]] = []

    def fake(socket: str, args: list[str]) -> str | None:
        calls.append(args)
        return None

    monkeypatch.setattr(IdeSidebar, "_tmux_capture", staticmethod(fake))
    monkeypatch.delenv("TMUX_PANE", raising=False)

    assert IdeSidebar._wake_sidebar_pane("sock", "@3") == 0
    assert _respawned(calls) == []


def test_wake_refuses_an_ambiguous_window_with_two_sidebar_panes(tmux_calls, monkeypatch):
    calls, state = tmux_calls
    monkeypatch.delenv("TMUX_PANE", raising=False)
    state["rows"] = "sidebar\t%1\nsidebar\t%2\n"

    assert IdeSidebar._wake_sidebar_pane("sock", "@3") == 0
    assert _respawned(calls) == []


def test_wake_never_respawns_the_pane_it_is_running_in(tmux_calls, monkeypatch):
    calls, state = tmux_calls
    state["rows"] = "sidebar\t%8\n"
    monkeypatch.setenv("TMUX_PANE", "%8")

    assert IdeSidebar._wake_sidebar_pane("sock", "@3") == 0
    assert _respawned(calls) == []


def test_wake_mode_needs_no_state_home_and_reaches_the_waker(monkeypatch):
    """The hook passes only `--window`/`--tmux-socket`; `main` must not demand state."""
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        IdeSidebar,
        "_wake_sidebar_pane",
        staticmethod(lambda socket, window: seen.append((socket, window)) or 0),
    )

    rc = IdeSidebar.main(["--wake", "--window", "@4", "--tmux-socket", "sock-x"])

    assert rc == 0
    assert seen == [("sock-x", "@4")]
