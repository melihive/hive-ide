"""The sidebar of the window tmux just switched to must repaint at once — and must
never go blank doing it.

Only the displayed window runs a sidebar process, so a backgrounded pane keeps the
frame it last painted — and because the list is activity-ordered, that frame goes
stale while you are away. Switching to the window then showed a wrong ORDER until
the keep-alive shell next probed, up to a second later.

The first fix respawned the pane. That DID repaint it, but `respawn-pane -k` clears
the pane first and then waits on a shell probe and an interpreter, so every switch
showed an empty sidebar instead of a stale one. Measured: 1492ms stale before,
401ms blank after, 73ms correct once it paints instead.

So the wake now PAINTS: it renders the canonical frame and writes it to the pane's
tty, where the pane's own process writes, so tmux renders it as that pane's output
drawn over the stale frame. Nothing is cleared and nothing is started in the pane.
These cover that contract, the "never respawn again" regression, the refusal to
paint a pane it cannot address, and the fail-open promise.
"""
from __future__ import annotations

import pytest

from hive_ide.frame import Frame
from hive_ide.sidebar import IdeSidebar
from hive_ide.wake import IdeWake


class _Ok:
    returncode = 0
    stdout = "/dev/pts/7"
    stderr = ""


@pytest.fixture
def painted(monkeypatch):
    """Record what a wake would paint, and onto which tty."""
    calls: list[tuple[str, str]] = []
    panes: dict[str, dict[str, str]] = {"roles": {"sidebar": "%8", "agent": "%9"}}

    monkeypatch.setattr(Frame, "role_panes", lambda self, session_id: panes["roles"])
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: f"/dev/pts/{pane_id}")
    monkeypatch.setattr(
        IdeSidebar,
        "paint_tty",
        classmethod(
            lambda cls, skill_dir, repo, session_id, tty: (
                calls.append((session_id, tty)) or True
            )
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


def test_wake_paints_the_pane_holding_the_sidebar_role(store, painted):
    calls, _panes = painted
    record = _session(store)

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is True
    assert calls == [(record["id"], "/dev/pts/%8")]


def test_wake_does_not_respawn_the_pane(store, monkeypatch):
    """The regression this file exists for.

    Respawning is what made the sidebar blank on every switch: the pane is cleared
    and then nothing is on screen until a shell probe and an interpreter have run.
    The wake must paint the existing pane and issue no respawn of any kind — neither
    a bare tmux `respawn-pane` nor the role helper.
    """
    record = _session(store, "BRAVO")
    monkeypatch.setattr(Frame, "role_panes", lambda self, session_id: {"sidebar": "%4"})
    tmux_calls: list[list[str]] = []
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: tmux_calls.append(list(args)) or _Ok()
    )

    def must_not_respawn(self, rec, role, pane_id, **kw):
        raise AssertionError("the wake respawned the pane, which blanks it")

    monkeypatch.setattr(Frame, "respawn_role_pane", must_not_respawn)
    monkeypatch.setattr(
        IdeSidebar, "paint_tty", classmethod(lambda cls, *a: True)
    )

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is True
    assert not [a for a in tmux_calls if a and a[0] in ("respawn-pane", "kill-pane")], (
        f"the wake destroyed or respawned the pane: {tmux_calls}"
    )


def test_wake_resolves_the_pane_by_role_not_by_index(store, painted):
    """Pane order drifts, and `relayout --mode=adopt` re-sorts panes on this same
    hook — so an index-based target races it and paints the agent pane."""
    calls, panes = painted
    panes["roles"] = {"agent": "%1", "plan": "%2", "sidebar": "%3"}
    record = _session(store)

    IdeWake.wake(store.home, store.workspace_key, record["id"], "sock")

    assert calls == [(record["id"], "/dev/pts/%3")]


def test_wake_does_nothing_when_the_window_has_no_sidebar_role(store, painted):
    calls, panes = painted
    panes["roles"] = {"agent": "%9", "plan": "%10"}
    record = _session(store)

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is False
    assert calls == []


def test_wake_does_not_paint_a_pane_whose_tty_it_cannot_resolve(store, monkeypatch):
    """`pane_tty` returns "" when tmux cannot answer. That is "could not observe",
    not "paint it anyway" — writing to a guessed path is how you scribble on another
    terminal. The stale frame is the correct outcome here."""
    record = _session(store)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"sidebar": "%8"})
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: "")
    calls: list[tuple] = []
    monkeypatch.setattr(
        IdeSidebar,
        "paint_tty",
        classmethod(lambda cls, *a: calls.append(a) or True),
    )

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is False
    assert calls == []


def test_wake_does_nothing_for_an_unknown_session(store, painted):
    calls, _panes = painted

    assert IdeWake.wake(store.home, store.workspace_key, "nosuchsession", "sock") is False
    assert calls == []


def test_wake_is_fail_open_when_the_paint_raises(store, monkeypatch):
    """A pane that disappeared mid-switch, or any tmux failure, must not wedge the
    window switch."""
    record = _session(store)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"sidebar": "%8"})
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: "/dev/pts/7")

    def boom(cls, *a):
        raise RuntimeError("pane vanished")

    monkeypatch.setattr(IdeSidebar, "paint_tty", classmethod(boom))

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
        IdeWake, "wake", staticmethod(lambda *a: seen.append(a) or True)
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
