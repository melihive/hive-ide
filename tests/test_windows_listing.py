"""`windows()` asks tmux once, not twice.

It used to probe `has-session` before every listing. The listing already
answers that: tmux exits non-zero both when the server is absent ("error
connecting to <socket>") and when the session is missing ("can't find
session"), and zero only when it is there.

`windows()` runs 239 times in one warm `open`, each call paying for both
processes, which made this the most repeated redundant process in a launch —
258 of a warm open's 1025 tmux spawns were that probe.

What must NOT change is the answer. These pin the behaviour across all three
states with a real tmux server, because the premise is a claim about what tmux
actually returns, and a fake would simply be me asserting my own assumption.
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


def test_no_server_at_all_is_no_windows(frame):
    assert frame.windows() == {}


def test_a_server_without_this_session_is_no_windows(frame):
    subprocess.run(
        ["tmux", "-L", frame.socket, "new-session", "-d", "-s", "someone-else", "sh"],
        capture_output=True,
        text=True,
    )

    assert frame.windows() == {}, (
        "another workspace's session on the same socket was read as this one's"
    )


def test_a_live_session_reports_its_tagged_windows(frame):
    subprocess.run(
        ["tmux", "-L", frame.socket, "new-session", "-d", "-s", frame.target, "sh"],
        capture_output=True,
        text=True,
    )
    window = subprocess.run(
        ["tmux", "-L", frame.socket, "display-message", "-p", "-t", frame.target,
         "#{window_id}"],
        capture_output=True, text=True,
    ).stdout.strip()
    subprocess.run(
        ["tmux", "-L", frame.socket, "set-option", "-w", "-t", window,
         "@hive_ide_session_id", "s1"],
        capture_output=True, text=True,
    )

    assert frame.windows() == {"s1": window}


def test_an_untagged_window_is_not_reported(frame):
    """Only windows this package owns. A bare window carries no session id."""
    subprocess.run(
        ["tmux", "-L", frame.socket, "new-session", "-d", "-s", frame.target, "sh"],
        capture_output=True,
        text=True,
    )

    assert frame.windows() == {}


def test_the_listing_is_one_process_per_call(frame, monkeypatch):
    """The point of the change: no `has-session` probe before the listing."""
    ran: list[str] = []
    real = Frame.tmux

    def counting(self, args, **kwargs):
        ran.append(args[0])
        return real(self, args, **kwargs)

    monkeypatch.setattr(Frame, "tmux", counting)
    frame.windows()

    assert ran == ["list-windows"], (
        f"windows() spent more than one tmux call to answer: {ran}"
    )


def test_a_failed_listing_is_not_parsed_even_if_it_printed(frame, monkeypatch):
    """Could-not-observe is not an empty frame.

    Real tmux writes its error to stderr, so with a live server the guard looks
    redundant — a mutation run confirmed no real-tmux test can see it. But a
    command can fail AND have written to stdout: a partial listing, or an error
    on the same stream. Parsing that would invent windows that may not exist,
    and callers build on absence. The same rule is already pinned for
    `attached_clients`; this is its sibling.
    """
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda self, args, **kw: subprocess.CompletedProcess(
            args, 1, "s1\t@1\ns2\t@2\n", "can't find session"
        ),
    )

    assert frame.windows() == {}, (
        "parsed the output of a failed listing, inventing windows from a call "
        "that did not succeed"
    )

