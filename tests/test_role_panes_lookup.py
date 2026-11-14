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


def test_a_colliding_window_name_cannot_hijack_the_listing(frame):
    """`-s` does not make `-t` a session target. It still resolves a WINDOW first.

    A bare `-t <name>` matches a window of that name in whatever session tmux
    considers current, and `-s` then lists THAT session's panes. A successful
    listing of the wrong session reads as "no roles here", which licenses a
    rebuild of a healthy window. `={name}:` forces an exact session target.
    """
    expected = _window(frame, "s1", {"sidebar": "", "agent": ""})
    # A decoy session created LAST, so tmux treats it as current, holding a
    # window named exactly like this frame's session.
    _tmux(frame, "new-session", "-d", "-s", "decoy", "sh")
    _tmux(frame, "rename-window", "-t", "decoy", frame.target)
    _tmux(frame, "set-option", "-w", "-t", "decoy", "@hive_ide_session_id", "DECOY")

    # The premise, pinned: the bare form really does read the decoy. If tmux
    # ever changes this, this line fails and the guard above is what is stale --
    # rather than the test quietly passing for a new reason.
    hijacked = _tmux(
        frame, "list-panes", "-s", "-t", frame.target, "-F", "#{@hive_ide_session_id}"
    )
    assert "DECOY" in hijacked, f"tmux no longer resolves a bare -t as a window: {hijacked}"

    assert frame.role_panes("s1") == expected


def test_a_failed_fast_path_falls_back_to_the_old_two_calls(frame, monkeypatch):
    """No new answer is invented from one failure: the old path still answers.

    A single failed listing cannot tell "the window is gone" from "I could not
    look", and those want opposite outcomes. `windows()` plus the per-window
    pane listing can, and that is the shipped contract.
    """
    expected = _window(frame, "s1", {"agent": ""})
    ran: list[str] = []
    real = Frame.tmux

    def failing_fast_path(self, args, **kwargs):
        ran.append(args[0])
        if args[:1] == ["list-panes"] and "-s" in args:
            return subprocess.CompletedProcess(args, 1, "", "server exited unexpectedly")
        return real(self, args, **kwargs)

    monkeypatch.setattr(Frame, "tmux", failing_fast_path)

    assert frame.role_panes("s1") == expected
    assert ran == ["list-panes", "list-windows", "list-panes"], ran


def test_panes_that_cannot_be_observed_at_all_are_unknown(frame, monkeypatch):
    """The window is found, but no pane listing works: unknown, never absence.

    Deferring costs a retry; rebuilding on a misread destroys a live window.
    """
    _window(frame, "s1", {"agent": ""})
    real = Frame.tmux

    def failing_pane_listings(self, args, **kwargs):
        if args[:1] == ["list-panes"]:
            return subprocess.CompletedProcess(args, 1, "", "server exited unexpectedly")
        return real(self, args, **kwargs)

    monkeypatch.setattr(Frame, "tmux", failing_pane_listings)

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
