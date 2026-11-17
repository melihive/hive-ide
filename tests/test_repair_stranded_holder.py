"""Repair must resolve a conversation stranded in a RETIRED frame — and only then.

The stalemate, observed in the field on 2026-11-17. A frame had been rebuilt
onto the current `hive-ide-next-<hash>` socket while the pre-2.0 server on
`hive-ide-<hash>` kept running, with the session's `claude` alive inside it for
31 days. No client was attached to that server, so nobody could see or type at
the driver, but it still owned the conversation. Every `hive-ide chat` therefore
hit the launch guard ("already held by pid N") and every `hive-ide repair`
rebuilt a frame whose agent could never start. Repairing harder never helps:
the holder has to be released.

`holders.orphaned` does not describe this shape — the driver's ppid is its own
`sh -c` wrapper, so it reads as perfectly parented. What proves it abandoned is
three facts together: another server, no client attached, a socket this package
hands out.

The refusals matter as much as the fix. Everything here is a process this
package did not start and the user never asked it to stop, so each gate is
pinned by its own test, and `unknown` protects rather than acts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

from hive_ide.frame import Frame
from hive_ide.holders import ConversationHolders
from hive_ide.repair import SessionRepair
from hive_ide.store import StateStore

from conftest import tmux_wait_budget


REFERENCE = "01352fc0-f747-4934-8771-3e3b357fb92b"
LIVE_SOCKET = "hive-ide-next-abc12345"
RETIRED_SOCKET = "hive-ide-abc12345"


def _rows(*, server_argv: str = f"tmux -L {RETIRED_SOCKET} -f /x/ide.conf new-session -d"):
    """The reported process shape: server -> `sh -c` wrapper -> driver."""
    return [
        (100, 1, server_argv),
        (200, 100, f"sh -c unset NO_COLOR; claude --resume {REFERENCE} --name SESSION"),
        (201, 200, f"claude --resume {REFERENCE} --name SESSION"),
    ]


def _stranded(rows, *, live_socket=LIVE_SOCKET, has_clients=lambda _socket: False):
    return ConversationHolders.stranded(
        REFERENCE, live_socket=live_socket, has_clients=has_clients, rows=rows
    )


# ---------------------------------------------------------------- classification


def test_the_server_is_found_by_walking_parents_not_by_ppid():
    """The driver's parent is its wrapper; only the walk reaches the server.

    This is why `orphaned` (ppid == 1) misses the whole shape: no process in the
    chain has ppid 1 until the server itself.
    """
    rows = _rows()
    assert ConversationHolders.server_socket_of(201, rows) == RETIRED_SOCKET
    assert ConversationHolders.server_socket_of(200, rows) == RETIRED_SOCKET
    assert all(ppid != 1 for pid, ppid, _ in rows if pid != 100)


def test_a_driver_in_a_retired_clientless_server_is_stranded():
    found = _stranded(_rows())
    assert [(h["pid"], h["socket"]) for h in found] == [(201, RETIRED_SOCKET)]


def test_a_server_someone_is_attached_to_is_never_touched():
    """A client means a human can see it. That is use, not abandonment."""
    assert _stranded(_rows(), has_clients=lambda _socket: True) == []


def test_an_unanswerable_client_question_protects_the_process():
    """None is not False. An unreadable answer must never authorize a signal."""
    assert _stranded(_rows(), has_clients=lambda _socket: None) == []


def test_the_live_frames_own_driver_is_never_stranded():
    """Same socket as the frame being repaired: that is the session's own agent."""
    assert _stranded(_rows(), live_socket=RETIRED_SOCKET) == []


def test_a_server_this_package_did_not_name_is_left_alone():
    """Someone else's tmux is not ours to reason about, let alone signal."""
    rows = _rows(server_argv="tmux -L work-stuff -f /x/conf new-session -d")
    assert _stranded(rows) == []


def test_a_driver_with_no_tmux_ancestor_at_all_is_left_alone():
    """No server in the chain is no evidence of a retired frame."""
    rows = [
        (300, 1, f"sh -c claude --resume {REFERENCE}"),
        (301, 300, f"claude --resume {REFERENCE} --name SESSION"),
    ]
    assert _stranded(rows) == []


# ------------------------------------------------------------------- the action

pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is unavailable"
)


def _fake_driver(tmp_path):
    """An executable whose argv[0] really IS `claude`.

    A `#!/bin/sh` script will not do: the kernel runs it as `/bin/sh <path>`, so
    argv[0] is the shell and `_is_driver` rightly refuses it — which is how this
    fixture first failed. Copying a shell to the name `claude` gives a process
    that looks to `ps` exactly as a real driver does.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    driver = bin_dir / "claude"
    shutil.copy("/bin/sh", driver)
    driver.chmod(0o755)
    return driver


def _repair_for(tmp_path, *, reference=REFERENCE):
    """A SessionRepair whose session wants `reference`, with NO window built.

    Not building the frame is deliberate: with no agent pane there is no live
    driver, which is exactly the state a stranded session is in, and it keeps
    this test about the release rather than about window construction.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="SESSION",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver={"id": "claude", "resume": {"reference": reference}},
    )
    frame = Frame(store, socket=f"hive-ide-live-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    return SessionRepair(store, frame), record


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


@pytest.fixture
def retired_server(tmp_path):
    """A real second tmux server, clientless, running a real fake driver."""
    socket = f"hive-ide-retired-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    driver = _fake_driver(tmp_path)
    # `-c <loop>` keeps it alive; the arguments after it land in argv exactly as
    # a real driver's do, which is all `ps` and the matcher ever look at.
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", "stale",
         f"{driver} -c 'while :; do sleep 0.2; done' --resume {REFERENCE} --name SESSION"],
        check=True, capture_output=True, text=True,
    )
    deadline = time.monotonic() + tmux_wait_budget(5.0)
    pid = None
    while time.monotonic() < deadline and pid is None:
        for candidate, _ppid, command in ConversationHolders.snapshot():
            # argv[0] must BE the driver. The tmux server's own argv carries the
            # whole pane command — driver path and reference included — so a
            # substring match picks up the server instead, which this fixture
            # did until the kill it asserted turned out to be the server exiting
            # as its last pane went away.
            if command.split()[:1] == [str(driver)] and REFERENCE in command:
                pid = candidate
                break
        if pid is None:
            time.sleep(0.05)
    assert pid, "the fake driver never appeared in the process table"
    try:
        yield socket, pid
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
        for _ in range(40):
            if not _alive(pid):
                break
            time.sleep(0.05)


@pytestmark_tmux
def test_repair_releases_a_stranded_driver_and_says_what_it_stopped(
    tmp_path, retired_server
):
    """End to end on real processes: the holder is gone and the report names it."""
    socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    actions: list[str] = []
    warnings: list[str] = []

    assert _alive(pid), "precondition: the stranded driver is running"
    repair._release_stranded_holder(record, actions, warnings, apply=True)

    assert not _alive(pid), "the stranded driver survived the repair"
    assert warnings == []
    assert any(f"pid {pid}" in line and socket in line for line in actions), actions
    assert any("conversation released" in line for line in actions), actions


@pytestmark_tmux
def test_a_dry_run_reports_the_stranded_driver_without_stopping_it(
    tmp_path, retired_server
):
    """`--dry-run` has to be safe on the one path that signals a process."""
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=False)

    assert _alive(pid), "a dry run stopped a process"
    assert any("would stop" in line for line in actions), actions


@pytestmark_tmux
def test_repair_leaves_the_driver_alone_when_a_client_is_attached(
    tmp_path, retired_server, monkeypatch
):
    """The gate that decides this is `_socket_has_clients`; hold it True."""
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    monkeypatch.setattr(repair, "_socket_has_clients", lambda _socket: True)
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "a server with a client attached was signalled anyway"
    assert actions == []


@pytestmark_tmux
def test_repair_leaves_the_driver_alone_when_the_question_cannot_be_answered(
    tmp_path, retired_server, monkeypatch
):
    """Unknown protects — the same rule the orphan sweeper already lives by."""
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    monkeypatch.setattr(repair, "_socket_has_clients", lambda _socket: None)
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "an unanswerable client question still got a signal"
    assert actions == []


@pytestmark_tmux
def test_a_session_whose_conversation_is_elsewhere_signals_nothing(
    tmp_path, retired_server
):
    """The holder must match THIS session's conversation, not merely exist."""
    _socket, pid = retired_server
    repair, record = _repair_for(
        tmp_path, reference="99999999-0000-4000-8000-000000000000"
    )
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "a driver on an unrelated conversation was stopped"
    assert actions == []
    # Positive control, so this cannot pass by finding nothing at all: the very
    # same process IS stranded when asked about the conversation it holds.
    assert ConversationHolders.stranded(
        REFERENCE,
        live_socket=repair.frame.socket,
        has_clients=repair._socket_has_clients,
    ), "the fixture's driver was not stranded even for its own conversation"


@pytestmark_tmux
def test_the_client_question_tells_no_clients_apart_from_no_server(tmp_path):
    """False and None are different answers, and only False authorizes acting."""
    repair, _record = _repair_for(tmp_path)
    socket = f"hive-ide-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", "s", "sh -c 'sleep 30'"],
        check=True, capture_output=True, text=True,
    )
    try:
        assert repair._socket_has_clients(socket) is False, (
            "a live server with nobody attached must answer False"
        )
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    assert repair._socket_has_clients(socket) is None, (
        "a socket with no server must answer None, not False"
    )


@pytestmark_tmux
def test_a_holder_is_still_found_when_the_pane_is_narrow(retired_server, monkeypatch):
    """`ps` truncates the command to $COLUMNS — and every frame pane exports it.

    This is not a test-harness quirk. The package runs its `ps` from inside tmux
    panes, which set COLUMNS, so on a narrow pane a driver's argv was cut long
    before its `--resume <uuid>`. The holder then became invisible, the launch
    guard failed OPEN, and a second driver started on a conversation that was
    already held — the permanently blank pane `holders.py` exists to prevent.
    Reproduced at 80 columns, which is narrower than a phone frame's agent pane.
    """
    _socket, pid = retired_server
    monkeypatch.setenv("COLUMNS", "80")

    rows = ConversationHolders.snapshot()

    assert any(row[0] == pid for row in rows), "the holder vanished from `ps`"
    assert any(pid == row[0] and REFERENCE in row[2] for row in rows), (
        "the holder's row was truncated before its conversation reference"
    )
