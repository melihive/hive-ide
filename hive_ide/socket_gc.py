"""Remove this package's own abandoned tmux sockets.

A tmux socket file outlives the server that created it. Every workspace that has
ever opened a frame leaves one behind, and so does every test run, so the tmux
directory accumulates hundreds of dead entries that make it unreadable and slow
every socket listing down.

Deleting a socket whose server is alive would cut every attached client off from
it, so this is deliberately timid. An entry is removed only when ALL of these
hold, and any one of them failing to be provable keeps the file:

1. it sits directly in this user's tmux directory,
2. its name is one this package hands out,
3. it is a socket — not a symlink, file or directory,
4. it is owned by this user,
5. it is not a socket the caller asked to keep,
6. it has not been touched recently, so a server binding right now is not caught,
7. connecting to it is REFUSED — not merely an error, which is unknown.

The sweep is bounded, every removal is independent, and any error skips that
entry rather than failing the caller. Nothing here is worth interrupting an
`open` for.
"""

from __future__ import annotations

import errno
import os
import re
import signal
import socket
import stat
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Callable


class SocketReaper:
    """Sweep dead tmux sockets this package left behind."""

    PREFIX = "hive-ide-"
    #: The socket-name families this package hands out — an allowlist, so a
    #: plain `default` belonging to somebody else is never touched.
    #:
    #: Deliberately NOT widened. One machine in 2026-11 held 190 dead sockets
    #: under other `ide-`-ish names, and it is tempting to sweep those too — but
    #: they are created by a HOST integration that embeds this package, not by
    #: the package itself. A shipped package deleting another codebase's sockets
    #: is exactly what the foreign-name test in `test_socket_gc.py` exists to
    #: prevent. Their cleanup belongs to whoever creates them.
    PREFIXES = ("hive-ide-",)
    MIN_AGE_SECONDS = 3600
    MAX_REMOVALS = 500
    CONNECT_TIMEOUT = 0.25

    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        now: float | None = None,
    ):
        self.directory = Path(directory) if directory else self.default_directory()
        self.now = now if now is not None else time.time()

    @classmethod
    def ours(cls, name: str) -> bool:
        """Is this a socket name this package hands out?"""
        return any(name.startswith(prefix) for prefix in cls.PREFIXES)

    @staticmethod
    def default_directory() -> Path:
        """Where tmux keeps this process's sockets: `TMUX_TMPDIR`, else `/tmp`.

        `TMPDIR` is deliberately NOT consulted, because tmux does not consult it
        either — `tmux(1)`: "the sockets are all created in a directory tmux-UID
        under the directory given by TMUX_TMPDIR or in /tmp". Adding a `TMPDIR`
        step here looks like a portability fix and is the opposite: macOS sets a
        per-user `TMPDIR` for every GUI and launchd session, so this would point
        at a directory tmux never uses, and `socket_path` — which every caller
        verdict compares against a real `$TMUX` — would stop recognising the
        server the caller is actually on. Verified against tmux 3.8.
        """
        base = os.environ.get("TMUX_TMPDIR") or "/tmp"
        return Path(base) / f"tmux-{os.getuid()}"

    @staticmethod
    def is_listening(path: Path) -> bool | None:
        """True if a server answers, False if REFUSED, None if it cannot be told.

        Only an explicit refusal proves the socket is dead. A permission error, a
        timeout or anything else is unknown, and unknown keeps the file.
        """
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(SocketReaper.CONNECT_TIMEOUT)
            connection.connect(str(path))
            return True
        except OSError as exc:
            if exc.errno == errno.ECONNREFUSED:
                return False
            return None
        finally:
            try:
                connection.close()
            except OSError:
                pass

    @staticmethod
    def _identity(entry: Path) -> tuple[int, int, float] | None:
        """What this path pointed at when we looked: device, inode, mtime."""
        try:
            info = entry.lstat()
        except OSError:
            return None
        return (info.st_dev, info.st_ino, info.st_mtime)

    def _is_reapable(self, entry: Path, keep: set[str]) -> tuple[int, int, float] | None:
        """The identity we judged, or None when the entry must be kept."""
        if not self.ours(entry.name) or entry.name in keep:
            return None
        try:
            info = entry.lstat()
        except OSError:
            return None
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return None
        if self.now - info.st_mtime < self.MIN_AGE_SECONDS:
            return None
        if self.is_listening(entry) is not False:
            return None
        return (info.st_dev, info.st_ino, info.st_mtime)

    def _still_the_same_dead_socket(
        self, entry: Path, judged: tuple[int, int, float]
    ) -> bool:
        """Re-check identity immediately before unlinking.

        Between the refusal probe and the unlink, another workspace can start its
        tmux server on this very path. tmux binds by creating a NEW socket file,
        so the inode changes — and a freshly bound socket also has a fresh mtime,
        which the age floor rejects on its own. Checking both just before the
        unlink turns "the server started while we were deciding" from a deleted
        live socket into a skipped entry.

        This narrows the window rather than closing it: POSIX offers no
        unlink-if-unchanged, so a bind landing between this stat and the unlink is
        still possible. It is microseconds wide, and the cost if it ever lands is
        a server that new clients cannot reach until its frame is reopened —
        attached clients hold an open fd and are unaffected. That is the right
        trade for a sweep whose only job is removing clutter.
        """
        return self._identity(entry) == judged

    def inventory(self) -> int | None:
        """How many of this package's socket files have no server behind them.

        Deliberately NOT the sweep's candidate list. `sweep` answers "what may I
        delete right now", which excludes everything under `MIN_AGE_SECONDS` and
        stops at `MAX_REMOVALS` — so reporting it as inventory said ZERO on an
        afternoon that had just produced 150 fresh ones, which is precisely the
        question inventory is asked.

        None when the directory cannot be read: "could not look" must never render
        as "none". Symlinks and non-sockets are skipped, as in the sweep.
        """
        try:
            entries = sorted(self.directory.iterdir())
        except OSError:
            return None
        count = 0
        for entry in entries:
            if not entry.name.startswith(self.PREFIX):
                continue
            try:
                if entry.is_symlink() or not entry.is_socket():
                    continue
            except OSError:
                continue
            if self.is_listening(entry) is False:
                count += 1
        return count

    def sweep(self, *, keep: set[str] | None = None, apply: bool = False) -> dict:
        """Report, and optionally remove, the dead sockets in this directory."""
        kept = set(keep or ())
        dead: list[str] = []
        removed: list[str] = []
        scanned = 0
        try:
            entries = sorted(self.directory.iterdir())
        except OSError:
            return {
                "directory": str(self.directory),
                "scanned": 0,
                "dead": [],
                "removed": [],
                "applied": apply,
            }
        for entry in entries:
            scanned += 1
            if len(dead) >= self.MAX_REMOVALS:
                break
            judged = self._is_reapable(entry, kept)
            if judged is None:
                continue
            dead.append(entry.name)
            if not apply:
                continue
            if not self._still_the_same_dead_socket(entry, judged):
                continue
            try:
                entry.unlink()
            except OSError:
                continue
            removed.append(entry.name)
        return {
            "directory": str(self.directory),
            "scanned": scanned,
            "dead": dead,
            "removed": removed,
            "applied": apply,
        }


class OrphanSweeper:
    """Kill this package's processes that nothing can reach any more.

    `SocketReaper` removes dead socket FILES. It never removes processes, so the
    expensive leftovers survived every sweep: a tmux server whose socket file has
    gone keeps running with every pane it owned, and each sidebar keep-alive shell
    under it keeps forking twice a second — a `tmux display-message` and a guard
    subshell — to ask a server it can no longer reach whether its window is
    visible. Measured on one machine in 2026-11: a server up 65 hours with 30
    such shells, about 60 wasted forks a second, invisible to every existing
    check.

    Two populations, and only ONE of them is ever killed automatically, because
    only one of them has self-contained evidence:

    **A keep-alive shell whose server PROCESS is absent from the whole table** is
    unambiguously dead — `sweep` reaps it. It has no pane to paint and no server
    to ask; nothing it does can ever reach a screen again. The evidence needs no
    socket probe, so it cannot be confused by a socket directory we did not look
    in: if a server for that name existed anywhere, under any `TMUX_TMPDIR`, it
    would be in the same `ps` snapshot and the shell would be spared. Only a
    short age floor applies, so a frame starting up is never caught mid-launch.

    **A server that is merely unreachable is NEVER killed automatically.** It
    still owns live panes, and a client that attached BEFORE its socket file was
    deleted keeps its open descriptor and stays connected — so an unreachable
    server can still be on someone's screen. Worse, "unreachable" is only ever
    unreachable *from the namespace we probed*: a server under a different
    `TMUX_TMPDIR` is perfectly healthy and answers nothing here, and argv alone
    cannot tell us which directory it used. So `orphan_servers` only REPORTS
    them (for `verify`), and `reap_servers` kills them solely when a human asks.

    Reachability is never inferred from the socket file alone: the file can be
    missing while the server runs, and present while the server is dead. And it
    is three-state throughout — a probe that could not conclude protects the
    server, because the negative branch here is the one that destroys.

    Bounded, fail-open, and every kill independent: this runs inside `open` and
    must never make opening a frame slow, let alone fail it.
    """

    #: Never race a frame that is starting: its shells exist before its server
    #: finishes binding.
    SHELL_MIN_AGE_SECONDS = 300
    #: A server must have been up at least this long before `reap_servers` will
    #: consider it. This is a floor on the server's OWN AGE, not on how long it
    #: has been unreachable — nothing here persists across runs, so the duration
    #: of unreachability is simply not knowable. It is therefore a weak guard,
    #: which is exactly why the server path is human-driven rather than automatic.
    SERVER_MIN_AGE_SECONDS = 86400
    MAX_KILLS = 200
    TERM_GRACE_SECONDS = 2.0

    #: tmux accepts the value joined to the flag (`-Lname`), so the separator is
    #: OPTIONAL. Requiring whitespace made such a server invisible to `classify`,
    #: and an invisible server means its live sidebars look orphaned and get
    #: reaped — the asymmetry that makes a missed match expensive rather than
    #: merely untidy. Anchored at a token boundary so `-L` inside a path or a
    #: quoted string cannot masquerade as the flag.
    _SOCKET_FLAG = re.compile(r"(?<!\S)-L\s*([A-Za-z0-9._-]+)")
    _TMUX_SOCKET_FLAG = re.compile(r"(?<!\S)--tmux-socket[\s=]\s*([A-Za-z0-9._-]+)")
    #: `tmux -S <path> attach` names the socket by PATH and carries no `-L`, so a
    #: client matched on `-L` alone is invisible and its server loses the one
    #: protection that keeps a live screen alive. The basename IS the socket name.
    _SOCKET_PATH_FLAG = re.compile(r"(?<!\S)-S\s*(\S+)")
    #: A standalone `-d`: what makes a `new-session` a SERVER start rather than a
    #: client that attaches. `attach -d` is excluded before this is consulted.
    _DETACHED_FLAG = re.compile(r"(?<!\S)-d(?!\S)")
    #: The keep-alive is a SHELL running a specific loop. Matching merely on the
    #: strings `hive_ide.sidebar` + `--tmux-socket` condemned anything that
    #: mentioned them — a diagnostic `grep`, or an agent process investigating
    #: this very code — so the executable and the loop's own shape both have to
    #: agree before anything is signalled.
    _SHELL_COMMANDS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish"})
    #: Markers of the real loop: the visibility guard it polls, and the `while`
    #: that makes it a loop rather than a one-shot mention.
    _KEEPALIVE_MARKERS = ("window_active", "hive_ide.sidebar", "while")

    def __init__(
        self,
        *,
        rows: list[tuple[int, int, str]]
             | Callable[[], list[tuple[int, int, str]]]
             | None = None,
        kill: Callable[[int, int], None] | None = None,
        argv_of: Callable[[int], str | None] | None = None,
        now: float | None = None,
    ):
        #: A list pins ONE snapshot; a CALLABLE is re-read on every access, which
        #: is how a test can make the process table change between selecting a
        #: candidate and signalling it — the pid-reuse window. Production passes
        #: neither and gets `_ps`.
        self._rows = rows
        self._kill = kill or os.kill
        self._argv_reader = argv_of
        self.now = time.time() if now is None else now

    # -- process table ----------------------------------------------------------

    def _snapshot(self) -> list[tuple[int, int, str]]:
        """A fresh read of the process table, honouring an injected source."""
        if self._rows is None:
            return self._ps()
        if callable(self._rows):
            return list(self._rows())
        return list(self._rows)

    @staticmethod
    def _ps() -> list[tuple[int, int, str]]:
        """(pid, age_seconds, argv) for this user's processes.

        `ps -axww -o pid=,etime=,args=` and nothing else: `-o comm=` prints
        "tmux: server" WITH A SPACE, so a whitespace field split silently moves
        "server ..." into the argv column and the server stops being recognised
        at all. On macOS `pgrep -a` means "include ancestors" and prints no
        command line, so pgrep is not an option either.
        """
        try:
            result = subprocess.run(
                ["ps", "-axww", "-o", "pid=,etime=,args="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return []
        rows: list[tuple[int, int, str]] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) != 3 or not parts[0].isdigit():
                continue
            rows.append((int(parts[0]), OrphanSweeper.etime_seconds(parts[1]), parts[2]))
        return rows

    @staticmethod
    def etime_seconds(etime: str) -> int:
        """`ps` elapsed time to seconds: `[[dd-]hh:]mm:ss`."""
        days, _, rest = etime.partition("-")
        if not rest:
            rest, days = days, "0"
        try:
            bits = [int(b) for b in rest.split(":")]
        except ValueError:
            return 0
        while len(bits) < 3:
            bits.insert(0, 0)
        return int(days) * 86400 + bits[0] * 3600 + bits[1] * 60 + bits[2]

    # -- classification ---------------------------------------------------------

    @classmethod
    def classify(
        cls, rows: list[tuple[int, int, str]]
    ) -> tuple[dict[str, list[tuple[int, int]]], dict[str, list[int]], dict[str, list[tuple[int, int]]]]:
        """(servers, clients, shells) keyed by socket name.

        Decided from the EXECUTABLE first. A tmux server keeps the whole argv it
        was started with, and for a frame that argv contains the pane command —
        including `sh -c '... hive_ide.sidebar ...'`. Matching the sidebar string
        first therefore classifies the server as one of its own keep-alive
        shells, and the server then hides among them.
        """
        servers: dict[str, list[tuple[int, int]]] = {}
        clients: dict[str, list[int]] = {}
        shells: dict[str, list[tuple[int, int]]] = {}
        mine = os.getpid()
        for pid, age, argv in rows:
            if pid == mine or not argv:
                continue
            executable = PurePosixPath(argv.split(None, 1)[0]).name
            if executable != "tmux":
                if executable not in cls._SHELL_COMMANDS:
                    continue        # not a shell: cannot be the keep-alive loop
                if not all(marker in argv for marker in cls._KEEPALIVE_MARKERS):
                    continue        # mentions the sidebar; is not the loop
                match = cls._TMUX_SOCKET_FLAG.search(argv)
                if match and SocketReaper.ours(match.group(1)):
                    shells.setdefault(match.group(1), []).append((pid, age, argv))
                continue
            match = cls._SOCKET_FLAG.search(argv)
            name = match.group(1) if match else None
            if name is None:
                by_path = cls._SOCKET_PATH_FLAG.search(argv)
                name = PurePosixPath(by_path.group(1)).name if by_path else None
            if name is None or not SocketReaper.ours(name):
                continue
            # Only a DETACHED `new-session -d` is a server. `tmux -L X new-session
            # -A -s work` is a client that happens not to say "attach", and calling
            # it a server let `reap` kill a live client and its panes. Everything
            # unrecognised falls to `clients`, which is the protective side: a
            # misread client spares a server, a misread server kills one.
            #
            # A server retains the argv of whatever client first started it, so if
            # that was an attaching form the server reads as a client too — again
            # erring toward keeping it.
            if "attach" in argv:
                clients.setdefault(name, []).append(pid)
            elif "new-session" in argv and cls._DETACHED_FLAG.search(argv):
                servers.setdefault(name, []).append((pid, age, argv))
            else:
                clients.setdefault(name, []).append(pid)
        return servers, clients, shells

    # -- sweep ------------------------------------------------------------------

    def sweep(self, *, keep: set[str] | None = None, apply: bool = False) -> dict:
        """Reap keep-alive shells whose server process is GONE. Servers: never.

        Takes no reachability argument on purpose. A shell is judged against the
        same `ps` snapshot that found it: if no server process anywhere names its
        socket, no server exists for it to reach, in any socket directory, and the
        shell can never paint a pane again. That is positive evidence, not the
        absence of a probe result — so there is no unknown state to mishandle and
        no directory to be scoped wrongly.
        """
        protected = set(keep or ())
        rows = self._snapshot()
        servers, clients, shells = self.classify(rows)

        doomed: list[tuple[str, int]] = []
        for name, entries in sorted(shells.items()):
            if name in protected:
                continue
            if servers.get(name):
                continue        # its server is alive somewhere — leave it alone
            if clients.get(name):
                # A client still names this socket. Its server is absent from the
                # table, so this should be impossible; treat the contradiction as
                # unknown and keep everything rather than act on a confused read.
                continue
            for pid, age, argv in entries:
                if age >= self.SHELL_MIN_AGE_SECONDS:
                    doomed.append((name, pid, argv))

        doomed = doomed[: self.MAX_KILLS]
        result = {
            "servers": [],
            "shells": [pid for _n, pid, _a in doomed],
            "sockets": sorted({name for name, _pid, _a in doomed}),
            "applied": apply,
            "killed": self._terminate([(pid, argv) for _n, pid, argv in doomed]) if apply else [],
        }
        return result

    @staticmethod
    def server_answers(name: str) -> bool | None:
        """Does a server answer on this socket NAME? True / False / None.

        Three states on purpose. `True` and `None` both protect; only a positive
        `False` — tmux telling us there is no server there — makes a name a
        candidate. Probing by NAME rather than by socket file is what matters for
        an orphan: its file is gone, so a directory listing can never see it,
        while tmux resolves the name exactly as it did when the server started.
        """
        try:
            probe = subprocess.run(["tmux", "-L", name, "has-session"],
                                   capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        if probe.returncode == 0:
            return True
        blob = ((probe.stderr or "") + (probe.stdout or "")).lower()
        if "session not found" in blob or "can't find session" in blob:
            return True        # the server answered; it just has no such session
        if "no server running" in blob:
            return False       # tmux resolved the name and found nothing there
        # `error connecting to <path> (<errno>)` — the ERRNO decides, not the
        # prefix. A deleted socket (the orphan case, verified against a real one)
        # reports ENOENT here, so the whole branch cannot be dismissed; but
        # "Permission denied" in the same shape means we were not ALLOWED to look,
        # which is not evidence that nothing is there. Reading the prefix alone
        # either way is wrong: taking it all as False let an EACCES authorise a
        # kill, and taking it all as unknown stopped every real orphan being found.
        if "error connecting" in blob:
            if "no such file or directory" in blob or "connection refused" in blob:
                return False
            return None
        return None

    def confirmed_unreachable(self, names: set[str]) -> set[str]:
        """The subset of `names` a probe POSITIVELY confirmed has no server."""
        return {name for name in sorted(names) if self.server_answers(name) is False}

    def server_names(self) -> set[str]:
        """Socket names with a live server process, from the process table."""
        rows = self._snapshot()
        servers, _clients, _shells = self.classify(rows)
        return set(servers)

    def orphan_servers(
        self,
        *,
        unreachable: set[str],
        keep: set[str] | None = None,
    ) -> list[dict]:
        """Servers that look abandoned — a REPORT, killing nothing.

        `unreachable` holds only the socket names a probe POSITIVELY confirmed
        dead. A name the caller could not conclude about is simply absent, so it
        is never a candidate: unknown protects, by construction rather than by a
        branch somebody has to remember to write.
        """
        protected = set(keep or ())
        rows = self._snapshot()
        servers, clients, _shells = self.classify(rows)

        return [
            {"socket": name, "pid": pid, "age_seconds": age}
            for name, pid, age, _argv in self._orphan_server_rows(
                unreachable=unreachable, keep=keep)
        ]

    def _orphan_server_rows(
        self,
        *,
        unreachable: set[str],
        keep: set[str] | None = None,
    ) -> list[tuple[str, int, int, str]]:
        """(socket, pid, age, argv) for the servers that look abandoned.

        The argv is the identity `_terminate` revalidates against, so it has to
        travel with the candidate rather than be re-derived later.
        """
        protected = set(keep or ())
        rows = self._snapshot()
        servers, clients, _shells = self.classify(rows)

        found: list[tuple[str, int, int, str]] = []
        for name, entries in sorted(servers.items()):
            if name in protected or name not in unreachable:
                continue
            if clients.get(name):
                continue        # a client holds it: it may still be on a screen
            for pid, age, argv in entries:
                if age < self.SERVER_MIN_AGE_SECONDS:
                    continue
                found.append((name, pid, age, argv))
        return found

    def reap_servers(
        self,
        *,
        unreachable: set[str],
        keep: set[str] | None = None,
        apply: bool = False,
    ) -> dict:
        """Kill the reported orphan servers. Only ever called because a human asked.

        This is the destructive half, deliberately NOT wired into `open`: it can
        take down live panes, and "unreachable" is only ever true of the namespace
        we probed. A person reading `verify` decides; this carries it out.
        """
        found = self._orphan_server_rows(unreachable=unreachable, keep=keep)[: self.MAX_KILLS]
        return {
            "servers": [pid for _n, pid, _age, _a in found],
            "shells": [],
            "sockets": sorted({name for name, _p, _age, _a in found}),
            "applied": apply,
            "killed": self._terminate([(pid, argv) for _n, pid, _age, argv in found]) if apply else [],
        }

    # -- killing ----------------------------------------------------------------

    def _terminate(self, candidates: list[tuple[int, str]]) -> list[int]:
        """SIGTERM, grace, then SIGKILL — re-checking IDENTITY before BOTH signals.

        `kill(pid, 0)` proves only that the NUMBER is live. A pid can be recycled
        at two different moments, and both needed closing:

        Before the SIGTERM, because the gap between selecting a candidate and
        signalling it is not small on the server path — `confirmed_unreachable`
        runs a tmux probe per name, each with its own timeout, all inside that
        window. So the table is re-read here and a pid whose argv has changed is
        dropped rather than signalled.

        Before the SIGKILL, because the grace window is another such gap.

        The argv we matched IS the identity. An argv we cannot read is unknown, and
        unknown authorises no signal at all.
        """
        was = dict(candidates)
        fresh = {pid: argv for pid, _age, argv in self._snapshot()}
        killed: list[int] = []
        for pid, expected in candidates:
            if fresh.get(pid) != expected:
                continue        # exited, or the pid now belongs to something else
            try:
                self._kill(pid, signal.SIGTERM)
                killed.append(pid)
            except OSError:
                continue
        if not killed:
            return killed
        time.sleep(self.TERM_GRACE_SECONDS)
        for pid in killed:
            try:
                self._kill(pid, 0)
            except OSError:
                continue            # gone: SIGTERM was enough
            if self._argv_of(pid) != was.get(pid):
                continue            # different process, or unreadable — leave it
            try:
                self._kill(pid, signal.SIGKILL)
            except OSError:
                continue
        return killed

    def _argv_of(self, pid: int) -> str | None:
        """This pid's CURRENT argv, or None when it cannot be read."""
        if self._argv_reader is not None:
            return self._argv_reader(pid)
        try:
            probe = subprocess.run(["ps", "-p", str(pid), "-o", "args="],
                                   capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return probe.stdout.strip() or None
