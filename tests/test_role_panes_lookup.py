"""`role_panes()` asks tmux once, and only about its own session.

It used to list EVERY window in the frame just to turn a session id into a
window id, then list that window's panes. At 141 calls in one warm `open` that
was 282 subprocesses for this method alone. `list-panes -s` lists the whole
session's panes in one go, and a pane resolves its own window's
`@hive_ide_session_id` through tmux's option hierarchy — so one listing answers
both questions.

Against a real tmux server, because the premise is a claim about what tmux
resolves in a pane's format context, and a fake would just be me asserting my
own assumption back at myself.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from hive_ide.frame import Frame
from hive_ide.store import StateStore

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is unavailable"
)


@pytest.fixture
def frame(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    socket = f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    made = Frame(StateStore(tmp_path / "state", workspace), socket=socket)
    try:
        yield made
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"], capture_output=True, text=True
        )


def _tmux(frame, *args) -> str:
    return subprocess.run(
        ["tmux", "-L", frame.socket, *args], capture_output=True, text=True
    ).stdout.strip()


def _window(frame, session_id: str, roles: dict[str, str]) -> dict[str, str]:
    """A window tagged for `session_id`, with its panes tagged by role."""
    # By RETURN CODE: `has-session` prints nothing on success, so testing its
    # stdout said "no session" every time and re-tagged the same window.
    alive = subprocess.run(
        ["tmux", "-L", frame.socket, "has-session", "-t", frame.target],
        capture_output=True, text=True,
    ).returncode == 0
    if not alive:
        subprocess.run(
            ["tmux", "-L", frame.socket, "new-session", "-d", "-s", frame.target, "sh"],
            capture_output=True, text=True,
        )
        window = _tmux(frame, "display-message", "-p", "-t", frame.target, "#{window_id}")
    else:
        window = _tmux(frame, "new-window", "-d", "-P", "-F", "#{window_id}", "-t",
                       frame.target, "sh")
    _tmux(frame, "set-option", "-w", "-t", window, "@hive_ide_session_id", session_id)
    made: dict[str, str] = {}
    first = _tmux(frame, "list-panes", "-t", window, "-F", "#{pane_id}").split()[0]
    for index, role in enumerate(roles):
        pane = first if index == 0 else _tmux(
            frame, "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", window, "sh"
        )
        _tmux(frame, "set-option", "-p", "-t", pane, "@hive_ide_pane", role)
        made[role] = pane
    return made


def test_a_session_reports_its_own_roles(frame):
    expected = _window(frame, "s1", {"sidebar": "", "agent": "", "plan": ""})

    assert frame.role_panes("s1") == expected


def test_another_sessions_panes_are_not_mixed_in(frame):
    """The filter is the whole safety of listing the session at once."""
    mine = _window(frame, "s1", {"sidebar": "", "agent": ""})
    _window(frame, "s2", {"sidebar": "", "agent": "", "plan": ""})

    assert frame.role_panes("s1") == mine, "another session's panes leaked in"
    assert set(frame.role_panes("s2")) == {"sidebar", "agent", "plan"}


def test_an_absent_session_is_empty_not_unknown(frame):
    """A successful listing that simply lacks the session means absent."""
    _window(frame, "s1", {"agent": ""})

    assert frame.role_panes("nosuch") == {}


def test_untagged_panes_are_ignored(frame):
    _window(frame, "s1", {})

    assert frame.role_panes("s1") == {}


def test_an_empty_session_id_matches_nothing(frame):
    """An untagged window renders an EMPTY session tag, so `""` would match it."""
    _window(frame, "", {"agent": ""})

    assert frame.role_panes("") == {}


def test_only_the_selected_windows_roles_are_reported(frame):
    """Two windows can carry the same session tag. Merging them is a lie.

    Listing the session at once means panes arrive with no window identity
    unless the format carries it. Without it, a window holding only a sidebar
    gets reported as holding all three roles, borrowed from the other window --
    which hides a missing pane from repair and aims a mutation at a pane in a
    window nobody selected.
    """
    _window(frame, "s1", {"sidebar": "", "agent": "", "plan": ""})
    selected = _window(frame, "s1", {"sidebar": ""})

    roles = frame.role_panes("s1")

    assert roles == selected, "roles were merged across two windows with the same tag"
    # Tied to `windows()` on purpose: the two must never select different
    # windows, or a caller reading one and mutating via the other corrupts the
    # window it did not look at.
    assert frame.windows()["s1"] == _tmux(
        frame, "display-message", "-p", "-t", selected["sidebar"], "#{window_id}"
    )


def test_a_failed_listing_is_unknown_while_the_session_is_there(frame, monkeypatch):
    """The deliberate semantic change, and the direction it fails in.

    `None` reads as "could not observe" and makes callers defer. Deferring costs
    a retry; rebuilding on a misread destroys a live window, so the unknown
    routes to the inert outcome.
    """
    _window(frame, "s1", {"agent": ""})
    real = Frame.tmux

    def failing_listing(self, args, **kwargs):
        if args[:1] == ["list-panes"] and "-s" in args:
            return subprocess.CompletedProcess(args, 1, "", "server exited unexpectedly")
        return real(self, args, **kwargs)

    monkeypatch.setattr(Frame, "tmux", failing_listing)

    assert frame.role_panes("s1") is None


def test_a_stopped_server_is_absence_not_unknown(frame):
    """Confirmed absence has to stay absence, or a reboot strands the lease.

    A persisted plan lease outlives the tmux server. On the next run the window
    is genuinely gone, and `PaneLeases.restore()` must reach its reconstruction
    path to clear the lease and rebuild. Answering `None` here raises
    `pane_unobservable` (503) instead -- even with `force=True` -- and the
    session can never be repaired.
    """
    _window(frame, "s1", {"agent": ""})
    subprocess.run(
        ["tmux", "-L", frame.socket, "kill-server"], capture_output=True, text=True
    )

    assert frame.exists() is False
    assert frame.role_panes("s1") == {}


def test_a_missing_session_on_a_live_server_is_absence(frame):
    """Same answer, different absence: the server is up, the session is not."""
    _window(frame, "s1", {"agent": ""})
    other = Frame(frame.store, socket=frame.socket)
    other.target = "no-such-session"

    assert other.exists() is False
    assert other.role_panes("s1") == {}


def test_the_happy_path_is_one_tmux_call(frame, monkeypatch):
    _window(frame, "s1", {"agent": ""})
    ran: list[str] = []
    real = Frame.tmux

    def counting(self, args, **kwargs):
        ran.append(args[0])
        return real(self, args, **kwargs)

    monkeypatch.setattr(Frame, "tmux", counting)
    frame.role_panes("s1")

    assert ran == ["list-panes"], f"spent more than one tmux call: {ran}"


def test_the_absence_probe_costs_nothing_until_the_listing_fails(frame, monkeypatch):
    """The `has-session` probe is on the failure path only."""
    _window(frame, "s1", {"agent": ""})
    ran: list[str] = []
    real = Frame.tmux

    def failing_listing(self, args, **kwargs):
        ran.append(args[0])
        if args[:1] == ["list-panes"] and "-s" in args:
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(self, args, **kwargs)

    monkeypatch.setattr(Frame, "tmux", failing_listing)
    frame.role_panes("s1")

    assert ran == ["list-panes", "has-session"], ran
