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


def _sweeper(rows, *, survive=None, argv_of=None, answers=None, **kw):
    kills = _Kills(survive)
    # By default a survivor still carries the argv it was matched on, so the
    # identity re-check passes and SIGKILL escalation is exercised. A test that
    # cares about pid REUSE overrides `argv_of`.
    table = {pid: argv for pid, _age, argv in rows}
    # `answers` defaults to "tmux says there is no server there", so the rows stay
    # the thing under test; without it every candidate would shell out to the real
    # tmux and the suite would depend on this machine's sockets.
    sweeper = OrphanSweeper(
        rows=list(rows), kill=kills,
        argv_of=argv_of or (lambda pid: table.get(pid)),
        answers=answers if answers is not None else (lambda name: False), **kw)
    sweeper.TERM_GRACE_SECONDS = 0.0
    return sweeper, kills


def sweep(rows, *, apply=True, survive=None, argv_of=None, answers=None, **kw):
    """The AUTOMATIC path: shells only, no reachability argument."""
    sweeper, kills = _sweeper(rows, survive=survive, argv_of=argv_of, answers=answers, **kw)
    return sweeper.sweep(apply=apply), kills


def reap(rows, *, unreachable=(), apply=True, survive=None, argv_of=None, answers=None, **kw):
    """The HUMAN-DRIVEN path: servers, and only names positively confirmed dead."""
    sweeper, kills = _sweeper(rows, survive=survive, argv_of=argv_of, answers=answers, **kw)
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
    from hive_ide import socket_gc

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
    sweeper = OrphanSweeper(rows=lambda: next(snapshots, reused), kill=kills)
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

    found = OrphanSweeper(rows=rows).orphan_servers(unreachable={SOCKET})
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

    assert result["servers"] == [], "killed a server that had come back"
    assert kills.sent == []

    # The dry run still REPORTS it, because reporting is what `verify` wants and
    # nothing is destroyed by saying so.
    listed, _ = reap([server(99, 86400 * 9)], unreachable=[SOCKET],
                     answers=lambda name: True, apply=False)
    assert listed["servers"] == [99]
