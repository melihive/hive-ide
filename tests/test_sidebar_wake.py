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

import os

import pytest

from hive_ide.frame import Frame
from hive_ide.sidebar import IdeSidebar
from hive_ide.wake import IdeWake


class _Ok:
    returncode = 0
    stdout = "/dev/pts/7"
    stderr = ""


@pytest.fixture
def pty_path():
    """A real tty the wake can actually open, since it opens one for itself now."""
    master, slave = os.openpty()
    try:
        yield os.ttyname(slave)
    finally:
        os.close(master)
        os.close(slave)


@pytest.fixture
def painted(monkeypatch, pty_path):
    """Record what a wake would paint, and which pane it resolved."""
    calls: list[tuple[str, str]] = []
    panes: dict[str, dict[str, str]] = {"roles": {"sidebar": "%8", "agent": "%9"}}
    seen_panes: list[str] = []

    def pane_tty(self, pane_id):
        seen_panes.append(pane_id)
        return pty_path

    monkeypatch.setattr(Frame, "role_panes", lambda self, session_id: panes["roles"])
    monkeypatch.setattr(Frame, "pane_tty", pane_tty)
    monkeypatch.setattr(Frame, "pane_still_is", lambda self, pane_id, **kw: True)
    monkeypatch.setattr(
        IdeSidebar,
        "paint_fd",
        classmethod(
            lambda cls, skill_dir, repo, session_id, fd: (
                calls.append((session_id, seen_panes[0])) or True
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
    assert calls == [(record["id"], "%8")]


def test_wake_does_not_respawn_the_pane(store, monkeypatch, pty_path):
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
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: pty_path)
    monkeypatch.setattr(Frame, "pane_still_is", lambda self, pane_id, **kw: True)
    monkeypatch.setattr(IdeSidebar, "paint_fd", classmethod(lambda cls, *a: True))

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

    assert calls == [(record["id"], "%3")]


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
        "paint_fd",
        classmethod(lambda cls, *a: calls.append(a) or True),
    )

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is False
    assert calls == []


def test_wake_does_nothing_for_an_unknown_session(store, painted):
    calls, _panes = painted

    assert IdeWake.wake(store.home, store.workspace_key, "nosuchsession", "sock") is False
    assert calls == []


def test_wake_is_fail_open_when_the_paint_raises(store, monkeypatch, pty_path):
    """A pane that disappeared mid-switch, or any tmux failure, must not wedge the
    window switch."""
    record = _session(store)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"sidebar": "%8"})
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: pty_path)
    monkeypatch.setattr(Frame, "pane_still_is", lambda self, pane_id, **kw: True)

    def boom(cls, *a):
        raise RuntimeError("pane vanished")

    monkeypatch.setattr(IdeSidebar, "paint_fd", classmethod(boom))

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


def test_pane_tty_refuses_a_pane_tmux_reports_as_dead(store, monkeypatch):
    """The finding that makes liveness a safety property, not a nicety.

    A pane kept after its process exited still REPORTS its old tty, but tmux has
    closed that pty — and the kernel is free to hand the same `/dev/pts/N` to the
    next pane that opens one. Painting the dead pane's reported path then paints a
    completely unrelated pane. Reproduced on an isolated tmux server during review,
    and it needs no race: `remain-on-exit` preserves the stale association.
    """
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Reply("1\t/dev/pts/3")
    )
    assert frame.pane_tty("%8") == ""

    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Reply("0\t/dev/pts/3")
    )
    assert frame.pane_tty("%8") == "/dev/pts/3"


def test_pane_tty_refuses_anything_that_is_not_a_device_path(store, monkeypatch):
    """tmux prints an empty field for a pane with no tty; "" is not a path to open."""
    frame = Frame(store, socket="sock")
    for reply in ("0\t", "0\tnot-a-device", ""):
        monkeypatch.setattr(Frame, "tmux", lambda self, args, _r=reply, **kw: _Reply(_r))
        assert frame.pane_tty("%8") == "", reply


def test_wake_does_not_paint_when_the_tty_changed_under_it(store, monkeypatch, pty_path):
    """Re-verified while the descriptor is held: if the pane's tty is no longer the
    one that was opened, the open landed on someone else's pty and must not be
    written to."""
    record = _session(store)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"sidebar": "%8"})
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: pty_path)
    # tmux now answers with a DIFFERENT tty than the one that was opened.
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda self, args, **kw: _Reply("0\t/dev/pts/999\tsidebar\t" + record["id"]),
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        IdeSidebar, "paint_fd", classmethod(lambda cls, *a: calls.append(a) or True)
    )

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is False
    assert calls == []


def test_wake_does_not_paint_when_the_sidebar_role_moved_to_another_pane(
    store, monkeypatch, pty_path
):
    """`relayout --mode=adopt` runs on this same hook and can re-sort panes, so the
    role may have moved between resolving it and holding its descriptor."""
    record = _session(store)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"sidebar": "%8"})
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: pty_path)
    # The pane we opened now reports a different role: adopt moved the sidebar.
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda self, args, **kw: _Reply(
            "0\t" + pty_path + "\tagent\t" + record["id"]
        ),
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        IdeSidebar, "paint_fd", classmethod(lambda cls, *a: calls.append(a) or True)
    )

    assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is False
    assert calls == []


def test_wake_closes_the_descriptor_it_opened(store, monkeypatch, pty_path):
    """This runs on EVERY window switch; a leaked fd per switch is a wedged frame
    after a day of normal use."""
    record = _session(store)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"sidebar": "%8"})
    monkeypatch.setattr(Frame, "pane_tty", lambda self, pane_id: pty_path)
    monkeypatch.setattr(Frame, "pane_still_is", lambda self, pane_id, **kw: True)
    monkeypatch.setattr(IdeSidebar, "paint_fd", classmethod(lambda cls, *a: True))

    opened = []
    real_open = os.open

    def observe_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == pty_path:
            opened.append(fd)
        return fd

    monkeypatch.setattr(os, "open", observe_open)
    for _ in range(12):
        assert IdeWake.wake(store.home, store.workspace_key, record["id"], "sock") is True
        assert opened, "wake must actually open the pane's tty"
        with pytest.raises(OSError) as error:
            os.fstat(opened[-1])
        assert error.value.errno == 9  # EBADF, not another observation failure
    assert len(opened) == 12


class _Reply:
    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""
