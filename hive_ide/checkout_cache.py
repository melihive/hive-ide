"""Workspace-scoped cache of checkout observations, so that no PAINT forks git.

`CheckoutProvider` decorates every sidebar row with the state of its checkout, and
`git_status.inspect_linked_checkout` answers that with up to SIX `git` invocations
per row, each allowed two seconds. Measured on a 14-session frame over 11 distinct
checkouts, one frame cost 116 ms of `git` — and a window switch rendered that same
frame three times, in three processes, so the switch paid it three times. The 5 s
cache the provider already had could not help: it lived on the provider INSTANCE,
and `canonical_frame` builds a fresh registry on every call.

So the observation is cached per WORKSPACE, on disk, and nothing on a paint path
probes. `nav`'s pre-paint and the `session-window-changed` hook are READERS; only a
VISIBLE renderer refreshes, in its own thread, off the first paint and off the input
loop as well — that loop draws before it reads keys, so a probe there would delay a
keystroke by the length of a `git status`.

The file is deliberately NOT a protocol document. It is disposable, it carries its
own version in its NAME so an older package simply never reads a newer one, and
every way of being unusable — absent, truncated, corrupt, an unknown version, an
entry of the wrong shape, a timestamp from the future — is a MISS.

**A miss is not an observation.** The caller draws nothing rather than falling back
to a session record's `worktree_ahead`, which would paint `shipped` over a checkout
that has unmerged work. An observation INSIDE the reuse age is drawn as it stands,
including `shipped` (the user's decision, 2026-11-25): a five-second-old icon is
wrong far less often than a blinking empty column is distracting. It is evidence of
what was observed, never a licence to skip `git status` before acting.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .git_status import inspect_linked_checkout
from .store import StateStore


@dataclass(frozen=True)
class CheckoutObservation:
    """What a probe saw, and when.

    `state` is the probe's verdict, or None for "observed, and not a linked
    checkout" — an ordinary directory or an ordinary repo. That is a real
    observation and must stay distinguishable from a cache miss.
    """

    state: str | None
    at: float


class CheckoutCache:
    """The disposable per-workspace file, outside the protocol collections."""

    #: In the NAME, so a package that does not know this shape never reads it.
    FILENAME = "checkout-cache-v1.json"
    VERSION = 1
    #: Maximum age at which an observation may still be reused. Taken from the
    #: observation itself and never extended on read, nor by a later batch publish
    #: that happens to rewrite the file.
    MAX_REUSE_SECONDS = 5.0
    #: A clock that jumped forward is not evidence. Anything claiming to be from
    #: the future by more than this is dropped rather than trusted for 5 s.
    FUTURE_SKEW_SECONDS = 2.0
    #: Bound on the file. A workspace with more sessions than this keeps the newest.
    MAX_ENTRIES = 256

    @classmethod
    def path(cls, state_home: str | Path, workspace_key: str) -> Path:
        return StateStore(state_home, workspace_key).workspace_dir / cls.FILENAME

    @classmethod
    def read(
        cls, state_home: str | Path, workspace_key: str, *, now: float | None = None
    ) -> dict[str, CheckoutObservation]:
        """Every entry this package can trust. Anything else is simply absent."""
        moment = time.time() if now is None else now
        try:
            raw = cls.path(state_home, workspace_key).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return {}
        try:
            document = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        if not isinstance(document, dict) or document.get("version") != cls.VERSION:
            return {}
        entries = document.get("checkouts")
        if not isinstance(entries, dict):
            return {}
        observations: dict[str, CheckoutObservation] = {}
        for key, entry in entries.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            state = entry.get("state")
            if state is not None and not isinstance(state, str):
                continue
            at = entry.get("at")
            if not isinstance(at, (int, float)) or isinstance(at, bool):
                continue
            if at > moment + cls.FUTURE_SKEW_SECONDS:
                continue
            observations[key] = CheckoutObservation(state=state, at=float(at))
        return observations

    @classmethod
    def fresh(
        cls,
        observations: dict[str, CheckoutObservation],
        working_dir: str | Path,
        *,
        now: float | None = None,
    ) -> CheckoutObservation | None:
        """The observation for one checkout, or None for a miss OR an expiry.

        Both answer "no observation", and the caller must treat them the same way:
        draw nothing. Only the refresher cares which it was.
        """
        moment = time.time() if now is None else now
        found = observations.get(cls.key(working_dir))
        if found is None:
            return None
        if moment - found.at > cls.MAX_REUSE_SECONDS:
            return None
        return found

    @staticmethod
    def key(working_dir: str | Path) -> str:
        """One key per checkout, so two sessions sharing a directory share a probe."""
        return str(Path(working_dir).expanduser())

    @classmethod
    def publish(
        cls,
        state_home: str | Path,
        workspace_key: str,
        observations: dict[str, CheckoutObservation],
    ) -> None:
        """Merge these observations into the file, newest per key winning.

        Read-modify-write with an atomic replace, and no lock: refreshes happen only
        in a VISIBLE renderer, and a tmux session has exactly one current window, so
        there is at most one refresher per workspace outside the brief overlap of a
        switch. A duplicate probe there is idempotent and a lost update is re-probed
        within `MAX_REUSE_SECONDS`, which is a smaller price than a lock file.
        """
        if not observations:
            return
        merged = dict(cls.read(state_home, workspace_key))
        for key, fresh in observations.items():
            known = merged.get(key)
            if known is None or fresh.at >= known.at:
                merged[key] = fresh
        if len(merged) > cls.MAX_ENTRIES:
            newest = sorted(merged.items(), key=lambda item: item[1].at, reverse=True)
            merged = dict(newest[: cls.MAX_ENTRIES])
        document = {
            "version": cls.VERSION,
            "checkouts": {
                key: {"state": value.state, "at": value.at}
                for key, value in merged.items()
            },
        }
        target = cls.path(state_home, workspace_key)
        payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.stem}.", suffix=".tmp"
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as writer:
                    writer.write(payload)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.replace(temporary, target)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except OSError:
            # A cache that cannot be written is a cache that misses. Never an
            # exception out of a refresh pass, and never out of a paint.
            return


class CheckoutCacheReader:
    """A paint's view of the file: re-read only when the file has actually changed.

    One `stat` per paint instead of one parse per row, and no probe ever. A provider
    instance in the render loop lives for thousands of frames, so it has to notice a
    refresher's publish; a provider instance inside `canonical_frame` lives for one
    frame, so it must not pay for more than a single read.
    """

    def __init__(self) -> None:
        self._stamps: dict[str, tuple[int, int]] = {}
        self._entries: dict[str, dict[str, CheckoutObservation]] = {}

    def entries(
        self, state_home: str | Path, workspace_key: str
    ) -> dict[str, CheckoutObservation]:
        path = CheckoutCache.path(state_home, workspace_key)
        location = str(path)
        try:
            status = path.stat()
            stamp = (status.st_mtime_ns, status.st_size)
        except OSError:
            self._stamps.pop(location, None)
            self._entries.pop(location, None)
            return {}
        if self._stamps.get(location) != stamp:
            self._entries[location] = CheckoutCache.read(state_home, workspace_key)
            self._stamps[location] = stamp
        return self._entries.get(location, {})

    def fresh(
        self,
        state_home: str | Path,
        workspace_key: str,
        working_dir: str | Path,
        *,
        now: float | None = None,
    ) -> CheckoutObservation | None:
        return CheckoutCache.fresh(
            self.entries(state_home, workspace_key), working_dir, now=now
        )


class CheckoutRefresher:
    """The only thing that probes: one bounded pass at a time, in its own thread.

    Started by a renderer that is VISIBLE, stopped when it goes hidden or exits to
    reload. The renderer hands it the directories the frame lists each tick, so the
    thread never reads the registry itself and never decides what matters.
    """

    INTERVAL_SECONDS = 1.0
    #: Probes per pass, oldest observation first. A workspace with fifty sessions
    #: must not fork three hundred `git` processes in one burst; at this bound a
    #: 14-checkout frame is fully refreshed inside two passes.
    MAX_PROBES_PER_PASS = 8

    def __init__(
        self,
        state_home: str | Path,
        workspace_key: str,
        *,
        probe: Callable[[str], object] | None = None,
        interval: float | None = None,
    ) -> None:
        self.state_home = state_home
        self.workspace_key = workspace_key
        self._probe = probe or inspect_linked_checkout
        self._interval = self.INTERVAL_SECONDS if interval is None else interval
        self._directories: tuple[str, ...] = ()
        self._guard = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def note_directories(self, directories: Iterable[str | Path]) -> None:
        """What the frame currently lists. Cheap enough to call every tick."""
        seen: list[str] = []
        for directory in directories:
            if not directory:
                continue
            key = CheckoutCache.key(directory)
            if key not in seen:
                seen.append(key)
        with self._guard:
            self._directories = tuple(seen)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hive-ide-checkout-refresh", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 0.2) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)

    def run_once(self, *, now: float | None = None) -> dict[str, CheckoutObservation]:
        """One bounded pass. Returns what it published, for a test to assert on."""
        with self._guard:
            directories = self._directories
        if not directories:
            return {}
        known = CheckoutCache.read(self.state_home, self.workspace_key, now=now)
        stale = [
            directory
            for directory in directories
            if CheckoutCache.fresh(known, directory, now=now) is None
        ]
        stale.sort(key=lambda directory: _age_of(known, directory))
        observed: dict[str, CheckoutObservation] = {}
        for directory in stale[: self.MAX_PROBES_PER_PASS]:
            if self._stop.is_set():
                break
            try:
                status = self._probe(directory)
            except Exception:  # noqa: BLE001 — a refresh pass never raises into a frame
                continue
            state = getattr(status, "state", None) if status is not None else None
            observed[CheckoutCache.key(directory)] = CheckoutObservation(
                state=state, at=time.time()
            )
        if observed:
            CheckoutCache.publish(self.state_home, self.workspace_key, observed)
        return observed

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 — the thread outlives any one pass
                pass
            self._stop.wait(self._interval)


def _age_of(known: dict[str, CheckoutObservation], directory: str | Path) -> float:
    found = known.get(CheckoutCache.key(directory))
    return found.at if found is not None else float("-inf")
