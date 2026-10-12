"""The sidebar of the window tmux just switched to must repaint at once.

Only the displayed window runs a sidebar process, so a backgrounded pane keeps the
frame it last painted — and because the list is activity-ordered, that frame goes
stale while you are away. Switching to the window then showed a wrong ORDER until
the keep-alive shell next probed, up to a second later.

The wake respawns that pane. It must do so through `Frame.respawn_role_pane` and
never a bare tmux `respawn-pane`, because the helper carries three guarantees a
bare respawn silently drops: the pane is found by ROLE (pane order drifts, and
`relayout --mode=adopt` re-sorts panes on this same hook), it is spawned with THIS
record's `HIVE_IDE_*` variables rather than whatever identity the tmux session
environment is holding, and a pane that cannot be proven not to be the caller's
own is left alone. These cover that contract and the fail-open promise.
"""
from __future__ import annotations

import pytest

from hive_ide.frame import Frame
from hive_ide.wake import IdeWake


@pytest.fixture
def woken(monkeypatch):
    """Record which (session, role, pane) a wake would respawn."""
    calls: list[tuple[str, str, str]] = []
    panes: dict[str, dict[str, str]] = {"roles": {"sidebar": "%8", "agent": "%9"}}

    monkeypatch.setattr(
        Frame, "role_panes", lambda self, session_id: panes["roles"]
    )
    monkeypatch.setattr(
        Frame,
        "respawn_role_pane",
        lambda self, record, role, pane_id, **kw: (
            calls.append((record["id"], role, pane_id)) or True
        ),
    )
    return calls, panes


def _session(store, name: str = "ALPHA") -> dict:
    from hive_ide.drivers import bundled_drivers

    driver = bundled_drivers()["term"]
    return store.create_session(
        name=name,
        working_dir=store.workspace_key,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=driver.resolve(
            name=name, working_dir=str(store.workspace_key), conversation_reference=None
        ),
    )


@pytest.fixture
def store(tmp_path):
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def test_wake_respawns_the_pane_holding_the_sidebar_role(store, woken):
    calls, _panes = woken
    record = _session(store)

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is True
    assert calls == [(record["id"], "sidebar", "%8")]


def test_wake_goes_through_the_role_respawn_not_a_bare_tmux_respawn(store, monkeypatch):
    """A bare `respawn-pane` rebuilds the pane's env from the tmux SESSION
    environment, which holds whichever identity the frame's first window left
    there — so every woken sidebar would claim the wrong session. The wake must
    delegate to the helper that passes this record's own `HIVE_IDE_*` values."""
    record = _session(store, "BRAVO")
    monkeypatch.setattr(
        Frame, "role_panes", lambda self, session_id: {"sidebar": "%4"}
    )
    tmux_calls: list[list[str]] = []
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda self, args, **kw: tmux_calls.append(list(args)) or _Ok(),
    )
    respawned: list[tuple[str, str]] = []
    monkeypatch.setattr(
        Frame,
        "respawn_role_pane",
        lambda self, rec, role, pane_id, **kw: (
            respawned.append((role, pane_id)) or True
        ),
    )

    IdeWake.wake(store.home, store.workspace_key, record["id"], "sock")

    assert respawned == [("sidebar", "%4")]
    assert not [args for args in tmux_calls if args and args[0] == "respawn-pane"], (
        "the wake issued its own respawn-pane and bypassed the role helper"
    )


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


def test_wake_does_nothing_when_the_window_has_no_sidebar_role(store, woken):
    calls, panes = woken
    panes["roles"] = {"agent": "%9", "plan": "%10"}
    record = _session(store)

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is False
    assert calls == []


def test_wake_does_nothing_for_an_unknown_session(store, woken):
    calls, _panes = woken

    assert IdeWake.wake(store.home, store.workspace_key, "nosuchsession", "sock") is False
    assert calls == []


def test_wake_is_fail_open_when_the_respawn_raises(store, monkeypatch):
    """A lease on the pane, or any tmux failure, must not wedge the window switch."""
    record = _session(store)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"sidebar": "%8"})

    def boom(self, rec, role, pane_id, **kw):
        raise RuntimeError("pane is leased")

    monkeypatch.setattr(Frame, "respawn_role_pane", boom)

    assert IdeWake.main(
        [
            "--state-home", str(store.home),
            "--workspace-key", str(store.workspace_key),
            "--session-id", record["id"],
            "--tmux-socket", "sock",
        ]
    ) == 0


def test_wake_main_reaches_the_wake_with_the_hooks_arguments(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        IdeWake,
        "wake",
        staticmethod(lambda *a: seen.append(a) or True),
    )

    rc = IdeWake.main(
        [
            "--state-home", "/s",
            "--workspace-key", "/w",
            "--session-id", "abc",
            "--tmux-socket", "sock-x",
        ]
    )

    assert rc == 0
    assert [tuple(str(x) for x in call) for call in seen] == [("/s", "/w", "abc", "sock-x")]
