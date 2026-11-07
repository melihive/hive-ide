"""Several screens on one frame: one controller, the rest mirrors.

Until 1.0.94 every `open` left another client attached and nothing reaped them
-- two on most frames across four machines, one with three, the oldest idle for
6.4 days. With `window-size latest` the window sizes itself to whichever client
was active last, so a stale 126x33 beside a live 203x70 flipped the window's
geometry and reflowed every pane whenever focus moved. That is what people
reported as the frame freezing, and 1.0.94 answered it by attaching with `-d`:
one session, one client, and the second screen you wanted was thrown off.

The vote was the problem, not the client. `ignore-size` lets a client watch
without a say in geometry, so the fix keeps every screen attached and gives
exactly one of them -- the most recently active -- control. The decisive test
here is the last one: a mirror resizing its own terminal must not move the
frame. That is the freeze cause, pinned.
"""
from __future__ import annotations

import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid
from pathlib import Path

import pytest

from hive_ide import __version__
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.store import StateStore


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": __version__}


def _frame(tmp_path: Path) -> tuple[Frame, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["term"]
    record = store.create_session(
        name="ALPHA",
        working_dir=workspace,
        source=_source(),
        driver=driver.resolve(
            name="ALPHA", working_dir=str(workspace), conversation_reference=None
        ),
    )
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    return frame, record


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _attach_client(
    socket: str, target: str, *, cols: int, rows: int
) -> tuple[subprocess.Popen, int]:
    """A real tmux client of a given size, on its own pty.

    A client needs a terminal, so there is no way to test this with pipes: the
    size IS the subject. `start_new_session` keeps the client out of pytest's
    process group so a signal sent here never reaches it by accident.
    """
    master, slave = pty.openpty()
    _set_size(slave, cols, rows)
    proc = subprocess.Popen(
        ["tmux", "-L", socket, "attach", "-t", target],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "TERM": "xterm-256color"},
        start_new_session=True,
    )
    os.close(slave)
    return proc, master


def _close(clients: list[tuple[subprocess.Popen, int]]) -> None:
    for proc, fd in clients:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001 — teardown must not mask a real failure
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _wait_for(predicate, *, timeout: float = 15.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def _clients_numbering(frame: Frame, count: int):
    return _wait_for(
        lambda: (
            frame.attached_clients() if len(frame.attached_clients()) == count else None
        )
    )


def _window_size(frame: Frame, window: str) -> str:
    result = frame.tmux(
        ["display-message", "-p", "-t", window, "#{window_width}x#{window_height}"]
    )
    return result.stdout.strip()


def test_a_second_open_mirrors_the_first_client_instead_of_detaching_it(tmp_path):
    """The regression that matters: the first screen stays attached."""
    frame, record = _frame(tmp_path)
    clients: list[tuple[subprocess.Popen, int]] = []
    try:
        frame.open(no_attach=True)
        clients.append(_attach_client(frame.socket, frame.target, cols=100, rows=30))
        assert _clients_numbering(frame, 1), "the first client never attached"

        # Exactly what `open` does in the breath before it execs `tmux attach`.
        assert frame.demote_clients_to_mirrors() == [
            frame.attached_clients()[0]["client"]
        ]
        clients.append(_attach_client(frame.socket, frame.target, cols=160, rows=48))

        rows = _clients_numbering(frame, 2)
        assert rows, (
            "a second attach left only one client: mirroring has regressed to the "
            "`-d` eviction it replaced"
        )

        # Converged, not instantaneous. The elections run from `run-shell -b`
        # hooks, and between `open` demoting the incumbent and the new client
        # attaching there is a moment when every attached client is a mirror --
        # where the election correctly promotes the survivor, as it must after a
        # detach. The new client's own election then settles it.
        settled = _wait_for(
            lambda: (
                [c for c in frame.attached_clients() if c["controller"]]
                if sum(1 for c in frame.attached_clients() if c["controller"]) == 1
                else None
            ),
            timeout=20.0,
        )
        assert settled, (
            f"never settled on exactly one controller: {frame.attached_clients()!r}"
        )
        assert settled[0]["size"] == "160x48", (
            f"the newest screen must control the frame: {frame.attached_clients()!r}"
        )
        assert len(frame.attached_clients()) == 2, "a client was dropped while settling"
    finally:
        _close(clients)
        frame.tmux(["kill-server"])


def test_election_promotes_the_survivor_when_the_controller_leaves(tmp_path):
    """Otherwise the last screen standing is stuck at the departed one's size."""
    frame, record = _frame(tmp_path)
    clients: list[tuple[subprocess.Popen, int]] = []
    try:
        frame.open(no_attach=True)
        first = _attach_client(frame.socket, frame.target, cols=100, rows=30)
        clients.append(first)
        assert _clients_numbering(frame, 1), "the first client never attached"
        frame.demote_clients_to_mirrors()
        second = _attach_client(frame.socket, frame.target, cols=160, rows=48)
        clients.append(second)
        assert _clients_numbering(frame, 2), "the second client never attached"
        assert frame.elect_controller() is not None

        mirror = [c for c in frame.attached_clients() if not c["controller"]]
        assert len(mirror) == 1, "setup did not produce exactly one mirror"

        second[0].kill()
        second[0].wait(timeout=10)
        assert _clients_numbering(frame, 1), "the controller never detached"

        # What the `client-detached` hook runs.
        promoted = frame.elect_controller()
        survivors = frame.attached_clients()
        assert len(survivors) == 1
        assert survivors[0]["controller"] is True, (
            "the surviving screen was left as a mirror, so nothing sizes the frame"
        )
        assert promoted == survivors[0]["client"]
    finally:
        _close(clients)
        frame.tmux(["kill-server"])


def test_a_mirror_resizing_itself_does_not_move_the_frame(tmp_path):
    """The freeze cause, pinned.

    A mirror changing its own terminal size must not reflow the frame. Before
    this, `window-size latest` handed the window to whichever client moved most
    recently, so the stale screen won simply by existing.
    """
    frame, record = _frame(tmp_path)
    clients: list[tuple[subprocess.Popen, int]] = []
    try:
        frame.open(no_attach=True)
        mirror_proc, mirror_fd = _attach_client(
            frame.socket, frame.target, cols=100, rows=30
        )
        clients.append((mirror_proc, mirror_fd))
        assert _clients_numbering(frame, 1), "the mirror never attached"
        frame.demote_clients_to_mirrors()
        clients.append(_attach_client(frame.socket, frame.target, cols=160, rows=48))
        assert _clients_numbering(frame, 2), "the controller never attached"
        frame.elect_controller()

        window = frame.windows()[record["id"]]
        # The controller's WIDTH, not its full size: tmux spends a row on the
        # status line, and relayout may snap the height. Width is the axis the
        # pane reflow is visible on anyway.
        settled = _wait_for(
            lambda: _window_size(frame, window).startswith("160x") or None,
            timeout=20.0,
        )
        assert settled, (
            f"the frame never took the controller's width, got "
            f"{_window_size(frame, window)!r}; clients="
            f"{frame.attached_clients()!r}"
        )
        before = _window_size(frame, window)

        # Resize the MIRROR to something larger than the controller.
        _set_size(mirror_fd, 200, 60)
        os.kill(mirror_proc.pid, signal.SIGWINCH)

        # The resize must genuinely land, or this test observes nothing.
        resized = _wait_for(
            lambda: any(
                c["size"] == "200x60" for c in frame.attached_clients()
            )
            or None,
            timeout=15.0,
        )
        assert resized, (
            "the mirror never reported its new size, so this test did not "
            "exercise the geometry vote it exists to check"
        )

        # Give any hook that was going to fire the chance to fire, so this is not
        # merely a race won by reading too early.
        time.sleep(2.0)
        assert _window_size(frame, window) == before, (
            f"a mirror moved the frame from {before} to "
            f"{_window_size(frame, window)}: either `ignore-size` is not in "
            "effect or relayout re-sized to the mirror, and the pane-reflow "
            "freeze is back"
        )
    finally:
        _close(clients)
        frame.tmux(["kill-server"])


def test_the_frame_still_sizes_to_the_latest_client(tmp_path):
    """Mirroring's premise: tmux is still asked to follow the active client.

    If this option ever changes, `ignore-size` stops being the thing that
    decides geometry and the tests above would pass for the wrong reason.
    """
    frame, _record = _frame(tmp_path)
    try:
        frame.open(no_attach=True)
        result = frame.tmux(["show-options", "-g", "window-size"])
        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["window-size", "latest"]
    finally:
        frame.tmux(["kill-server"])


def test_activity_ties_so_creation_order_decides(tmp_path):
    """`client_activity` is whole seconds: a second screen TIES with the first.

    Ranking by activity therefore could not express "the newest screen" at all,
    and picked an arbitrary winner -- in practice the older, smaller client,
    pulling the frame back to its geometry. Creation order is what decides.
    """
    frame, _record = _frame(tmp_path)
    clients: list[tuple[subprocess.Popen, int]] = []
    try:
        frame.open(no_attach=True)
        clients.append(_attach_client(frame.socket, frame.target, cols=100, rows=30))
        assert _clients_numbering(frame, 1), "the first client never attached"
        frame.demote_clients_to_mirrors()
        clients.append(_attach_client(frame.socket, frame.target, cols=160, rows=48))
        rows = _clients_numbering(frame, 2)
        assert rows, "the second client never attached"

        if len({c["last_active_epoch"] for c in rows}) == 1:
            # The normal case, and the one that used to break. Not asserted
            # outright because a slow run can straddle a second boundary.
            assert rows[0]["created_epoch"] is not None, (
                "no creation time to fall back on, so the election has nothing "
                "left to order clients by"
            )

        controller = frame.elect_controller()
        after = frame.attached_clients()
        controllers = [c for c in after if c["controller"]]
        assert len(controllers) == 1, f"expected one controller, got {after}"
        assert controllers[0]["size"] == "160x48", (
            f"the older screen took control: {after!r}"
        )
        assert controller == controllers[0]["client"]
    finally:
        _close(clients)
        frame.tmux(["kill-server"])


def test_the_newest_screen_ends_up_in_control_without_any_demotion(tmp_path):
    """A plain `tmux attach` -- nobody called `open`, so nothing was demoted.

    Both clients arrive unflagged, so both are claimants and creation order is
    the only thing separating them. The newest screen must end up driving: that
    is the behaviour `window-size latest` always promised.
    """
    frame, _record = _frame(tmp_path)
    clients: list[tuple[subprocess.Popen, int]] = []
    try:
        frame.open(no_attach=True)
        clients.append(_attach_client(frame.socket, frame.target, cols=100, rows=30))
        assert _clients_numbering(frame, 1), "the first client never attached"
        clients.append(_attach_client(frame.socket, frame.target, cols=160, rows=48))
        assert _clients_numbering(frame, 2), "the second client never attached"

        settled = _wait_for(
            lambda: (
                [c for c in frame.attached_clients() if c["controller"]]
                if sum(1 for c in frame.attached_clients() if c["controller"]) == 1
                else None
            ),
            timeout=20.0,
        )
        assert settled, (
            f"the frame never settled on exactly one controller: "
            f"{frame.attached_clients()!r}"
        )
        assert settled[0]["size"] == "160x48", (
            f"the older screen ended up in control: {frame.attached_clients()!r}"
        )
    finally:
        _close(clients)
        frame.tmux(["kill-server"])


def test_repeated_elections_cannot_move_control(tmp_path):
    """Concurrent hooks must agree.

    `client-attached` and `client-detached` both run `run-shell -b`, so several
    elections can overlap. Each decides from the client set alone, so running it
    again -- as a late hook does -- must change nothing. An earlier version
    honoured "the client that just attached" and a late hook re-promoted a
    demoted mirror, making the winner whichever hook finished last: flaky 2 runs
    in 3.
    """
    frame, _record = _frame(tmp_path)
    clients: list[tuple[subprocess.Popen, int]] = []
    try:
        frame.open(no_attach=True)
        clients.append(_attach_client(frame.socket, frame.target, cols=100, rows=30))
        assert _clients_numbering(frame, 1), "the first client never attached"
        frame.demote_clients_to_mirrors()
        clients.append(_attach_client(frame.socket, frame.target, cols=160, rows=48))
        assert _clients_numbering(frame, 2), "the second client never attached"

        first = frame.elect_controller()
        for _ in range(5):
            assert frame.elect_controller() == first, (
                "the election is not idempotent, so overlapping hooks will "
                "fight over which screen controls the frame"
            )
        after = frame.attached_clients()
        assert sum(1 for c in after if c["controller"]) == 1
        assert next(c["size"] for c in after if c["controller"]) == "160x48"
    finally:
        _close(clients)
        frame.tmux(["kill-server"])
