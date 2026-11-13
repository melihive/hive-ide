"""Two sessions on one socket, with a real tmux server.

The per-session hook stamp exists because `bind_keys` writes two kinds of state
that tmux keeps at different scopes: key bindings are server-wide, hooks are per
session. A single stamp let one workspace's refresh vouch for another's stale
hooks.

Closing that opened a second door. A session with no stamp of its own predates
the option, and for the common case — one workspace, one socket — the
server-wide stamp IS about it and is the only evidence there is; ignoring it
would report every server in the field as unknown and drop the upgrade warning
they need. But on a SHARED socket that same fallback reads a sibling's refresh
as this session's, which is the original bug walked back in through the
migration door.

These run against a real tmux server on purpose. A mocked two-session test
cannot find this, because the mock decides what each stamp read returns — the
whole question is which session a real tmux option belongs to.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from hive_ide import __version__
from hive_ide.frame import Frame
from hive_ide.store import StateStore

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is unavailable"
)


@pytest.fixture
def two_frames(tmp_path):
    """Two workspaces whose frames share one tmux socket."""
    socket = f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    first, second = tmp_path / "wsA", tmp_path / "wsB"
    first.mkdir()
    second.mkdir()
    frames = (
        Frame(StateStore(tmp_path / "state", first), socket=socket),
        Frame(StateStore(tmp_path / "state", second), socket=socket),
    )
    try:
        yield frames
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"], capture_output=True, text=True
        )


def _start(frame: Frame) -> None:
    subprocess.run(
        ["tmux", "-L", frame.socket, "new-session", "-d", "-s", frame.target, "sh"],
        capture_output=True,
        text=True,
    )


def _refresh(frame: Frame) -> None:
    """What `bind_keys` writes: the server-wide stamp AND this session's."""
    subprocess.run(
        ["tmux", "-L", frame.socket, "set-option", "-g",
         "@hive_ide_bound_version", __version__],
        capture_output=True, text=True,
    )
    subprocess.run(
        ["tmux", "-L", frame.socket, "set-option", "-t", frame.target,
         Frame.HOOKS_VERSION_OPTION, __version__],
        capture_output=True, text=True,
    )


def test_a_lone_session_is_still_judged_by_the_server_wide_stamp(two_frames):
    """Migration. Every server in the field has only the old stamp."""
    alone, _other = two_frames
    _start(alone)
    subprocess.run(
        ["tmux", "-L", alone.socket, "set-option", "-g",
         "@hive_ide_bound_version", "1.0.100"],
        capture_output=True, text=True,
    )

    assert alone.binding_version() == "1.0.100", (
        "a server carrying only the legacy stamp was reported unknown, which "
        "drops the upgrade warning for every frame in the field"
    )


def test_a_sibling_refresh_does_not_vouch_for_an_unstamped_session(two_frames):
    """THE test. The migration fallback must not re-open the original hole."""
    refreshed, stale = two_frames
    _start(refreshed)
    _start(stale)
    _refresh(refreshed)

    assert refreshed.binding_version() == __version__
    assert stale.binding_version() is None, (
        "an un-refreshed session read its sibling's server-wide stamp as its "
        "own, so its stale hooks report as current"
    )


def test_a_session_with_its_own_stamp_ignores_the_server_wide_one(two_frames):
    """Precedence: once a session is stamped, the global one cannot speak for it."""
    mine, other = two_frames
    _start(mine)
    _start(other)
    _refresh(mine)
    subprocess.run(
        ["tmux", "-L", mine.socket, "set-option", "-g",
         "@hive_ide_bound_version", "0.0.1"],
        capture_output=True, text=True,
    )

    assert mine.binding_version() == __version__, (
        "a session with its own stamp was overruled by the server-wide one"
    )
