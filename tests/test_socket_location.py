"""Where a frame's tmux socket lives, and why a running one never moves.

`/tmp` is age-cleaned — `q /tmp … 10d` with systemd-tmpfiles-clean.timer active
on a stock Fedora — so a frame's socket can be deleted from under a server that
is still running with every pane in it. The trigger is narrower than it first
looks: tmpfiles removes an entry only when atime, btime, ctime AND mtime are all
older than the age, and connecting refreshes atime, so a frame in USE is never at
risk. What is exposed is a live frame nothing has touched for ten straight days.

So new frames get a durable directory and running frames keep the address they
were started on. The second half is the one that can break things: the config
snapshot is rewritten on every `open`, and re-deciding the address of a server
that is already listening would leave every later call looking somewhere else —
finding nothing, which must never read as "no frame exists, build one".

Real tmux for the end-to-end, because "does tmux put the socket where we told it"
is a question about tmux.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from hive_ide.config import SOCKET_PATH_BUDGET, durable_socket_tmpdir
from hive_ide.frame import Frame
from hive_ide.socket_gc import SocketReaper
from hive_ide.store import StateStore

requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is unavailable"
)


def _frame(tmp_path, *, tmpdir: str | None = None) -> Frame:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    made = Frame(
        StateStore(tmp_path / "state", workspace),
        socket=f"hive-ide-loc-{os.getpid()}-{uuid.uuid4().hex[:6]}",
    )
    made.socket_tmpdir = tmpdir
    return made


# -- choosing the address ----------------------------------------------------------


def test_an_address_already_pinned_is_kept(tmp_path):
    assert durable_socket_tmpdir(
        state_home=tmp_path, socket="s", previous="/already/here",
        listening=lambda p: False,
    ) == "/already/here"


def test_a_listening_legacy_server_keeps_its_address(tmp_path):
    """The hazard: re-deciding the address of a server that is already running."""
    assert durable_socket_tmpdir(
        state_home=tmp_path, socket="s", previous=None, listening=lambda p: True,
    ) is None


def test_a_legacy_address_that_cannot_be_probed_keeps_it_too(tmp_path):
    """Only a definite "nothing is there" frees an address.

    `is_listening` answers None for a permission error or a timeout — unknown,
    not empty. Treating that as free would relocate a server that is very
    probably running and merely unreachable for a moment.
    """
    assert durable_socket_tmpdir(
        state_home=tmp_path, socket="s", previous=None, listening=lambda p: None,
    ) is None


def test_a_fresh_frame_gets_the_durable_directory(tmp_path):
    chosen = durable_socket_tmpdir(
        state_home=tmp_path, socket="s", previous=None, listening=lambda p: False,
    )

    assert chosen == str(tmp_path / "sockets")


def test_a_path_that_would_not_fit_falls_back_to_tmux_default(tmp_path):
    """A socket path over the limit does not degrade the frame — it prevents it.

    `sockaddr_un.sun_path` is 108 bytes on Linux and 104 on macOS. Past that tmux
    cannot create the socket at all, so the durable directory is declined rather
    than handed a frame that cannot start.
    """
    deep = Path("/" + "d" * (SOCKET_PATH_BUDGET + 20))

    assert durable_socket_tmpdir(
        state_home=deep, socket="s", previous=None, listening=lambda p: False,
    ) is None


# -- using it ----------------------------------------------------------------------


def test_the_path_follows_the_pin(tmp_path):
    legacy = _frame(tmp_path)
    pinned = _frame(tmp_path, tmpdir="/durable/base")

    assert legacy.socket_path() == str(
        SocketReaper.default_directory() / legacy.socket
    ), "a frame with no pin must sit exactly where tmux itself would put it"
    assert pinned.socket_path() == str(
        Path("/durable/base") / f"tmux-{os.getuid()}" / pinned.socket
    )


@requires_tmux
def test_tmux_puts_the_socket_where_the_frame_says_and_can_reach_it(tmp_path):
    """The end-to-end, and the reason every call must carry `TMUX_TMPDIR`.

    `-L <name>` resolves under `TMUX_TMPDIR`, so a frame that passes it on some
    calls and not others does not get an error — it gets a SECOND, empty server
    at the other path, which looks exactly like a frame that needs building.
    """
    # A SHORT base, deliberately. pytest's `tmp_path` is ~63 characters deep, and
    # `<tmp_path>/sockets/tmux-1000/<socket name>` came to exactly 108 bytes — the
    # `sun_path` limit — so tmux could not create the socket and this test failed
    # for a reason that had nothing to do with what it checks. Which is also the
    # best evidence that `SOCKET_PATH_BUDGET` is guarding a real limit rather than
    # a theoretical one.
    base = Path(tempfile.mkdtemp(prefix="hs-", dir="/tmp")) / "s"
    frame = _frame(tmp_path, tmpdir=str(base))
    started = frame.tmux(
        ["new-session", "-d", "-s", frame.target, "sh", "-c", "sleep 30"]
    )
    try:
        assert started.returncode == 0, (
            f"the server never started: {started.stderr.strip()!r} — "
            f"path was {len(frame.socket_path())} bytes"
        )
        assert Path(frame.socket_path()).is_socket(), (
            f"no socket at the frame's own address: {frame.socket_path()}"
        )
        # Nothing at the legacy address, so no second server was created.
        assert not (SocketReaper.default_directory() / frame.socket).exists()
        # And the frame can talk to the server it started.
        assert frame.exists() is True
        assert frame.tmux(["list-panes", "-t", f"={frame.target}:"]).returncode == 0
        # A client WITHOUT the directory cannot find it — which is why the
        # directory lives in state and not in the ambient environment.
        blind = subprocess.run(
            ["tmux", "-L", frame.socket, "has-session", "-t", frame.target],
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items() if k != "TMUX_TMPDIR"},
        )
        assert blind.returncode != 0
    finally:
        frame.tmux(["kill-server"])


@requires_tmux
def test_a_legacy_frame_is_left_exactly_where_it_was(tmp_path):
    """No pin, no change: the address an existing frame already uses."""
    frame = _frame(tmp_path)
    frame.tmux(["new-session", "-d", "-s", frame.target, "sh", "-c", "sleep 30"])
    try:
        assert Path(frame.socket_path()).is_socket()
        assert str(Path(frame.socket_path()).parent) == str(
            SocketReaper.default_directory()
        )
        assert frame.exists() is True
    finally:
        frame.tmux(["kill-server"])


@requires_tmux
def test_the_pinned_directory_is_private(tmp_path):
    """0700, asserted. A socket anyone can reach is a tmux server anyone can drive.

    Found by mutation: flipping the mode to 0777 left every other test in this
    file green, because they all check WHERE the socket is and none checked who
    can get at it. `/tmp/tmux-<uid>` is 0700 for this reason and the durable
    directory inherits the requirement, not the location's good manners.
    """
    base = Path(tempfile.mkdtemp(prefix="hp-", dir="/tmp")) / "s"
    frame = _frame(tmp_path, tmpdir=str(base))

    frame.tmux(["has-session", "-t", frame.target])  # any call creates the directory

    assert base.is_dir(), "the pinned directory was never created"
    assert (base.stat().st_mode & 0o777) == 0o700, (
        f"pinned socket directory is {oct(base.stat().st_mode & 0o777)}, not 0700"
    )
