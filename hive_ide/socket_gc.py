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

    Two populations, judged differently, because the evidence differs:

    **A keep-alive shell whose server PROCESS is gone** is unambiguously dead. It
    has no pane to paint and no server to ask; nothing it does can ever reach a
    screen again. Only a short age floor applies, so a frame starting up is never
    caught mid-launch.

    **A server that is merely unreachable** is judged much more slowly. It still
    owns live panes, and — the part that makes haste dangerous — a client that
    attached BEFORE its socket file was deleted keeps its open descriptor and
    stays connected, so an unreachable server can still be on someone's screen.
    It is reaped only when no client process references it at all and it has been
    unreachable for a day.

    Reachability is never inferred from the socket file alone: the file can be
    missing while the server runs, and present while the server is dead.

    Bounded, fail-open, and every kill independent: this runs inside `open` and
    must never make opening a frame slow, let alone fail it.
    """

    #: Never race a frame that is starting: its shells exist before its server
    #: finishes binding.
    SHELL_MIN_AGE_SECONDS = 300
    #: An unreachable server is only called abandoned after a day.
    SERVER_MIN_AGE_SECONDS = 86400
    MAX_KILLS = 200
    TERM_GRACE_SECONDS = 2.0

    _SOCKET_FLAG = re.compile(r"-L\s+([A-Za-z0-9._-]+)")
    _TMUX_SOCKET_FLAG = re.compile(r"--tmux-socket\s+([A-Za-z0-9._-]+)")

    def __init__(
        self,
        *,
        rows: list[tuple[int, int, str]] | None = None,
        kill: Callable[[int, int], None] | None = None,
        now: float | None = None,
    ):
        self._rows = rows
        self._kill = kill or os.kill
        self.now = time.time() if now is None else now

    # -- process table ----------------------------------------------------------

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
                if "hive_ide.sidebar" not in argv:
                    continue
                match = cls._TMUX_SOCKET_FLAG.search(argv)
                if match and SocketReaper.ours(match.group(1)):
                    shells.setdefault(match.group(1), []).append((pid, age))
                continue
            match = cls._SOCKET_FLAG.search(argv)
            if not match or not SocketReaper.ours(match.group(1)):
                continue
            name = match.group(1)
            # A client attaches. A server keeps its start argv, and the process
            # that merely STARTS one shares that argv but exits in milliseconds,
            # which the age floors exclude.
            if "attach" in argv:
                clients.setdefault(name, []).append(pid)
            else:
                servers.setdefault(name, []).append((pid, age))
        return servers, clients, shells

    # -- sweep ------------------------------------------------------------------

    def sweep(
        self,
        *,
        reachable: set[str],
        keep: set[str] | None = None,
        apply: bool = False,
    ) -> dict:
        """Report, and optionally kill, the orphans.

        `reachable` is the set of socket names whose server answered a connect —
        the caller owns that probe, because it is the one expensive step and
        `SocketReaper.is_listening` already does it per entry.
        """
        protected = set(keep or ()) | set(reachable)
        rows = self._rows if self._rows is not None else self._ps()
        servers, clients, shells = self.classify(rows)

        doomed_servers: list[tuple[str, int]] = []
        kept_servers: set[str] = set()
        for name, entries in sorted(servers.items()):
            for pid, age in entries:
                if (
                    name not in protected
                    and not clients.get(name)
                    and age >= self.SERVER_MIN_AGE_SECONDS
                ):
                    doomed_servers.append((name, pid))
                else:
                    kept_servers.add(name)

        doomed_shells: list[tuple[str, int]] = []
        for name, entries in sorted(shells.items()):
            if name in protected or name in kept_servers:
                # Either reachable, or its server survived a check above and may
                # still be on a screen through an already-open descriptor.
                continue
            if name in servers and name not in {n for n, _ in doomed_servers}:
                continue
            for pid, age in entries:
                if age >= self.SHELL_MIN_AGE_SECONDS:
                    doomed_shells.append((name, pid))

        doomed = (doomed_servers + doomed_shells)[: self.MAX_KILLS]
        result = {
            "servers": [pid for _n, pid in doomed_servers],
            "shells": [pid for _n, pid in doomed_shells],
            "sockets": sorted({name for name, _pid in doomed}),
            "applied": apply,
            "killed": [],
        }
        if not apply:
            return result

        killed: list[int] = []
        for _name, pid in doomed:
            try:
                self._kill(pid, signal.SIGTERM)
                killed.append(pid)
            except OSError:
                continue
        if killed:
            time.sleep(self.TERM_GRACE_SECONDS)
            for pid in killed:
                try:
                    self._kill(pid, 0)
                except OSError:
                    continue      # already gone: SIGTERM was enough
                try:
                    self._kill(pid, signal.SIGKILL)
                except OSError:
                    continue
        result["killed"] = killed
        return result
