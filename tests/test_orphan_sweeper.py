"""Reaping the processes a dead frame leaves behind.

`SocketReaper` only ever removed socket FILES, so the expensive leftovers
survived every sweep. Measured on one machine in 2026-11: a tmux server up 65
hours whose socket file had been deleted, still owning 30 sidebar keep-alive
shells, each forking a `tmux display-message` and a guard subshell twice a second
to ask a server it could not reach whether its window was visible.

Every row here is injected and every kill is captured, so this suite never
signals a real process.
"""
from __future__ import annotations

import os
import signal

import pytest

from hive_ide.socket_gc import OrphanSweeper, SocketReaper


SOCKET = "hive-ide-abc12345"
OTHER = "hive-ide-def67890"

# The real shapes matter: each of these argvs broke an earlier version.
SHELL_ARGV = (
    'sh -c unset NO_COLOR; while :; do if out=$(tmux display-message -p -t '
    '"$TMUX_PANE" "#{{window_active}}" 2>/dev/null & pid=$!; (sleep 0.25; kill '
    '"$pid") & guard=$!; wait "$pid"; exit "$rc") && [ "$out" = 1 ]; then '
    "/usr/bin/python3 -m hive_ide.sidebar --state-home /s --workspace-key /w "
    "--session-id sid --tmux-socket {socket}; sleep 1; else sleep 1; fi; done"
)
SERVER_ARGV = (
    "tmux -L {socket} -f /state/ide.conf new-session -d -s {socket} -n NAME -c /w "
    + SHELL_ARGV
)
CLIENT_ARGV = "tmux -L {socket} attach -t {socket}"


def shell(pid: int, age: int, socket: str = SOCKET) -> tuple[int, int, str]:
    return (pid, age, SHELL_ARGV.format(socket=socket))


def server(pid: int, age: int, socket: str = SOCKET) -> tuple[int, int, str]:
    return (pid, age, SERVER_ARGV.format(socket=socket))


def client(pid: int, age: int, socket: str = SOCKET) -> tuple[int, int, str]:
    return (pid, age, CLIENT_ARGV.format(socket=socket))


class _Kills:
    """Records signals instead of sending them, and reports pids as gone."""

    def __init__(self, survive: set[int] | None = None):
        self.sent: list[tuple[int, int]] = []
        self.survive = survive or set()

    def __call__(self, pid: int, sig: int) -> None:
        if sig == 0:
            # Liveness probe: only a pid in `survive` is still around.
            if pid not in self.survive:
                raise ProcessLookupError(pid)
            return
        self.sent.append((pid, sig))


def _sweeper(rows, *, survive=None, argv_of=None, **kw):
    kills = _Kills(survive)
    # By default a survivor still carries the argv it was matched on, so the
    # identity re-check passes and SIGKILL escalation is exercised. A test that
    # cares about pid REUSE overrides `argv_of`.
    table = {pid: argv for pid, _age, argv in rows}
    sweeper = OrphanSweeper(
        rows=list(rows), kill=kills,
        argv_of=argv_of or (lambda pid: table.get(pid)), **kw)
    sweeper.TERM_GRACE_SECONDS = 0.0
    return sweeper, kills


def sweep(rows, *, apply=True, survive=None, argv_of=None, **kw):
    """The AUTOMATIC path: shells only, no reachability argument."""
    sweeper, kills = _sweeper(rows, survive=survive, argv_of=argv_of, **kw)
    return sweeper.sweep(apply=apply), kills


def reap(rows, *, unreachable=(), apply=True, survive=None, argv_of=None, **kw):
    """The HUMAN-DRIVEN path: servers, and only names positively confirmed dead."""
    sweeper, kills = _sweeper(rows, survive=survive, argv_of=argv_of, **kw)
    result = sweeper.reap_servers(unreachable=set(unreachable), apply=apply)
    return result, kills


# -- the population that is unambiguously dead ---------------------------------


def test_a_shell_whose_server_is_gone_is_reaped():
    """No server process, no socket: it can never paint anything again."""
    result, kills = sweep([shell(101, 7200), shell(102, 7200)])

    assert sorted(result["shells"]) == [101, 102]
    assert result["servers"] == []
    assert [pid for pid, sig in kills.sent if sig == signal.SIGTERM] == [101, 102]


def test_a_live_frames_shells_are_never_touched():
    """A live frame is protected by its SERVER PROCESS being in the same snapshot.

    No reachability argument is involved any more, which is the point: the shell
    and the evidence that spares it come from one `ps` read, so there is no probe
    to be inconclusive and no socket directory to be the wrong one.
    """
    result, kills = sweep([shell(101, 7200), server(99, 7200)])

    assert result["shells"] == []
    assert result["servers"] == []
    assert kills.sent == []


def test_the_socket_name_is_parsed_without_shell_punctuation():
    """Directly: the argv ends `--tmux-socket <name>; sleep 1; ...`."""
    servers, _clients, shells = OrphanSweeper.classify([shell(101, 10)])

    assert list(shells) == [SOCKET], "the socket name carried shell punctuation"
    assert servers == {}


# -- the server, judged slowly --------------------------------------------------


def test_the_automatic_sweep_never_kills_a_server():
    """The whole point of the split. A server owns live panes, an already-attached
    client can still be watching one through an open descriptor, and
    "unreachable" is only ever true of the namespace we probed — so `open` must
    never take one down, however old and however unreachable it looks."""
    result, kills = sweep([server(99, 86400 * 9), shell(101, 86400 * 9)])

    assert result["servers"] == []
    assert kills.sent == [], "the automatic path signalled a server"


def test_the_explicit_reap_kills_an_unreachable_server_with_no_client():
    """The human-driven path still works — that is what `reap --apply` is for."""
    result, kills = reap([server(99, 86400 + 1)], unreachable=[SOCKET])

    assert result["servers"] == [99]
    assert (99, signal.SIGTERM) in kills.sent


def test_a_server_not_confirmed_unreachable_is_never_reaped():
    """UNKNOWN PROTECTS. `unreachable` holds only names a probe positively
    confirmed dead, so a timeout, a permission error or a directory we never
    looked in leaves the name absent — and absent means keep.

    The earlier shape asked for `reachable` and killed anything missing from it,
    which made every inconclusive probe authorise a kill.
    """
    rows = [server(99, 86400 * 9)]

    result, kills = reap(rows, unreachable=set())

    assert result["servers"] == [], "an unconfirmed server was killed"
    assert kills.sent == []


def test_an_unreachable_server_is_kept_while_a_client_holds_it():
    """A client that attached before the socket file went keeps its open fd.

    So an unreachable server can still be on someone's screen. Its shells must
    survive with it, or the session loses its sidebars.
    """
    rows = [server(99, 86400 * 9), client(500, 86400 * 9), shell(101, 86400 * 9)]

    result, kills = reap(rows, unreachable=[SOCKET])

    assert result["servers"] == [], "killed a server a client is still attached to"
    assert kills.sent == []


def test_a_client_attached_by_socket_PATH_still_protects_its_server():
    """`tmux -S <path> attach` carries no `-L`, so a client matched on `-L` alone
    is invisible and its server loses the one protection that keeps a live screen
    alive. The basename of the path is the socket name."""
    rows = [server(99, 86400 * 9), (500, 86400 * 9, f"tmux -S /tmp/tmux-1000/{SOCKET} attach")]

    result, kills = reap(rows, unreachable=[SOCKET])

    assert result["servers"] == [], "a -S client did not protect its server"
    assert kills.sent == []


def test_an_unreachable_server_younger_than_a_day_is_kept():
    result, _kills = reap([server(99, 3600)], unreachable=[SOCKET])

    assert result["servers"] == []


def test_a_freshly_started_shell_is_kept():
    """A frame's shells exist before its server finishes binding."""
    result, _kills = sweep([shell(101, 5)])

    assert result["shells"] == []


# -- classification traps -------------------------------------------------------


def test_a_server_is_not_mistaken_for_its_own_keep_alive_shell():
    """A tmux server keeps the full argv it was started with — which for a frame
    contains the pane command, `sh -c '... hive_ide.sidebar ...'`. Matching the
    sidebar string first classified the server as one of its own shells, and it
    then hid among them and reported "0 orphan servers"."""
    servers, _clients, shells = OrphanSweeper.classify([server(99, 10)])

    assert list(servers) == [SOCKET], "the server was not recognised as a server"
    assert shells == {}, "the server was counted as a keep-alive shell"


def test_a_client_is_not_mistaken_for_a_server():
    servers, clients, _shells = OrphanSweeper.classify([client(500, 10)])

    assert list(clients) == [SOCKET]
    assert servers == {}


def test_another_tools_socket_is_never_ours():
    """The allowlist is the point: a plain `default` socket is somebody else's."""
    rows = [(77, 99999, "tmux -L default -f /x new-session -d")]

    result, kills = sweep(rows)

    assert result["servers"] == [] and result["shells"] == []
    assert kills.sent == []
    assert SocketReaper.ours("default") is False
    # `ide-test-*` is the HIVE side's name, not this package's. Sweeping it would
    # mean a shipped package deleting another codebase's sockets.
    assert SocketReaper.ours("ide-test-9") is False
    assert SocketReaper.ours("hive-ide-next-abc") is True


def test_each_socket_is_judged_on_its_own_evidence():
    """A dead frame next to a live one must not take the live one with it."""
    rows = [shell(101, 7200), shell(201, 7200, OTHER), server(99, 7200, OTHER)]

    result, _kills = sweep(rows)

    assert result["shells"] == [101]
    assert 201 not in result["shells"]


# -- mechanics ------------------------------------------------------------------


def test_a_dry_run_signals_nothing():
    result, kills = sweep([shell(101, 7200)], apply=False)

    assert result["shells"] == [101], "a dry run must still report what it would do"
    assert result["killed"] == []
    assert kills.sent == []


def test_a_survivor_is_escalated_to_sigkill():
    result, kills = sweep([shell(101, 7200), shell(102, 7200)], survive={101})

    assert (101, signal.SIGTERM) in kills.sent
    assert (101, signal.SIGKILL) in kills.sent, "a shell that ignored SIGTERM survived"
    assert (102, signal.SIGKILL) not in kills.sent, "SIGKILL sent to a pid already gone"
    assert sorted(result["killed"]) == [101, 102]


def test_a_vanished_pid_does_not_abort_the_sweep():
    """Every kill is independent: a process that exits between listing and
    signalling is normal, not a failure."""

    class _Gone(_Kills):
        def __call__(self, pid: int, sig: int) -> None:
            if pid == 101 and sig == signal.SIGTERM:
                raise ProcessLookupError(pid)
            super().__call__(pid, sig)

    rows = [shell(101, 7200), shell(102, 7200)]
    kills = _Gone()
    table = {pid: argv for pid, _a, argv in rows}
    sweeper = OrphanSweeper(rows=rows, kill=kills, argv_of=lambda pid: table.get(pid))
    sweeper.TERM_GRACE_SECONDS = 0.0

    result = sweeper.sweep(apply=True)

    assert result["killed"] == [102]
    assert (102, signal.SIGTERM) in kills.sent


def test_the_kill_count_is_bounded():
    rows = [shell(1000 + i, 7200) for i in range(50)]
    sweeper, kills = _sweeper(rows)
    sweeper.MAX_KILLS = 10

    sweeper.sweep(apply=True)

    assert len([s for s in kills.sent if s[1] == signal.SIGTERM]) == 10


def test_etime_parsing():
    assert OrphanSweeper.etime_seconds("00:05") == 5
    assert OrphanSweeper.etime_seconds("01:30") == 90
    assert OrphanSweeper.etime_seconds("02:45:21") == 9921
    assert OrphanSweeper.etime_seconds("2-17:25:17") == 235517  # 2d + 17h25m17s
    assert OrphanSweeper.etime_seconds("nonsense") == 0


def test_the_sweeper_never_signals_itself():
    rows = [(os.getpid(), 99999, SHELL_ARGV.format(socket=SOCKET))]

    result, kills = sweep(rows)

    assert result["shells"] == [], "the sweeper put its own pid on the kill list"
    assert kills.sent == []


def test_a_reused_pid_is_not_escalated_to_sigkill():
    """`kill(pid, 0)` proves only that the NUMBER is live.

    A process that survives SIGTERM, exits during the grace window, and has its
    pid handed to something new would be SIGKILLed on liveness alone. Identity is
    the argv we matched; here the pid comes back as something else entirely.
    """
    result, kills = sweep(
        [shell(101, 7200)],
        survive={101},
        argv_of=lambda pid: "/usr/bin/some-innocent-stranger --unrelated",
    )

    assert (101, signal.SIGTERM) in kills.sent
    assert (101, signal.SIGKILL) not in kills.sent, (
        "SIGKILLed a pid that no longer carries the argv it was matched on"
    )


def test_an_unreadable_argv_is_not_escalated_either():
    """The unknown arm of the same check: if the argv cannot be read, we do not
    know whether this is still our process, and unknown does not authorise a
    SIGKILL."""
    result, kills = sweep([shell(101, 7200)], survive={101}, argv_of=lambda pid: None)

    assert (101, signal.SIGKILL) not in kills.sent
