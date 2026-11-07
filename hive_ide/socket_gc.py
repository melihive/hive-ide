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

    NOTHING here runs automatically. Opening a frame sweeps dead socket FILES and
    stops; every method below is reached only from `hive-ide reap`, because
    killing a process may take panes somebody is looking at, and that is a
    person's decision rather than a side effect of opening a frame.

    Two populations, judged differently, because the evidence differs:

    **A keep-alive shell whose server PROCESS is absent from the whole table** is
    the unambiguous one — `sweep` selects it. It has no pane to paint and no server
    to ask; nothing it does can ever reach a screen again. The evidence needs no
    socket probe, so it cannot be confused by a socket directory we did not look
    in: if a server for that name existed anywhere, under any `TMUX_TMPDIR`, it
    would be in the same `ps` snapshot and the shell would be spared. Only a
    short age floor applies, so a frame starting up is never caught mid-launch.

    **A server that is merely unreachable is the dangerous one.** It
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

    Bounded, fail-open, and every kill independent.
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
    #: `etime` has one-second granularity and the reads are not simultaneous, so
    #: the two ages differ by a second or so even for one unchanging process.
    #: Everything beyond that is a different process.
    AGE_DRIFT_TOLERANCE = 3

    #: tmux accepts the value joined to the flag (`-Lname`), so the separator is
    #: OPTIONAL. Requiring whitespace made such a server invisible to `classify`,
    #: and an invisible server means its live sidebars look orphaned and get
    #: reaped — the asymmetry that makes a missed match expensive rather than
    #: merely untidy. Anchored at a token boundary so `-L` inside a path or a
    #: quoted string cannot masquerade as the flag.
    #: The value is taken WHOLE (`\S+`), never up to the first character we happen
    #: to find convenient. Capturing `[A-Za-z0-9._-]+` truncated `-L a-name:with`
    #: to `a-name`, which is a DIFFERENT socket — so both reachability checks
    #: probed something else, found nothing, and condemned a healthy server.
    #: Short options group, so `-uLname` is the same flag as `-L name`; a server
    #: the classifier cannot see has no presence to protect its own sidebars.
    _SOCKET_FLAG = re.compile(r"(?<!\S)-[A-Za-z0-9]*L\s*(\S+)")
    #: The keep-alive embeds the flag INSIDE a shell loop, so its value is followed
    #: by `;` with no space — `\S+` would swallow the punctuation. The value is
    #: therefore read up to a shell boundary, and the boundary is REQUIRED: a name
    #: holding anything else does not match at all, so the shell is left
    #: unattributed and alive rather than attributed to a socket it is not on.
    _TMUX_SOCKET_FLAG = re.compile(
        r"(?<!\S)--tmux-socket[\s=]\s*([A-Za-z0-9._-]+)(?=[;\s]|$)")
    #: What a name has to look like before anything may be probed or killed on the
    #: strength of it. Everything else is a name we can recognise but not reason
    #: about, which protects rather than condemns.
    _SAFE_NAME = re.compile(r"\A[A-Za-z0-9._-]+\Z")
    #: `tmux -S <path>` names the socket by PATH and carries no `-L`, so a client
    #: matched on `-L` alone is invisible and its server loses the one protection
    #: that keeps a live screen alive. The basename identifies the FAMILY, which is
    #: enough to protect — but it is NOT a name anything here can probe, so a
    #: path-addressed process is never a candidate. See `classify`.
    _SOCKET_PATH_FLAG = re.compile(r"(?<!\S)-[A-Za-z0-9]*S\s*(\S+)")
    #: A standalone `-d`: what makes a `new-session` a SERVER start rather than a
    #: client that attaches. `attach -d` is excluded before this is consulted.
    _DETACHED_FLAG = re.compile(r"(?<!\S)-d(?!\S)")
    #: As TOKENS. Searching the whole command line for "attach" matched a server
    #: whose working directory was `/work/attachments`, which read as a client and
    #: hid a genuine orphan from both `server_names()` and the inventory. A
    #: session name or a pane command can do the same.
    _ATTACH_WORD = re.compile(r"(?<!\S)attach(-session)?(?!\S)")
    _NEW_SESSION_WORD = re.compile(r"(?<!\S)new-session(?!\S)")
    #: The keep-alive is a SHELL running a specific loop. Matching merely on the
    #: strings `hive_ide.sidebar` + `--tmux-socket` condemned anything that
    #: mentioned them — a diagnostic `grep`, or an agent process investigating
    #: this very code — so the executable and the loop's own shape both have to
    #: agree before anything is signalled.
    _SHELL_COMMANDS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "fish"})
    #: The keep-alive's own SIGNATURE, as one ordered string rather than a few
    #: loose words. Three substrings "somewhere in argv" matched a `grep` whose
    #: pattern happened to contain all three; this is the literal shape
    #: `Frame._sidebar_visible_probe` emits, so a false positive has to reproduce
    #: the probe verbatim. Kept in sync by
    #: `test_the_keepalive_signature_still_matches_what_frame_builds`, which
    #: compares it against the real builder — socket_gc cannot import frame,
    #: because frame imports socket_gc.
    _KEEPALIVE_SIGNATURE = (
        'out=$(tmux display-message -p -t "$TMUX_PANE" "#{window_active}"'
    )
    #: And it must actually RUN the sidebar, as a module.
    _KEEPALIVE_MODULE = "-m hive_ide.sidebar"
    #: And the loop has to BE the shell's command, not text the shell is carrying.
    #: A diagnostic that prints the whole loop as quoted data and then waits holds
    #: every substring above, and was condemned on that alone. `Frame` builds the
    #: command as `unset <names>; while :; do if <probe>; ...`, so the command
    #: must START that way — an `echo` wrapper starts with `echo`.
    _KEEPALIVE_SHAPE = re.compile(r"\Aunset [A-Za-z0-9_ ]+; while :; do if ")

    def __init__(
        self,
        *,
        rows: list[tuple[int, int, str]]
             | Callable[[], list[tuple[int, int, str]]]
             | None = None,
        kill: Callable[[int, int], None] | None = None,
        answers: Callable[[str], bool | None] | None = None,
        socket_root: Callable[[int], str | None] | None = None,
        our_root: Callable[[], str | None] | None = None,
        cmdline: Callable[[int], list[str] | None] | None = None,
        now: float | None = None,
    ):
        #: A list pins ONE snapshot; a CALLABLE is re-read on every access, which
        #: is how a test can make the process table change between selecting a
        #: candidate and signalling it — the pid-reuse window. Production passes
        #: neither and gets `_ps`.
        self._rows = rows
        self._kill = kill or os.kill
        #: Injectable so the unit suite stays hermetic: without it every shell
        #: candidate shells out to a real `tmux has-session`, which makes the
        #: tests depend on the machine's sockets rather than on their own rows.
        self._answers = answers or self.server_answers
        #: Where a given process's sockets live. Not derivable from `rows` — it
        #: comes from the process's own environment — so it is its own seam.
        self._socket_root = socket_root or self.socket_root_of
        #: Our own side of that comparison, injectable for the same reason: a
        #: suite that pinned only the other side would pass on a host with no
        #: `/proc` by protecting everything, which observes nothing.
        self._our_root = our_root or self.our_socket_root
        #: The lossless argv, for the same reason: it is read per process and is
        #: not derivable from the `ps` rows that `rows` injects.
        self._cmdline = cmdline or self.cmdline_of
        self.now = time.time() if now is None else now
        #: When the most recent process-table read happened, so the ages it
        #: returned can be aged forward rather than merely compared.
        self._selected_at: float | None = None

    # -- process table ----------------------------------------------------------

    def _snapshot(self) -> list[tuple[int, int, str]]:
        """A fresh read of the process table, honouring an injected source."""
        self._selected_at = time.monotonic()
        if self._rows is None:
            return self._ps()
        if callable(self._rows):
            return list(self._rows())
        return list(self._rows)

    @staticmethod
    def _ps() -> list[tuple[int, int, str]]:
        """(pid, age_seconds, argv) for THIS USER's processes only.

        `-a` means every user, so the uid is read and filtered here rather than
        trusted. Without that, a machine where this runs with elevated privileges
        could reap another user's healthy server: their socket lives in their own
        directory, so it answers nothing when we probe ours and looks abandoned.
        Running unprivileged it would merely fail with EPERM — which is luck, not
        a guard.

        `-o comm=` is avoided because it prints "tmux: server" WITH A SPACE, so a
        whitespace field split silently moves "server ..." into the argv column
        and the server stops being recognised at all. On macOS `pgrep -a` means
        "include ancestors" and prints no command line, so pgrep is out too.
        """
        try:
            result = subprocess.run(
                ["ps", "-axww", "-o", "pid=,uid=,etime=,args="],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return []
        mine = os.getuid()
        rows: list[tuple[int, int, str]] = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 3)
            if len(parts) != 4 or not parts[0].isdigit() or not parts[1].isdigit():
                continue
            if int(parts[1]) != mine:
                continue        # somebody else's process is never ours to signal
            rows.append((int(parts[0]), OrphanSweeper.etime_seconds(parts[2]), parts[3]))
        return rows

    #: tmux's own global options that CONSUME the next value. Everything else in
    #: a short-option group is a flag. Getting this list wrong makes `-f /x` eat
    #: the socket name or the reverse.
    _VALUE_OPTIONS = frozenset("LSfcT")

    @staticmethod
    def cmdline_of(pid: int) -> list[str] | None:
        """This process's REAL argv vector, or None when it cannot be read.

        `ps` renders a command line by joining the arguments with spaces and
        dropping the quoting, so a socket name or path containing a space comes
        back indistinguishable from two arguments — and no regex can recover the
        boundary that was thrown away. `/proc/<pid>/cmdline` is NUL-separated and
        keeps every boundary exactly.

        `ps` is still what ENUMERATES processes, because it is one call for the
        whole table and it is portable. This is consulted only where a wrong
        answer costs something irreversible.
        """
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        if not raw.strip(b"\0"):
            return None             # a kernel thread, or a process we cannot read
        return [token.decode("utf-8", "replace")
                for token in raw.rstrip(b"\0").split(b"\0")]

    @classmethod
    def socket_from_tokens(cls, tokens: list[str]) -> tuple[str | None, str | None]:
        """(socket name, socket path) as TMUX would read this command line.

        Parsed rather than matched, because the three things a regex kept getting
        wrong are all structural: a value may be joined to its flag (`-Lname`),
        short flags group (`-uLname`), and when a flag is REPEATED tmux takes the
        last one while a search takes the first. `-S` and `-L` set the same thing,
        so whichever comes last wins and clears the other.

        Only the global options are read — the scan stops at the tmux command,
        because everything after it belongs to the command, not to tmux.
        """
        name = path = None
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith("-") or token in ("-", "--"):
                break               # the command word: the global options end here
            if token.startswith("--"):
                break
            body = token[1:]
            position = 0
            while position < len(body):
                letter = body[position]
                if letter not in cls._VALUE_OPTIONS:
                    position += 1
                    continue        # a grouped flag such as the `u` in `-uLname`
                value = body[position + 1:]
                if not value:
                    index += 1
                    value = tokens[index] if index < len(tokens) else ""
                if letter == "L":
                    name, path = value, None
                elif letter == "S":
                    path, name = value, None
                break               # the rest of this token was the value
            index += 1
        return name, path

    @staticmethod
    def resolve_root(value: str | None) -> str:
        """The directory tmux puts sockets in for a given `TMUX_TMPDIR` value.

        Unset and empty both mean `/tmp`, and the path is resolved because one
        side of the comparison may reach it through a symlink.
        """
        try:
            return os.path.realpath(value or "/tmp")
        except OSError:
            return value or "/tmp"

    @staticmethod
    def mount_namespace_of(pid: int) -> str | None:
        """Which filesystem a path NAMES for this process, or None if unreadable.

        `/tmp` is not one directory. A process in a container, or under a systemd
        unit with `PrivateTmp`, has its own — same uid, same string, different
        filesystem. Comparing the two `TMUX_TMPDIR` strings then says "we looked
        where its socket is" about a directory we cannot even see into.
        """
        try:
            return f"mnt:{os.stat(f'/proc/{pid}/ns/mnt').st_ino}"
        except OSError:
            return None

    @classmethod
    def our_socket_root(cls) -> str | None:
        """The directory OUR probes look in, qualified by the filesystem it is on."""
        namespace = cls.mount_namespace_of(os.getpid())
        if namespace is None:
            return None
        return f"{namespace}\0{cls.resolve_root(os.environ.get('TMUX_TMPDIR'))}"

    @staticmethod
    def socket_root_of(pid: int) -> str | None:
        """Where THIS process's sockets live, or None when that cannot be read.

        Read from the process's own environment, because nothing in `ps` carries
        it and a `-L` name says nothing about which directory it resolves in.
        `/proc/<pid>/environ` is the environment the process was EXEC'd with —
        which is the one tmux read when it chose where to put the socket, and
        deliberately NOT this process's current `os.environ`.
        Only `/proc` is consulted: it is the platform this was built for and the
        one the 65-hour orphan was found on. Anywhere without it, this returns
        None — which protects, and leaves the server reap inert rather than
        guessing. The shell sweep is unaffected either way; see `sweep`.
        """
        namespace = OrphanSweeper.mount_namespace_of(pid)
        if namespace is None:
            return None
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return None
        root = OrphanSweeper.root_from_environ(raw)
        return None if root is None else f"{namespace}\0{root}"

    @staticmethod
    def root_from_environ(raw: bytes) -> str | None:
        """The same answer, from a block of environ bytes.

        Split out from the read so the three outcomes can be pinned exactly: a
        read of a live process races its own exec, so a test that reads once to
        set up and lets the reader read again is testing two different moments.
        """
        if not raw.strip(b"\0"):
            # An EMPTY read is not "the variable is unset" — every process has
            # some environment. It means we could not see this one's: the kernel
            # serves zero bytes for a process whose memory is not readable, and
            # for one that has been forked but has not exec'd yet (measured: 40
            # reads in that window, 40 empty). Answering "/tmp" there would be a
            # positive claim with nothing behind it, and since /tmp is the default
            # it would read as AGREEMENT and clear the candidate to be killed.
            return None
        for item in raw.split(b"\0"):
            if item.startswith(b"TMUX_TMPDIR="):
                value = item[len(b"TMUX_TMPDIR="):].decode("utf-8", "replace")
                if value and not value.startswith("/"):
                    # A RELATIVE root resolves against the working directory of
                    # the process that used it, which is not ours. Resolving it
                    # here answers with a directory this server may never have
                    # seen — and if the answer happens to land on ours, that reads
                    # as agreement and clears it to be killed.
                    return None
                return OrphanSweeper.resolve_root(value)
        return OrphanSweeper.resolve_root(None)

    def _probes_the_same_directory(self, pid: int) -> bool:
        """Would our probe have looked where THIS process's socket actually is?

        `server_answers` runs `tmux -L <name>`, which resolves inside the CALLER's
        socket directory. A healthy detached server under a different
        `TMUX_TMPDIR` answers nothing there and looks exactly like an orphan — and
        the process table, which is what SELECTED it, cannot tell the two apart
        because the directory is nowhere in its argv.

        So before a server may be called abandoned, its own environment has to
        agree it lives where we looked. Unreadable is not agreement.
        """
        theirs = self._socket_root(pid)
        ours = self._our_root()
        return theirs is not None and ours is not None and theirs == ours

    def _really_names_this_socket(self, pid: int, name: str) -> bool:
        """Does this process's REAL argv say it is on exactly this socket?

        The process table is a rendering: `ps` joins the arguments and drops the
        quoting, so a name holding a space arrives already truncated, and a
        repeated `-L` is resolved the opposite way round from how tmux resolves
        it. Both produce a name that is in our family, belongs to a different
        socket, and answers nothing when probed — which is the exact shape of an
        orphan. So before anything is killed, the lossless source has to agree.

        Unreadable is not agreement.
        """
        tokens = self._cmdline(pid)
        if tokens is None:
            return False
        parsed_name, parsed_path = self.socket_from_tokens(tokens)
        return parsed_path is None and parsed_name == name

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
                command = argv.split(None, 2)
                if (len(command) < 3 or command[1] != "-c"
                        or cls._KEEPALIVE_SHAPE.match(command[2]) is None
                        or cls._KEEPALIVE_SIGNATURE not in argv
                        or cls._KEEPALIVE_MODULE not in argv):
                    continue        # mentions the sidebar; is not the loop
                match = cls._TMUX_SOCKET_FLAG.search(argv)
                if (match and cls._SAFE_NAME.match(match.group(1))
                        and SocketReaper.ours(match.group(1))):
                    shells.setdefault(match.group(1), []).append((pid, age, argv))
                continue
            # EVERY token whose basename names one of our sockets makes this
            # process a protector of that socket. `ps` prints a command line with
            # the quoting removed, so `-S "/some dir/hive-ide-X" attach` arrives
            # as two tokens and the flag parse keeps only `/some` — the attached
            # client then vanished from classification entirely and its server,
            # whose socket file had been deleted, was reapable with a human still
            # looking at its panes. Scanning the tokens finds the name anyway. It
            # also settles `-L A -S /other/B`, where tmux uses B and we named A:
            # both are protected, so neither is reaped on the strength of a guess
            # about which flag won.
            protects = {
                candidate
                for candidate in (PurePosixPath(t).name for t in argv.split())
                if cls._SAFE_NAME.match(candidate) and SocketReaper.ours(candidate)
            }
            # A server's argv ends with the command it was started with, so a `-S`
            # anywhere in it may belong to the PANE's command rather than to tmux.
            # `-L` is therefore still what NAMES the socket.
            by_path = cls._SOCKET_PATH_FLAG.search(argv)
            # Not the first `-L`, and not the last either. tmux takes the last of
            # its OWN global options, but the pane command's flags are in the same
            # flattened line — `tmux -LA new-session -A -s work worker.py -LB`
            # attaches to A, and reading either end of the line gets it wrong one
            # way or the other. Two different values is simply a line we cannot
            # read, so it names nothing and both are protected.
            flag_names = set(cls._SOCKET_FLAG.findall(argv))
            if len(flag_names) == 1:
                name = next(iter(flag_names))
            elif not flag_names and by_path is not None:
                name = PurePosixPath(by_path.group(1)).name
            else:
                name = None
            protects |= {candidate for candidate in flag_names
                         if cls._SAFE_NAME.match(candidate)
                         and SocketReaper.ours(candidate)}
            # A `-S` anywhere forfeits CANDIDACY, and so does a name we cannot
            # vouch for. Every reachability probe runs `tmux -L <name>`, which
            # resolves inside the CALLER's socket directory: a healthy detached
            # server whose socket is somewhere else answers nothing there and
            # reads exactly like an orphan. Neither can be probed, so neither may
            # be killed.
            #
            # And only a DETACHED `new-session -d` is a server at all. `tmux -L X
            # new-session -A -s work` is a client that happens not to say
            # "attach", and calling it a server let `reap` kill a live client and
            # its panes. A server also retains the argv of whatever client first
            # started it, so an attaching form reads as a client — erring toward
            # keeping it. Everything unrecognised stays a protector, which is the
            # side that costs forks rather than somebody's screen.
            is_server = (
                name is not None
                and SocketReaper.ours(name)
                and by_path is None
                and cls._SAFE_NAME.match(name) is not None
                and not cls._ATTACH_WORD.search(argv)
                and cls._NEW_SESSION_WORD.search(argv) is not None
                and cls._DETACHED_FLAG.search(argv) is not None
            )
            if name is not None and cls._SAFE_NAME.match(name) and SocketReaper.ours(name):
                # The token scan cannot see a JOINED value: `-Lhive-ide-A` is one
                # token and its basename is the whole thing. The parsed name has
                # to join the protection set or an attached client written that
                # way contributes none at all.
                protects.add(name)
            if is_server:
                servers.setdefault(name, []).append((pid, age, argv))
                protects.discard(name)      # it does not protect itself
            for other in sorted(protects):
                clients.setdefault(other, []).append(pid)
        return servers, clients, shells

    # -- sweep ------------------------------------------------------------------

    def sweep(self, *, keep: set[str] | None = None, apply: bool = False) -> dict:
        """Reap keep-alive shells whose server process is GONE. Servers: never.

        Takes no reachability argument on purpose: it establishes its own, and it
        needs TWO INDEPENDENT NEGATIVES before it will signal anything, because
        each covers the other's blind spot.

        The process table alone is not enough. tmux's option surface is richer than
        any regex — `-uLname` groups short options, `$TMUX` names the socket with
        no flag at all — so a server we merely failed to PARSE reads as a server
        that does not exist, and the live frame's shells get killed.

        A socket probe alone is not enough either. It answers for OUR namespace, so
        a server under a different `TMUX_TMPDIR` looks absent to it — while that
        server is plainly there in the process table. This is why the shell path
        needs no socket-directory evidence of its own, and the SERVER path does:
        here the table is the protection, there the table is what selects.

        So a shell is reaped only where the table and tmux AGREE it has no server.
        Anything inconclusive on either side keeps it: failing to clean up costs a
        few forks, and getting it wrong costs somebody their sidebars.
        """
        protected = set(keep or ())
        rows = self._snapshot()
        servers, clients, shells = self.classify(rows)

        doomed: list[tuple[str, int, int, str]] = []
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
            # The second, independent negative — see this method's docstring.
            if self._answers(name) is not False:
                continue
            for pid, age, argv in entries:
                if age >= self.SHELL_MIN_AGE_SECONDS:
                    doomed.append((name, pid, age, argv))

        doomed = doomed[: self.MAX_KILLS]
        killed, spared = (self._terminate(doomed, confirm=self._still_unreachable)
                          if apply else ([], []))
        return {
            "servers": [],
            "shells": [pid for _n, pid, _age, _a in doomed],
            "sockets": sorted({name for name, _pid, _age, _a in doomed}),
            "applied": apply,
            "killed": killed,
            "spared": spared,
        }

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

        A server only reaches this list if our probe looked in the directory its
        socket is actually in — see `_probes_the_same_directory`. That filter sits
        here rather than at the kill so the REPORT is right too: calling a healthy
        server an orphan in `verify` is its own kind of wrong, since a person then
        runs `reap --apply` on our word.
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
            if len(entries) > 1:
                # One socket has ONE server. Two processes reading as servers on
                # the same name is a contradiction, and the way it arises is a
                # client misread: `new-session -A -s work python3 -d app.py`
                # attaches, but the PANE command's `-d` is in the same flattened
                # line as tmux's own flags, so the client scored as detached. It
                # then stopped protecting the socket AND became a candidate
                # itself. Treat the contradiction as unknown and keep both.
                continue
            for pid, age, argv in entries:
                if age < self.SERVER_MIN_AGE_SECONDS:
                    continue
                if not self._really_names_this_socket(pid, name):
                    continue        # the real argv does not say what `ps` said
                if not self._probes_the_same_directory(pid):
                    continue        # its socket is not where we looked
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
        # `_terminate` re-probes each candidate in the instant before it signals it.
        # It cannot be done in a batch here: `unreachable` was collected by a walk
        # that probes one socket at a time, each call with its own ten-second
        # timeout, and a tmux server recreates its socket on SIGUSR1. Filtering the
        # whole list up front leaves every entry but the last holding an answer that
        # is already stale by the time the signal goes out, and a server that came
        # back during a later candidate's probe still carries its original pid and
        # argv — so the identity check waves it straight through.
        killed, spared = (self._terminate(found, confirm=self._still_unreachable)
                          if apply else ([], []))
        return {
            "servers": [pid for _n, pid, _age, _a in found],
            "shells": [],
            "sockets": sorted({name for name, _p, _age, _a in found}),
            "applied": apply,
            "killed": killed,
            "spared": spared,
        }

    # -- killing ----------------------------------------------------------------

    def _still_unreachable(self, name: str) -> bool:
        """Does tmux STILL positively say there is no server on this socket?"""
        return self._answers(name) is False

    def _unchanged(self, pid: int, age: int, argv: str, since: float) -> bool:
        """Is this pid STILL the process that was selected? Unknown answers no.

        A pid proves nothing by itself — it is recycled, and on a busy machine it
        is recycled quickly. argv barely helps on its own either: the keep-alive is
        spawned from one fixed command line, so a FRESH one wears a dead one's argv
        byte for byte and walks straight through an argv-only comparison.

        What separates them is WHEN each started, and that is not the same thing as
        "is it older than the one I picked". Requiring only that the age had not
        gone backwards let a replacement through whenever enough time passed: pick
        a five-minute-old shell, spend ten minutes probing sockets one at a time,
        and a replacement spawned a minute later now reports eight minutes — older
        than the five we recorded, and wearing the same command line, so it was
        signalled while its own live frame was still running.

        So the age is checked against what it SHOULD be by now: the age at
        selection plus the time actually spent since. One process tracks that
        exactly; anything that started at a different moment does not.
        """
        expected = age + (time.monotonic() - since)
        for other, other_age, other_argv in self._snapshot():
            if other != pid:
                continue
            if other_argv != argv:
                return False
            return abs(other_age - expected) <= self.AGE_DRIFT_TOLERANCE
        return False        # absent from the table, or the table could not be read

    def _terminate(
        self,
        candidates: list[tuple[str, int, int, str]],
        *,
        confirm: Callable[[str], bool] | None = None,
    ) -> list[int]:
        """SIGTERM, grace, then SIGKILL — re-checking the candidate before EACH signal.

        Returns (killed, spared): the pids that are gone or were SIGKILLed, and
        the pids that took a SIGTERM and were then deliberately left running.

        BOTH signals get both checks. Two things can change between choosing a
        candidate and signalling it, and the gap is not small: `confirm` runs a
        tmux probe per socket, each with its own ten-second timeout, and the grace
        window is another such gap.

        The SOCKET can come back. A tmux server recreates its file on SIGUSR1, so a
        name that answered "no server" when the list was built may be serving panes
        by the time its turn arrives. `confirm` is therefore asked HERE, one
        candidate at a time — never once over the whole list, which would leave
        every entry but the last acting on a stale answer.

        The PROCESS can be a different one. `kill(pid, 0)` proves only that the
        number is live, and argv alone is not identity for a keep-alive spawned
        from a fixed command line — see `_unchanged`, which judges on both.

        Each check is made as late as it can be: the probe first, then the cheaper
        process-table read, then the signal on the very next line.
        """
        was = {pid: (age, argv) for _n, pid, age, argv in candidates}
        #: When the ages in `candidates` were read. Every later check measures
        #: against this, so a long walk cannot make a replacement look old enough.
        since = self._selected_at if self._selected_at is not None else time.monotonic()
        signalled: list[tuple[str, int]] = []
        for name, pid, age, expected in candidates:
            if confirm is not None and not confirm(name):
                continue        # it answers again: it is not an orphan any more
            if not self._unchanged(pid, age, expected, since):
                continue        # exited, or the pid now belongs to something else
            try:
                self._kill(pid, signal.SIGTERM)
                signalled.append((name, pid))
            except OSError:
                continue
        if not signalled:
            return [], []
        spared: list[int] = []
        time.sleep(self.TERM_GRACE_SECONDS)
        for name, pid in signalled:
            try:
                self._kill(pid, 0)
            except OSError:
                continue            # gone: SIGTERM was enough
            # BOTH checks again, not just identity. A server that ignored the
            # SIGTERM and recreated its socket during the grace window — or while
            # the other candidates were being probed — is serving panes again, and
            # its pid and argv never changed, so identity alone waves it through
            # to the one signal it cannot refuse.
            if confirm is not None and not confirm(name):
                spared.append(pid)
                continue
            age, expected = was[pid]
            if not self._unchanged(pid, age, expected, since):
                spared.append(pid)
                continue            # different process, or unreadable — leave it
            try:
                self._kill(pid, signal.SIGKILL)
            except OSError:
                spared.append(pid)
                continue
        # A process that ignored the SIGTERM and was then deliberately left alone
        # is STILL RUNNING. Reporting it as killed tells a person the reaper did
        # something it did not do — and the report is the only thing they have to
        # judge it by, since they cannot see the signals.
        return [pid for _name, pid in signalled if pid not in spared], spared
