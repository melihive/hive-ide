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
import socket
import stat
import time
from pathlib import Path


class SocketReaper:
    """Sweep dead tmux sockets this package left behind."""

    PREFIX = "hive-ide-"
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

    @staticmethod
    def default_directory() -> Path:
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

    def _is_reapable(self, entry: Path, keep: set[str]) -> bool:
        if not entry.name.startswith(self.PREFIX) or entry.name in keep:
            return False
        try:
            info = entry.lstat()
        except OSError:
            return False
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return False
        if self.now - info.st_mtime < self.MIN_AGE_SECONDS:
            return False
        return self.is_listening(entry) is False

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
            if not self._is_reapable(entry, kept):
                continue
            dead.append(entry.name)
            if not apply:
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
