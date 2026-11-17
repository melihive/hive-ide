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
import pathlib
import shutil
import subprocess
import tempfile
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


def _argv_from(rows, pid):
    """argv for a fabricated row — the stand-in for the `/proc` reader."""
    for candidate, _ppid, command in rows:
        if candidate == pid:
            return command.split() or None
    return None


def _stranded(rows, *, live_socket=LIVE_SOCKET,
              has_clients=lambda _socket, _server: False, argv_of=None):
    """`stranded` over fabricated rows, reading argv from those same rows.

    The production reader is `/proc`, which fabricated PIDs do not have. Splitting
    the row's command is the honest stand-in for a shape with no quoted argument
    in it; the tests that care about ARGUMENT BOUNDARIES pass real argv lists.
    """
    commands = {pid: command for pid, _ppid, command in rows}
    default = lambda pid: (commands.get(pid) or "").split() or None
    return ConversationHolders.stranded(
        REFERENCE, live_socket=live_socket, has_clients=has_clients,
        argv_of=argv_of or default, rows=rows,
    )


# ---------------------------------------------------------------- classification


def test_the_server_is_found_by_walking_parents_not_by_ppid():
    """The driver's parent is its wrapper; only the walk reaches the server.

    This is why `orphaned` (ppid == 1) misses the whole shape: no process in the
    chain has ppid 1 until the server itself.
    """
    rows = _rows()
    assert ConversationHolders.server_of(201, rows) == (RETIRED_SOCKET, 100)
    assert ConversationHolders.server_of(200, rows) == (RETIRED_SOCKET, 100)
    assert all(ppid != 1 for pid, ppid, _ in rows if pid != 100)


def test_a_driver_in_a_retired_clientless_server_is_stranded():
    found = _stranded(_rows())
    assert [(h["pid"], h["socket"]) for h in found] == [(201, RETIRED_SOCKET)]


def test_a_server_someone_is_attached_to_is_never_touched():
    """A client means a human can see it. That is use, not abandonment."""
    assert _stranded(_rows(), has_clients=lambda _socket, _server: True) == []


def test_an_unanswerable_client_question_protects_the_process():
    """None is not False. An unreadable answer must never authorize a signal."""
    assert _stranded(_rows(), has_clients=lambda _socket, _server: None) == []


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

#: The release path reads a server's environment from `/proc` (a stated limit —
#: see `_socket_root_of`), so on a platform without it the feature is inert by
#: design. These tests therefore SKIP there rather than fail: a documented gap
#: must not read as a broken build on the platform it was documented for.
pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or not pathlib.Path("/proc/self/environ").exists(),
    reason="needs tmux and a /proc that carries process environments",
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
    monkeypatch.setattr(repair, "_socket_has_clients", lambda _s, _p: True)
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
    monkeypatch.setattr(repair, "_socket_has_clients", lambda _s, _p: None)
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
    server_pid = int(subprocess.run(
        ["tmux", "-L", socket, "display-message", "-p", "#{pid}"],
        check=True, capture_output=True, text=True,
    ).stdout.strip())
    try:
        assert repair._socket_has_clients(socket, server_pid) is False, (
            "a live server with nobody attached must answer False"
        )
        assert repair._socket_has_clients(socket, server_pid + 10_000_000) is None, (
            "a server that is not the one we walked to must answer None"
        )
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)
    assert repair._socket_has_clients(socket, server_pid) is None, (
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


# ------------------------------------- what a `-L` name is, and is not (review)


@pytest.mark.parametrize(
    "argv, expected",
    [
        (f"tmux -L {RETIRED_SOCKET} new-session -d", RETIRED_SOCKET),
        (f"tmux -L{RETIRED_SOCKET} new-session -d", RETIRED_SOCKET),
        # tmux takes the LAST of a repeated option. A search returns the first,
        # which names a different server than the one actually running.
        (f"tmux -L {RETIRED_SOCKET} -L hive-ide-other new-session", "hive-ide-other"),
        # A name that only PARTLY matches is not this name: `hive-ide-old` is a
        # real and different socket from `hive-ide-old:active`.
        (f"tmux -L {RETIRED_SOCKET}:active new-session", None),
        # `-S` addresses a socket by path, so the name says nothing about where
        # the server is — including when both are given.
        ("tmux -S /elsewhere/active new-session", None),
        (f"tmux -L {RETIRED_SOCKET} -S /elsewhere/active new-session", None),
        ("tmux -L", None),
    ],
)
def test_the_socket_name_is_parsed_as_an_option_not_searched_for(argv, expected):
    assert ConversationHolders._socket_from_tmux_argv(argv) == expected


@pytest.mark.parametrize(
    "argv",
    [
        f"tmux -L {RETIRED_SOCKET} -L hive-ide-other new-session",
        f"tmux -L {RETIRED_SOCKET}:active new-session",
        f"tmux -L {RETIRED_SOCKET} -S /elsewhere/active new-session",
    ],
)
def test_a_misread_socket_name_cannot_strand_the_wrong_server(argv):
    """The parse decides what gets signalled, so a wrong read must not reach it."""
    found = _stranded(_rows(server_argv=argv))
    assert [h.get("socket") for h in found] != [RETIRED_SOCKET]


@pytestmark_tmux
def test_a_server_in_another_socket_directory_is_still_reached(tmp_path, monkeypatch):
    """The reported case: the retired frame's socket is NOT where the live one's is.

    A frame from before the pinned socket directory keeps its socket in the
    default location while the live frame's lives elsewhere. Probing in the live
    frame's directory finds nothing there, answers "unknown", and the stranded
    session stays stuck forever — the feature failing on the exact shape it was
    built for (cross-model review, 2026-11-17).
    """
    # A unix socket path is capped near 108 bytes, so this root and this name
    # both stay short — a pytest tmp_path plus a descriptive name overruns it.
    elsewhere = pathlib.Path(tempfile.mkdtemp(prefix="hv"))
    driver = _fake_driver(tmp_path)
    socket = f"hive-ide-e{uuid.uuid4().hex[:6]}"
    env = {**os.environ, "TMUX_TMPDIR": str(elsewhere)}
    started = subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", "stale",
         f"{driver} -c 'while :; do sleep 0.2; done' --resume {REFERENCE}"],
        capture_output=True, text=True, env=env,
    )
    assert started.returncode == 0, f"tmux refused: {started.stderr!r}"
    repair, record = _repair_for(tmp_path)
    assert repair.frame.socket_tmpdir != str(elsewhere)
    actions: list[str] = []
    try:
        repair._release_stranded_holder(record, actions, [], apply=True)
        assert any("stopped stranded driver" in line for line in actions), (
            f"a retired server in another socket directory was never reached: {actions}"
        )
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"],
                       capture_output=True, env=env)
        shutil.rmtree(elsewhere, ignore_errors=True)


@pytestmark_tmux
def test_a_client_that_attaches_while_we_probe_calls_the_signal_off(
    tmp_path, retired_server, monkeypatch
):
    """Selection and signal are different moments, and the gap is not small.

    Choosing a candidate costs two tmux round trips and a `ps`; a client can
    attach inside that window, turning an abandoned server into one a human is
    sitting at. The verdict is therefore re-asked as late as it can be.
    """
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    answers = iter([False, True])       # free when chosen, attached when signalled

    monkeypatch.setattr(
        repair, "_socket_has_clients", lambda _s, _p: next(answers, True)
    )
    actions: list[str] = []
    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "a client attached mid-probe and the driver was signalled anyway"
    assert actions == []


@pytestmark_tmux
def test_a_recycled_pid_is_not_the_process_we_chose(tmp_path, retired_server):
    """`kill(pid, 0)` proves a number is live, never that it is the same program."""
    _socket, pid = retired_server
    repair, _record = _repair_for(tmp_path)
    holder = {
        "pid": pid,
        "command": "something else entirely --resume other",
        "socket": _socket,
        "server_pid": 1,
    }

    assert repair._still_stranded(holder) is False, (
        "a PID whose argv no longer matches was accepted as the same process"
    )
    assert _alive(pid)


def test_a_backgrounded_sessions_driver_child_is_never_stranded(monkeypatch):
    """A backgrounded session is held ON PURPOSE — host and children alike.

    The host spawns the real driver as a child carrying no marker of its own, so
    exempting only the host leaves that child looking abandoned. For the launch
    guard that cost a refused launch; here it would cost a SIGTERM to a session
    the user chose to background (cross-model review, 2026-11-17).
    """
    rows = _rows()
    monkeypatch.setattr(
        ConversationHolders, "background_tree",
        classmethod(lambda _cls, _reference, rows=None: {201}),
    )
    assert _stranded(rows) == []


def test_the_live_server_is_excluded_by_identity_not_by_name():
    """Same `-L` name, different server: that holder is stranded, not ours.

    Two servers can share a socket NAME under different `TMUX_TMPDIR` roots.
    Excluding by name alone would hide a genuinely stranded holder behind the
    coincidence, and repair could never release the conversation.
    """
    rows = _rows()
    same_name_other_server = _stranded(
        rows, live_socket=RETIRED_SOCKET, has_clients=lambda _s, _p: False
    )
    assert same_name_other_server == [], "no identity given: the name must still protect"

    found = ConversationHolders.stranded(
        REFERENCE,
        live_socket=RETIRED_SOCKET,      # the live frame happens to share the name
        live_server_pid=999_999,         # but it is a different server
        has_clients=lambda _s, _p: False,
        argv_of=lambda pid: _argv_from(rows, pid),
        rows=rows,
    )
    assert [h["pid"] for h in found] == [201], (
        "a holder under a DIFFERENT server was hidden by a matching socket name"
    )

    mine = ConversationHolders.stranded(
        REFERENCE,
        live_socket="unrelated-name",
        live_server_pid=100,             # this IS the live frame's own server
        has_clients=lambda _s, _p: False,
        argv_of=lambda pid: _argv_from(rows, pid),
        rows=rows,
    )
    assert mine == [], "the live frame's own driver was treated as stranded"


def test_a_driver_that_merely_MENTIONS_the_conversation_is_not_stranded():
    """`--resume OTHER -p 'see TARGET'` resumes OTHER. Signalling it is wrong.

    `holders()` matches the looser way on purpose — it feeds the launch guard,
    where a false positive only refuses a launch. On the kill path the same
    false positive terminates somebody's live, unrelated agent, so this path
    demands the reference be the VALUE of the selection flag.
    """
    other = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
    rows = [
        (100, 1, f"tmux -L {RETIRED_SOCKET} new-session -d"),
        (201, 100, f"claude --resume {other} -p Explain transcript {REFERENCE}"),
    ]
    # The loose matcher — the one the guard uses — does consider it a holder.
    assert ConversationHolders.holders(REFERENCE, rows=rows), (
        "precondition: the guard still sees it, which is what makes this a trap"
    )
    assert _stranded(rows) == [], "a driver resuming ANOTHER conversation was selected"


@pytest.mark.parametrize(
    "argv, selected",
    [
        (["claude", "--resume", REFERENCE], True),
        (["claude", f"--resume={REFERENCE}"], True),
        (["claude", "--session-id", REFERENCE], True),
        # The bundled Codex driver puts the reference LAST, positionally.
        (["codex", "resume", "--no-daemon", "-C", "/w", REFERENCE], True),
        (["claude", "--resume", "other", "-p", f"see {REFERENCE}"], False),
        (["claude", "-p", REFERENCE], False),
        # `resume` is a SUBCOMMAND, never a flag whose next argument is a
        # conversation. Treating it as one let an argument that merely reads
        # `resume <id>` do the selecting.
        (["claude", "-p", "resume", REFERENCE], False),
        (["claude", "--resume", f"{REFERENCE}x"], False),
        # A PROMPT is one argument. Flattened by `ps` it reads as a flag and a
        # value; with real boundaries it can never select anything.
        (["claude", "--resume", "other", "-p",
          f"Explain how to resume {REFERENCE} safely"], False),
    ],
)
def test_the_reference_must_be_the_one_the_argv_actually_selects(argv, selected):
    assert ConversationHolders.selects_exactly(argv, REFERENCE) is selected


@pytestmark_tmux
def test_the_signal_is_withheld_when_the_argv_changed_at_the_last_moment(
    tmp_path, retired_server
):
    """The final re-read is the only thing standing between a gap and a stranger.

    Everything before it — two tmux probes at ten seconds apiece — happens while
    the PID could be reaped and reissued to an unrelated program. So the argv is
    read once more immediately before the signal, with a pidfd already pinning
    the identity, and a mismatch calls the whole thing off.
    """
    _socket, pid = retired_server

    repair, _record = _repair_for(tmp_path)
    sent = repair._stop_and_confirm(pid, "some other program --flag")

    assert sent is False, "a process whose argv no longer matched was signalled"
    assert _alive(pid), "the signal went out despite the identity having changed"


@pytestmark_tmux
def test_a_pane_already_running_a_driver_stops_the_walk(tmp_path, retired_server, monkeypatch):
    """A live agent means nothing is stranded — including a pane with no shell.

    `_has_live_shell_agent` answers only for a pane running a shell that spawned
    a driver. A pane whose root process IS the driver answered False there, so
    the first gate did not hold for that shape.
    """
    _socket, pid = retired_server
    repair, record = _repair_for(tmp_path)
    monkeypatch.setattr(
        repair.frame, "agent_pane_command", lambda _record: "claude --resume x"
    )
    actions: list[str] = []

    repair._release_stranded_holder(record, actions, [], apply=True)

    assert _alive(pid), "the walk ran although the pane was running a driver"
    assert actions == []


@pytestmark_tmux
def test_the_real_argv_is_read_with_its_boundaries_intact(retired_server):
    """`/proc` keeps arguments separate; `ps` flattens them into one line."""
    _socket, pid = retired_server

    argv = ConversationHolders.argv_of(pid)

    assert argv is not None, "the driver's argv could not be read"
    assert argv[0].endswith("/claude"), argv
    assert REFERENCE in argv, argv
    assert ConversationHolders.selects_exactly(argv, REFERENCE)
