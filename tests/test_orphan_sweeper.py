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
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hive_ide.socket_gc import OrphanSweeper, SocketReaper


SOCKET = "hive-ide-abc12345"
OTHER = "hive-ide-def67890"

# The real shapes matter: each of these argvs broke an earlier version.
# Production's literal shape, probe text included — the classifier now requires
# the probe verbatim, so a simplified copy here would not be the thing under test.
SHELL_ARGV = (
    'sh -c unset NO_COLOR; while :; do if out=$(tmux display-message -p -t '
    '"$TMUX_PANE" "#{{window_active}}" 2>/dev/null & pid=$!; '
    '(sleep 0.25; kill "$pid" 2>/dev/null) & guard=$!; '
    'wait "$pid" 2>/dev/null; rc=$?; '
    'kill "$guard" 2>/dev/null; wait "$guard" 2>/dev/null; '
    'exit "$rc") && [ "$out" = 1 ]; then '
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


def _sweeper(rows, *, survive=None, answers=None, **kw):
    kills = _Kills(survive)
    # `rows` is the ONLY identity source: the re-check before each signal re-reads
    # the process table, so a static list means "nothing changed" and SIGKILL
    # escalation is exercised. A test about pid REUSE passes a CALLABLE instead,
    # which is re-read on every access and can return a different table.
    # `answers` defaults to "tmux says there is no server there", so the rows stay
    # the thing under test; without it every candidate would shell out to the real
    # tmux and the suite would depend on this machine's sockets.
    # Injected pids have no `/proc` entry, so the real reader would answer "unknown"
    # for every one of them and protect the whole population — the server tests
    # would then pass while observing nothing. The default here says "its socket is
    # where we looked"; the tests about a DIFFERENT directory override it.
    kw.setdefault("socket_root", lambda pid: OrphanSweeper.our_socket_root())
    sweeper = OrphanSweeper(
        rows=rows if callable(rows) else list(rows), kill=kills,
        answers=answers if answers is not None else (lambda name: False), **kw)
    sweeper.TERM_GRACE_SECONDS = 0.0
    return sweeper, kills


def sweep(rows, *, apply=True, survive=None, answers=None, **kw):
    """The AUTOMATIC path: shells only, no reachability argument."""
    sweeper, kills = _sweeper(rows, survive=survive, answers=answers, **kw)
    return sweeper.sweep(apply=apply), kills


def reap(rows, *, unreachable=(), apply=True, survive=None, answers=None, **kw):
    """The HUMAN-DRIVEN path: servers, and only names positively confirmed dead."""
    sweeper, kills = _sweeper(rows, survive=survive, answers=answers, **kw)
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
    sweeper = OrphanSweeper(
        rows=rows, kill=kills, answers=lambda name: False,
        socket_root=lambda pid: OrphanSweeper.our_socket_root())
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
    rows = [shell(101, 7200)]
    stranger = [(101, 5, "/usr/bin/some-innocent-stranger --unrelated")]
    # select, then re-check before the SIGTERM — then the pid becomes someone else
    snapshots = iter([rows, rows])
    result, kills = sweep(
        lambda: next(snapshots, stranger),
        survive={101},
    )

    assert (101, signal.SIGTERM) in kills.sent
    assert (101, signal.SIGKILL) not in kills.sent, (
        "SIGKILLed a pid that no longer carries the argv it was matched on"
    )


def test_an_unreadable_process_table_is_not_escalated_either():
    """The unknown arm of the same check.

    `_ps` returns an EMPTY list when it cannot run at all, which is indistinguish-
    able from "every candidate exited". Either way we no longer know that this pid
    is our process, and unknown does not authorise a SIGKILL — even though
    `kill(pid, 0)` says the number is very much alive.
    """
    rows = [shell(101, 7200)]
    snapshots = iter([rows, rows])
    result, kills = sweep(lambda: next(snapshots, []), survive={101})

    assert (101, signal.SIGTERM) in kills.sent, "the candidate was selected"
    assert (101, signal.SIGKILL) not in kills.sent


# -- the third review round -----------------------------------------------------


def test_an_attached_new_session_client_is_not_mistaken_for_a_server():
    """`tmux -L X new-session -A -s work` ATTACHES; it just never says "attach".

    Classified as a server it became a reap candidate, so `reap --apply` could
    kill a live client and the panes the user was looking at. Only a DETACHED
    `new-session -d` is a server; everything unrecognised falls to the protective
    side.
    """
    rows = [(99, 86400 * 9, f"tmux -L {SOCKET} new-session -A -s work")]

    servers, clients, _shells = OrphanSweeper.classify(rows)
    assert servers == {}, "an attaching client was classified as a server"
    assert list(clients) == [SOCKET]

    result, kills = reap(rows, unreachable=[SOCKET])
    assert result["servers"] == []
    assert kills.sent == []


def test_a_detached_new_session_is_still_recognised_as_a_server():
    """The other direction — or the fix would spare every orphan there is."""
    servers, clients, _shells = OrphanSweeper.classify([server(99, 7200)])

    assert list(servers) == [SOCKET]
    assert clients == {}


def test_permission_denied_is_unknown_not_proof_of_no_server(monkeypatch):
    """A socket we are not ALLOWED to reach is not a socket with nothing behind it.

    `error connecting … (Permission denied)` was read as a positive "no server",
    so an EACCES authorised killing a live server. Only tmux's own "no server
    running" is proof; everything else is unknown, and unknown protects.
    """
    from hive_ide import socket_gc

    class _Result:
        returncode = 1
        stdout = ""
        stderr = "error connecting to /tmp/tmux-1000/x (Permission denied)"

    monkeypatch.setattr(socket_gc.subprocess, "run", lambda *a, **k: _Result())
    assert OrphanSweeper.server_answers(SOCKET) is None, (
        "a permission error was taken as proof that no server is there"
    )

    class _NoServer(_Result):
        stderr = "no server running on /tmp/tmux-1000/x"

    monkeypatch.setattr(socket_gc.subprocess, "run", lambda *a, **k: _NoServer())
    assert OrphanSweeper.server_answers(SOCKET) is False

    # The ORPHAN case, and the reason the `error connecting` branch cannot simply
    # be dismissed as unknown: a deleted socket reports ENOENT in exactly the same
    # shape as the permission error above. Verified against a real orphan.
    class _Deleted(_Result):
        stderr = "error connecting to /tmp/tmux-1000/x (No such file or directory)"

    monkeypatch.setattr(socket_gc.subprocess, "run", lambda *a, **k: _Deleted())
    assert OrphanSweeper.server_answers(SOCKET) is False, (
        "a deleted socket must be positively unreachable, or no orphan is ever found"
    )

    class _Refused(_Result):
        stderr = "error connecting to /tmp/tmux-1000/x (Connection refused)"

    monkeypatch.setattr(socket_gc.subprocess, "run", lambda *a, **k: _Refused())
    assert OrphanSweeper.server_answers(SOCKET) is False

    class _Weird(_Result):
        stderr = "error connecting to /tmp/tmux-1000/x (Too many open files)"

    monkeypatch.setattr(socket_gc.subprocess, "run", lambda *a, **k: _Weird())
    assert OrphanSweeper.server_answers(SOCKET) is None, (
        "an errno we do not recognise is unknown, not proof of absence"
    )

    class _Alive(_Result):
        returncode = 0
        stderr = ""

    monkeypatch.setattr(socket_gc.subprocess, "run", lambda *a, **k: _Alive())
    assert OrphanSweeper.server_answers(SOCKET) is True


def test_an_unknown_probe_never_reaches_the_confirmed_unreachable_set(monkeypatch):
    """The three states end up as two outcomes, and only one of them can kill."""
    answers = {"a": True, "b": False, "c": None}
    monkeypatch.setattr(OrphanSweeper, "server_answers",
                        staticmethod(lambda name: answers[name]))
    assert OrphanSweeper().confirmed_unreachable({"a", "b", "c"}) == {"b"}


def test_a_pid_reused_before_the_sigterm_is_never_signalled():
    """The window between SELECTING a candidate and signalling it.

    On the server path that window holds a tmux probe per socket, each with its
    own timeout, so it is seconds wide rather than microseconds. A callable row
    source lets the table change inside it: the pid is recycled before the
    SIGTERM, and nothing may be sent to it at all.
    """
    rows = [shell(101, 7200)]
    reused = [(101, 5, "/usr/bin/some-innocent-stranger --unrelated")]
    snapshots = iter([rows, reused, reused, reused])

    kills = _Kills()
    sweeper = OrphanSweeper(
        rows=lambda: next(snapshots, reused), kill=kills,
        answers=lambda name: False,
        socket_root=lambda pid: OrphanSweeper.our_socket_root())
    sweeper.TERM_GRACE_SECONDS = 0.0

    result = sweeper.sweep(apply=True)

    assert result["shells"] == [101], "the candidate should still be selected"
    assert kills.sent == [], (
        "signalled a pid that no longer carries the argv it was selected on"
    )
    assert result["killed"] == []


# -- the fourth review round ----------------------------------------------------


def test_a_server_started_with_a_compact_flag_is_still_seen():
    """tmux accepts `-Lname` with no space. A server the classifier cannot see has
    no presence to protect its sidebars, so the live frame's shells get reaped —
    which is why a missed match here is expensive rather than merely untidy."""
    rows = [
        (99, 7200, f"tmux -L{SOCKET} -f /state/ide.conf new-session -d -s x -n N -c /w"),
        shell(101, 7200),
    ]

    servers, _clients, shells = OrphanSweeper.classify(rows)
    assert list(servers) == [SOCKET], "a compact -Lname server was invisible"

    result, kills = sweep(rows)
    assert result["shells"] == [], "reaped a live frame's sidebars"
    assert kills.sent == []


def test_a_compact_socket_path_client_still_protects_its_server():
    """`-S/path` joined the same way."""
    rows = [server(99, 86400 * 9), (500, 86400 * 9, f"tmux -S/tmp/tmux-1000/{SOCKET} attach")]

    result, kills = reap(rows, unreachable=[SOCKET])

    assert result["servers"] == []
    assert kills.sent == []


def test_an_equals_joined_tmux_socket_flag_is_read():
    _servers, _clients, shells = OrphanSweeper.classify(
        [(101, 7200, SHELL_ARGV.format(socket=SOCKET).replace(
            f"--tmux-socket {SOCKET}", f"--tmux-socket={SOCKET}"))]
    )
    assert list(shells) == [SOCKET]


def test_a_flag_like_string_inside_a_path_is_not_the_flag():
    """The token anchor: `/opt/-Lsomething` must not read as `-L something`."""
    rows = [(99, 7200, f"/usr/bin/python3 /opt/-L{SOCKET}/run.py")]

    servers, clients, shells = OrphanSweeper.classify(rows)

    assert servers == {} and clients == {} and shells == {}


def test_a_process_that_merely_MENTIONS_the_sidebar_is_never_reaped():
    """The self-match trap. An agent or a diagnostic whose command line contains
    `hive_ide.sidebar` and `--tmux-socket hive-ide-…` is not a keep-alive loop,
    and signalling it would kill the very investigation looking into this code.

    Both halves must agree: a shell executable AND the loop's own shape.
    """
    mentions = [
        (701, 7200, f"codex exec --task investigate hive_ide.sidebar --tmux-socket {SOCKET}"),
        (702, 7200, f"/usr/bin/grep -rn hive_ide.sidebar --tmux-socket {SOCKET}"),
        (703, 7200, f"python3 -m pytest -k hive_ide.sidebar --tmux-socket {SOCKET}"),
    ]

    _servers, _clients, shells = OrphanSweeper.classify(mentions)
    assert shells == {}, "a process that only names the sidebar was taken for one"

    result, kills = sweep(mentions)
    assert result["shells"] == []
    assert kills.sent == []


def test_a_shell_without_the_loops_shape_is_not_a_keep_alive():
    """A shell is necessary but not sufficient — `bash -c 'grep …'` is not the loop."""
    rows = [(704, 7200, f"bash -c grep -rn hive_ide.sidebar --tmux-socket {SOCKET}")]

    _servers, _clients, shells = OrphanSweeper.classify(rows)

    assert shells == {}, "a one-shot shell command was taken for the keep-alive loop"


def test_the_real_keep_alive_is_still_recognised():
    """The other direction, or the narrowing would spare every orphan there is."""
    _servers, _clients, shells = OrphanSweeper.classify([shell(101, 7200)])

    assert list(shells) == [SOCKET]


# -- the fifth review round -----------------------------------------------------


def test_the_keepalive_signature_still_matches_what_frame_builds():
    """The coupling guard. `socket_gc` cannot import `frame` (frame imports
    socket_gc), so the signature is a copy — and a copy drifts silently.

    If this goes red, the classifier has stopped recognising the real keep-alive,
    which does not kill anything wrongly but does mean no shell is ever reaped
    again. Re-sync the constant with the builder.
    """
    from hive_ide.frame import Frame

    probe = Frame._sidebar_visible_probe()
    assert OrphanSweeper._KEEPALIVE_SIGNATURE in probe, (
        "the keep-alive signature no longer appears in what frame.py builds"
    )


def test_the_test_fixture_matches_the_real_probe_too():
    """And the fixture is held to the same standard, so these tests cannot pass
    against a shape production never emits."""
    from hive_ide.frame import Frame

    assert Frame._sidebar_visible_probe() in SHELL_ARGV.format(socket=SOCKET), (
        "SHELL_ARGV has drifted from the probe frame.py actually emits"
    )


def test_a_grep_carrying_every_marker_is_still_not_a_keep_alive():
    """Three loose substrings were defeatable: a shell running `grep -F` with a
    pattern containing all of them was selected and signalled. The signature has
    to appear as the probe itself."""
    rows = [(705, 7200, "bash -c tail -F /var/log/x | grep -F 'while window_active "
                        f"hive_ide.sidebar --tmux-socket {SOCKET}'")]

    _servers, _clients, shells = OrphanSweeper.classify(rows)
    assert shells == {}, "a grep carrying the markers was taken for the keep-alive"

    result, kills = sweep(rows)
    assert result["shells"] == [] and kills.sent == []


def test_incidental_attach_text_does_not_hide_an_orphan_server():
    """A server whose working directory is `/work/attachments` read as a CLIENT,
    so it vanished from the inventory and no orphan was ever reported. `attach`
    counts only as a command token."""
    rows = [(99, 86400 * 9,
             f"tmux -L {SOCKET} -f /state/ide.conf new-session -d -s proj -c /work/attachments")]

    servers, clients, _shells = OrphanSweeper.classify(rows)

    assert list(servers) == [SOCKET], "incidental 'attach' text hid a real server"
    assert clients == {}
    assert OrphanSweeper(rows=rows).server_names() == {SOCKET}

    found = OrphanSweeper(
        rows=rows,
        socket_root=lambda pid: OrphanSweeper.our_socket_root(),
    ).orphan_servers(unreachable={SOCKET})
    assert [e["pid"] for e in found] == [99]


def test_a_real_attach_is_still_a_client():
    """The other direction."""
    _servers, clients, _shells = OrphanSweeper.classify([client(500, 7200)])

    assert list(clients) == [SOCKET]


def test_another_users_process_is_never_a_candidate(monkeypatch):
    """`ps -axww` lists every user. Unfiltered, a privileged run could reap
    somebody else's healthy server: their socket is in their own directory, so it
    answers nothing when we probe ours and looks abandoned."""
    from hive_ide import socket_gc

    mine, theirs = os.getuid(), os.getuid() + 1

    class _Result:
        returncode = 0
        stderr = ""
        stdout = (
            f"  101 {theirs}  02:00:00 {SHELL_ARGV.format(socket=SOCKET)}\n"
            f"  102 {mine}  02:00:00 {SHELL_ARGV.format(socket=OTHER)}\n"
        )

    monkeypatch.setattr(socket_gc.subprocess, "run", lambda *a, **k: _Result())
    rows = OrphanSweeper._ps()
    pids = [pid for pid, _age, _argv in rows]

    assert pids == [102], f"kept another user's process: {pids}"


# -- the sixth review round -----------------------------------------------------


def test_a_shell_is_kept_when_tmux_says_its_server_is_alive():
    """The grouped-flag and `$TMUX` cases, covered without parsing either.

    tmux accepts `-uLname`, and `$TMUX` names the socket with no flag at all, so a
    live server can be unparseable and vanish from the table. Asking tmux resolves
    the name the way tmux does, so a parsing gap can no longer invent an absence
    and kill a live frame's sidebars.
    """
    result, kills = sweep([shell(101, 7200)], answers=lambda name: True)

    assert result["shells"] == [], "reaped a shell whose server tmux says is alive"
    assert kills.sent == []


def test_a_shell_is_kept_when_the_probe_cannot_tell():
    """And the third state: unknown keeps it."""
    result, kills = sweep([shell(101, 7200)], answers=lambda name: None)

    assert result["shells"] == []
    assert kills.sent == []


def test_a_shell_needs_BOTH_negatives_the_table_and_tmux():
    """Positively: table says no server AND tmux says no server."""
    result, kills = sweep([shell(101, 7200)], answers=lambda name: False)

    assert result["shells"] == [101]
    assert (101, signal.SIGTERM) in kills.sent


def test_a_server_that_recovers_before_the_kill_is_spared():
    """A tmux server recreates its socket on SIGUSR1, and `unreachable` was
    collected before a probe-per-socket walk whose calls can take ten seconds
    each. The pid and argv are unchanged, so the identity check would pass it
    through — reachability has to be re-asked at the moment of signalling."""
    # `unreachable` is the STALE verdict that selected it; the probe is what the
    # world says now. The two deliberately disagree.
    result, kills = reap(
        [server(99, 86400 * 9)],
        unreachable=[SOCKET],
        answers=lambda name: True,
    )

    assert kills.sent == [], "signalled a server that had come back"
    assert result["killed"] == []
    # `servers` is the candidate list — what the reaper FOUND — and `killed` is
    # what it actually signalled. Both halves report that way, so a spare shows up
    # as the gap between them rather than by vanishing from the output entirely.
    assert result["servers"] == [99]

    # The dry run REPORTS the same candidate, because reporting is what `verify`
    # wants and nothing is destroyed by saying so.
    listed, _ = reap([server(99, 86400 * 9)], unreachable=[SOCKET],
                     answers=lambda name: True, apply=False)
    assert listed["servers"] == [99]
    assert listed["killed"] == []


# -- opening a frame must never kill anything -----------------------------------


def test_opening_a_frame_sweeps_socket_FILES_and_signals_nothing(tmp_path, monkeypatch):
    """The whole point of the correction.

    An earlier version reaped keep-alive shells from inside `open`. That turned
    opening a frame into a process killer running on a heuristic, which is a far
    bigger thing than clearing junk and is not what the open path is for. Opening
    a frame now removes dead socket FILES and stops; anything with a pid waits for
    `hive-ide reap --apply` and a person.

    Pinned by signalling, not by reading the source: `os.kill` is replaced, so any
    route from `open` to a signal — now or added later — fails this.
    """
    from hive_ide import cli, socket_gc

    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(socket_gc.os, "kill",
                        lambda pid, sig: signalled.append((pid, sig)))

    # A real orphan in the table, old enough and with no server, so the former
    # automatic path would certainly have selected it.
    rows = [shell(101, 86400)]
    monkeypatch.setattr(OrphanSweeper, "_ps", staticmethod(lambda: list(rows)))
    monkeypatch.setattr(OrphanSweeper, "server_answers", staticmethod(lambda name: False))

    socket_dir = tmp_path / "tmux-test"
    socket_dir.mkdir()
    # A REAL AF_UNIX socket file with nothing listening — a regular file is not
    # something the reaper can judge, so touching one would make the second
    # assertion below vacuous.
    stale = socket_dir / "hive-ide-stale"
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(str(stale))
    dead.close()
    # `SocketReaper.MIN_AGE_SECONDS` is an hour, so a socket created just now is
    # correctly left alone; backdate it or the sweep half of this test asserts
    # nothing.
    old_enough = time.time() - 2 * 3600
    os.utime(stale, (old_enough, old_enough))

    class _Frame:
        @staticmethod
        def socket_path():
            return str(socket_dir / "hive-ide-current")

    cli._reap_dead_sockets(_Frame(), keep=set())

    assert signalled == [], (
        f"opening a frame signalled {signalled} — the open path must clear socket "
        "files only, never processes"
    )
    # And it really did do its own job, so this is not passing by doing nothing.
    assert not (socket_dir / "hive-ide-stale").exists(), (
        "the dead socket FILE should still be swept on open"
    )


# -- the seventh review round ---------------------------------------------------


def test_a_server_addressed_by_socket_PATH_is_never_a_reap_candidate():
    """`-S /path` is not a name anything here can probe.

    Every reachability probe runs `tmux -L <name>`, which resolves inside the
    CALLER's socket directory. A healthy, detached server whose socket lives at
    some other path answers nothing there, so judging it by its basename reports a
    live server as an orphan and SIGTERMs it. The basename is good enough to
    recognise the family and protect the name; it is not good enough to kill on.
    """
    path_argv = (
        f"tmux -S /run/user/1000/other/{SOCKET} -f /state/ide.conf "
        f"new-session -d -s x -n N -c /w"
    )
    rows = [(99, 200000, path_argv)]

    servers, clients, _shells = OrphanSweeper.classify(rows)
    assert servers == {}, "a path-addressed server became a kill candidate"
    assert clients == {SOCKET: [99]}, "it should still protect its own name"

    result, kills = reap(rows, unreachable={SOCKET})
    assert result["servers"] == []
    assert kills.sent == [], "SIGTERMed a healthy server in another socket directory"


def test_a_path_addressed_server_is_not_rescued_by_a_trailing_L_flag():
    """`-S` OVERRIDES `-L`, so the presence of `-S` decides — not which matched
    first. Reading the `-L` name here would probe one socket and signal a process
    attached to a different one."""
    argv = (
        f"tmux -L {SOCKET} -S /run/user/1000/other/{SOCKET} "
        f"new-session -d -s x -n N -c /w"
    )
    servers, clients, _shells = OrphanSweeper.classify([(99, 200000, argv)])

    assert servers == {}, "-L won over -S and made a path-addressed server killable"
    assert clients == {SOCKET: [99]}


def test_a_server_that_recovers_during_another_candidates_probe_is_spared():
    """The probes are not instantaneous and the list is not re-asked as a batch.

    Each `confirm` call can block for its own ten-second timeout, so by the time
    the LAST candidate is signalled the FIRST one's answer is tens of seconds old —
    and a tmux server recreates its socket on SIGUSR1. Its pid and argv never
    change, so the identity check cannot catch this; only re-asking tmux in the
    instant before each signal can.
    """
    rows = [server(901, 200000, SOCKET), server(902, 200000, OTHER)]
    kills = _Kills()

    def answers(name: str) -> bool:
        # OTHER is dead while the list is built and comes back to life during the
        # walk — specifically, while the FIRST candidate is being dealt with. A
        # single batch probe ahead of the loop asks before that happens.
        if name == OTHER and kills.sent:
            return True
        return False

    sweeper = OrphanSweeper(
        rows=rows, kill=kills, answers=answers,
        socket_root=lambda pid: OrphanSweeper.our_socket_root())
    sweeper.TERM_GRACE_SECONDS = 0.0
    result = sweeper.reap_servers(unreachable={SOCKET, OTHER}, apply=True)

    assert (901, signal.SIGTERM) in kills.sent, "the genuine orphan was not reaped"
    assert (902, signal.SIGTERM) not in kills.sent, (
        "SIGTERMed a server that was answering again by the time its turn came"
    )
    assert result["killed"] == [901]


def test_a_pid_reused_by_an_IDENTICAL_command_line_is_still_not_signalled():
    """argv is not identity for a process spawned from a fixed command line.

    The keep-alive loop is built by one code path, so a fresh one wears a dead
    one's argv byte for byte. An argv-only check waves the replacement through and
    kills a shell that has been running for five seconds on behalf of a LIVE frame.
    Elapsed time is what separates them.
    """
    rows = [shell(101, 7200)]
    recycled = [shell(101, 1)]          # same pid, same argv, one second old
    snapshots = iter([rows])

    result, kills = sweep(lambda: next(snapshots, recycled))

    assert result["shells"] == [101], "the original candidate was still selected"
    assert kills.sent == [], (
        "signalled a one-second-old process that merely inherited the pid and argv"
    )


def test_the_same_process_a_little_older_is_still_signalled():
    """The falsifying half: age going UP must not spare anything, or the guard
    above would simply switch the reaper off."""
    rows = [shell(101, 7200)]
    older = [shell(101, 7260)]          # same process, one minute later
    snapshots = iter([rows])

    result, kills = sweep(lambda: next(snapshots, older))

    assert (101, signal.SIGTERM) in kills.sent


def test_a_one_second_age_wobble_does_not_spare_a_candidate():
    """`etime` has one-second granularity and the two reads are a moment apart, so
    an age a hair lower is sampling noise, not a new process."""
    rows = [shell(101, 7200)]
    jittered = [shell(101, 7199)]
    snapshots = iter([rows])

    result, kills = sweep(lambda: next(snapshots, jittered))

    assert (101, signal.SIGTERM) in kills.sent


# -- the eighth review round ----------------------------------------------------


def test_a_server_under_a_different_TMUX_TMPDIR_is_never_a_candidate():
    """The directory is nowhere in the argv, so the table cannot tell them apart.

    `tmux -L <name>` resolves inside the CALLER's socket directory. A healthy
    detached server started under another `TMUX_TMPDIR` answers nothing there and
    is indistinguishable, in the process table, from a genuine orphan — same
    argv, same name, same age. Only the process's own environment separates them,
    and it has to be consulted BEFORE the report, not just before the kill.
    """
    rows = [server(99, 86400 * 9)]

    result, kills = reap(
        rows, unreachable={SOCKET},
        socket_root=lambda pid: "/run/user/1000/somewhere-else",
    )

    assert result["servers"] == [], "reported a healthy server elsewhere as an orphan"
    assert kills.sent == [], "SIGTERMed a healthy server in another socket directory"


def test_a_server_whose_socket_directory_cannot_be_read_is_spared():
    """The unknown arm. Without `/proc` the reader cannot answer, and an answer we
    do not have is not evidence that the socket is where we looked."""
    result, kills = reap(
        [server(99, 86400 * 9)], unreachable={SOCKET},
        socket_root=lambda pid: None,
    )

    assert result["servers"] == []
    assert kills.sent == []


def test_a_server_in_OUR_socket_directory_is_still_reaped():
    """The falsifying half: the guard above must not simply switch the reaper off."""
    result, kills = reap(
        [server(99, 86400 * 9)], unreachable={SOCKET},
        socket_root=lambda pid: OrphanSweeper.our_socket_root(),
    )

    assert result["servers"] == [99]
    assert (99, signal.SIGTERM) in kills.sent


def test_an_unset_and_an_empty_TMUX_TMPDIR_both_mean_tmp():
    """tmux treats both as the default, so a server started with `TMUX_TMPDIR=`
    must not read as living somewhere else."""
    assert OrphanSweeper.resolve_root(None) == OrphanSweeper.resolve_root("")
    assert OrphanSweeper.resolve_root(None) == os.path.realpath("/tmp")


def test_the_socket_root_reader_answers_unknown_for_a_pid_it_cannot_read():
    """A pid that does not exist has no environment to read, and the reader says so
    rather than falling back to the default."""
    assert OrphanSweeper.socket_root_of(2 ** 22 + 7) is None


@pytest.mark.skipif(not Path("/proc/self/environ").exists(),
                    reason="no /proc: the reader answers unknown by design")
def test_the_socket_root_reader_reads_THAT_processs_environment_not_ours(tmp_path):
    """The readable case, and the whole point of reading per-process.

    A child is started with a `TMUX_TMPDIR` we do not have. The reader must come
    back with the CHILD's directory — if it ever answered with our own, the guard
    would say "same directory" for every process on the machine and protect
    nothing.

    `/proc/<pid>/environ` is the environment the process was EXEC'd with, which is
    exactly the one tmux read when it chose where to put the socket.
    """
    # The child announces that it has EXEC'd. Reading before that point is not a
    # near-miss: `/proc/<pid>/environ` serves zero bytes for the whole fork-to-exec
    # window, which is what `socket_root_of` must report as unknown.
    marker = tmp_path / "exec'd"
    child = subprocess.Popen(
        [sys.executable, "-c",
         f"import pathlib, time; pathlib.Path({str(marker)!r}).write_text('x'); "
         "time.sleep(60)"],
        env={**os.environ, "TMUX_TMPDIR": str(tmp_path)},
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "the child never got as far as running"

        theirs = OrphanSweeper.socket_root_of(child.pid)
        assert theirs == os.path.realpath(str(tmp_path))
        assert theirs != OrphanSweeper.our_socket_root(), (
            "read our own environment instead of the child's"
        )
    finally:
        child.kill()
        child.wait()


def test_an_environ_we_cannot_actually_read_is_unknown_not_the_default():
    """The hole the directory guard would otherwise have.

    `/proc/<pid>/environ` serves ZERO BYTES rather than failing when a process's
    memory is not readable. Finding no `TMUX_TMPDIR=` in that nothing once meant
    "unset", which resolves to `/tmp` — the default, so it matched our own
    directory and cleared the process to be killed on the strength of a read that
    saw nothing.
    """
    assert OrphanSweeper.root_from_environ(b"") is None
    assert OrphanSweeper.root_from_environ(b"\0\0\0") is None


def test_a_genuinely_unset_TMUX_TMPDIR_is_the_default_not_unknown():
    """The other half: a real environment that simply does not set it. These two
    must not collapse into one answer, or the guard either protects everything or
    protects nothing."""
    assert (OrphanSweeper.root_from_environ(b"PATH=/usr/bin\0HOME=/home/x\0")
            == os.path.realpath("/tmp"))


@pytest.mark.skipif(not Path("/proc/self/environ").exists(),
                    reason="no /proc: the reader answers unknown by design")
def test_the_kernel_really_does_serve_an_empty_environ_before_exec():
    """And the empty case is not hypothetical — this is where it comes from.

    Observational on purpose: it reads the kernel ONCE per child and only records
    what came back. Asserting the reader's answer here instead would read a second
    time, by which point the child has exec'd and a different answer is correct.
    """
    empties = 0
    for _attempt in range(20):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            if not Path(f"/proc/{child.pid}/environ").read_bytes().strip(b"\0"):
                empties += 1
        finally:
            child.kill()
            child.wait()
    if not empties:
        pytest.skip("never observed the pre-exec window on this host")
    assert empties, "the window the empty-environ rule exists for"


# -- the ninth review round -----------------------------------------------------


def test_a_socket_name_is_never_truncated_into_a_different_socket():
    """`-L hive-ide-project:dev` is not `-L hive-ide-project`.

    Reading the name up to the first inconvenient character produced a name that
    is in our family, is not this server's, and answers nothing when probed — so
    a healthy server with live panes read as an orphan. The whole value is taken,
    and a value we cannot vouch for forfeits candidacy instead of being trimmed
    until it looks acceptable.
    """
    odd = "hive-ide-project:dev"
    argv = f"tmux -L {odd} -f /state/ide.conf new-session -d -s x -n N -c /w"
    rows = [(99, 86400 * 9, argv)]

    servers, clients, _shells = OrphanSweeper.classify(rows)
    assert servers == {}, "an unprobeable name became a kill candidate"
    assert clients == {odd: [99]}, "the name should be read whole, and protect"
    assert "hive-ide-project" not in servers, "the name was truncated"

    result, kills = reap(rows, unreachable={odd, "hive-ide-project"})
    assert result["servers"] == []
    assert kills.sent == []


def test_a_grouped_short_flag_server_is_classified_so_it_protects_its_shells():
    """`tmux -uLname` is the same flag as `tmux -L name`.

    A server the classifier cannot see is absent from the table, which is the
    FIRST of the two negatives the shell sweep needs — so an unparsed server
    silently hands over half the protection its own sidebars depend on.
    """
    rows = [
        (99, 7200, f"tmux -uL{SOCKET} -f /state/ide.conf new-session -d -s x -n N"),
        shell(101, 7200),
    ]

    servers, _clients, _shells = OrphanSweeper.classify(rows)
    assert list(servers) == [SOCKET], "a grouped -uLname server was invisible"

    result, kills = sweep(rows)
    assert result["shells"] == [], "reaped a live frame's sidebars"
    assert kills.sent == []


def test_a_grouped_flag_server_in_ANOTHER_directory_still_protects_its_shells():
    """The two gaps Codex combined: an unparsed server AND a probe that looks in
    the wrong directory. Either alone is survivable — the table covers the probe
    and the probe covers the table — but together both negatives agree wrongly and
    the live frame's sidebars are killed. Parsing the flag is what keeps the table
    honest here; the shell path has no socket-directory evidence of its own."""
    rows = [
        (99, 7200, f"tmux -uL{SOCKET} -f /state/ide.conf new-session -d -s x -n N"),
        shell(101, 7200),
    ]

    # The probe answers for OUR directory, and this server is not in it.
    result, kills = sweep(rows, answers=lambda name: False)

    assert result["shells"] == [], "killed the sidebars of a live frame elsewhere"
    assert kills.sent == []


def test_a_dash_S_in_the_PANE_command_protects_rather_than_renames():
    """A server's argv ends with the command its first pane was started with, so a
    `-S` in it may be that command's flag, not tmux's. Letting it name the socket
    would drop the server out of classification entirely — and its sidebars with
    it. `-L` still names; the stray `-S` only forfeits candidacy."""
    argv = (
        f"tmux -L {SOCKET} -f /state/ide.conf new-session -d -s x -n N -c /w "
        f"vim -S /home/someone/session.vim"
    )
    rows = [(99, 86400 * 9, argv), shell(101, 7200)]

    servers, clients, _shells = OrphanSweeper.classify(rows)
    assert servers == {}, "a server with a stray -S must not be a candidate"
    assert clients == {SOCKET: [99]}, "it must still be found under its REAL name"

    result, kills = sweep(rows)
    assert result["shells"] == [], "the server was misnamed and its shells died"
    assert kills.sent == []


def test_a_keep_alive_whose_socket_cannot_be_read_cleanly_is_left_alone():
    """The flag's value sits inside a shell loop, so it ends at `;`. A name that
    runs into anything else is a name we did not read — and an unattributed shell
    is left running, not assigned to whichever socket the prefix resembles."""
    odd = SHELL_ARGV.format(socket="hive-ide-abc12345:extra")
    rows = [(101, 7200, odd)]

    _servers, _clients, shells = OrphanSweeper.classify(rows)
    assert shells == {}, "a shell was attributed to a socket name it does not use"

    result, kills = sweep(rows)
    assert result["shells"] == []
    assert kills.sent == []


# -- the tenth review round -----------------------------------------------------


@pytest.mark.skipif(not Path("/proc/self/environ").exists(),
                    reason="no /proc: the reader answers unknown by design")
def test_a_relative_TMUX_TMPDIR_is_unknown_not_resolved_against_our_cwd(tmp_path):
    """`TMUX_TMPDIR=.` means "wherever THAT process was", and we are not there.

    Resolving it here answers with a directory the server may never have seen —
    and when the answer happens to land on ours, that reads as agreement and
    clears a healthy server to be killed.
    """
    child = subprocess.Popen(
        [sys.executable, "-c",
         f"import pathlib, time; pathlib.Path({str(tmp_path / 'up')!r}).write_text('x'); "
         "time.sleep(60)"],
        cwd=str(tmp_path),
        env={**os.environ, "TMUX_TMPDIR": "."},
    )
    try:
        deadline = time.monotonic() + 20
        while not (tmp_path / "up").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "up").exists(), "the child never got as far as running"

        assert OrphanSweeper.socket_root_of(child.pid) is None, (
            "a relative root was resolved against the reaper's own directory"
        )
    finally:
        child.kill()
        child.wait()


def test_an_absolute_root_is_still_read():
    """The falsifying half: the rule above must reject only what it cannot place."""
    assert OrphanSweeper.resolve_root("/run/user/1000/tmux") == "/run/user/1000/tmux"


def test_a_server_that_recovers_during_the_GRACE_window_is_not_sigkilled():
    """SIGTERM is refusable; SIGKILL is not.

    A server can ignore the SIGTERM, recreate its socket and start serving panes
    again inside the grace window — with the same pid and the same argv, so the
    identity check passes it straight through to the one signal it cannot
    survive. Reachability has to be asked again here too, not only before the
    first signal.
    """
    rows = [server(99, 86400 * 9)]
    kills = _Kills(survive={99})
    answered: list[int] = []

    def answers(name: str) -> bool:
        answered.append(1)
        return False if len(answered) == 1 else True   # back up after the SIGTERM

    sweeper = OrphanSweeper(
        rows=rows, kill=kills, answers=answers,
        socket_root=lambda pid: OrphanSweeper.our_socket_root())
    sweeper.TERM_GRACE_SECONDS = 0.0

    sweeper.reap_servers(unreachable={SOCKET}, apply=True)

    assert (99, signal.SIGTERM) in kills.sent, "it was an orphan when we signalled"
    assert (99, signal.SIGKILL) not in kills.sent, (
        "SIGKILLed a server that was serving panes again by the end of the grace"
    )


def test_a_server_still_unreachable_after_the_grace_IS_sigkilled():
    """And the falsifying half, or the recheck above would simply disable
    escalation."""
    rows = [server(99, 86400 * 9)]
    kills = _Kills(survive={99})

    sweeper = OrphanSweeper(
        rows=rows, kill=kills, answers=lambda name: False,
        socket_root=lambda pid: OrphanSweeper.our_socket_root())
    sweeper.TERM_GRACE_SECONDS = 0.0

    sweeper.reap_servers(unreachable={SOCKET}, apply=True)

    assert (99, signal.SIGKILL) in kills.sent
