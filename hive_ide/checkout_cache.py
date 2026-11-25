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
import math
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
            try:
                # A JSON integer can be arbitrarily large, and `float()` raises
                # OverflowError on one that cannot be represented — from ANY row,
                # which would otherwise lose the whole file (or the whole paint).
                observed_at = float(at)
            except (OverflowError, ValueError):
                continue
            # Python's JSON decoder accepts `NaN`, which compares False against
            # every bound: it would pass the future check, never look expired, and
            # pin a stale icon for good.
            if not math.isfinite(observed_at):
                continue
            if observed_at > moment + cls.FUTURE_SKEW_SECONDS:
                continue
            observations[key] = CheckoutObservation(state=state, at=observed_at)
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
        age = moment - found.at
        # Checked at lookup, not only at parse: a long-lived reader keeps its
        # parsed entries while the file is unchanged, so a clock stepped BACKWARD
        # would otherwise make a stale `shipped` look brand new for ever.
        if age < -cls.FUTURE_SKEW_SECONDS:
            return None
        if age > cls.MAX_REUSE_SECONDS:
            return None
        return found

    @staticmethod
    def key(working_dir: str | Path) -> str:
        """One key per checkout, so two sessions sharing a directory share a probe.

        Never raises. `Path("~someone-who-no-longer-exists/w").expanduser()` raises
        `RuntimeError`, and this runs on the render thread, outside the per-row
        handler that used to swallow exactly that for one row. An unnormalisable
        path is keyed as it stands: the painter and the refresher still agree, so
        the row simply never has an observation — a blank slot, not a lost frame.
        """
        text = str(working_dir)
        try:
            return str(Path(text).expanduser())
        except (RuntimeError, OSError, ValueError):
            return text

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
    #: Re-probe at this age, BELOW the cutoff at which a paint stops drawing the
    #: observation. Waiting for expiry made a healthy icon blink: a pass at 4.9 s
    #: skipped the row, a paint at 5.1 s drew nothing, and only the pass at 5.9 s
    #: started `git`. The gap is the slack a probe has to finish in.
    REFRESH_AFTER_SECONDS = 3.0
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
        #: When each directory was last ATTEMPTED, including failures. In memory
        #: only: a failed probe is not an observation and has no business in a file
        #: other painters read.
        self._attempted: dict[str, float] = {}
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
        """One bounded pass. Returns what it published, for a test to assert on.

        The targets and the file are re-read before EVERY probe, not snapshotted
        for the pass: a scroll, a filter or the archive view changes what the frame
        draws mid-pass, and the rows that just became visible must not wait behind
        seven probes for rows nobody can see any more. It also picks up another
        renderer's publish during the brief overlap of a window switch.
        """
        observed: dict[str, CheckoutObservation] = {}
        for _ in range(self.MAX_PROBES_PER_PASS):
            if self._stop.is_set():
                break
            moment = time.time() if now is None else now
            with self._guard:
                directories = self._directories
            if not directories:
                break
            known = CheckoutCache.read(self.state_home, self.workspace_key, now=moment)
            due = [
                directory
                for directory in directories
                if self._is_due(known, directory, moment)
            ]
            if not due:
                break
            due.sort(key=lambda directory: self._last_look(known, directory))
            directory = due[0]
            key = CheckoutCache.key(directory)
            # Before the probe, so a directory that FAILS still counts as looked at
            # and rotates behind the untried ones. Without this, eight unreadable
            # checkouts keep first priority for ever and a healthy ninth starves.
            self._attempted[key] = time.time()
            try:
                status = self._probe(directory)
            except Exception:  # noqa: BLE001 — a refresh pass never raises into a frame
                continue
            state = getattr(status, "state", None) if status is not None else None
            fresh = CheckoutObservation(state=state, at=time.time())
            observed[key] = fresh
            # Published one at a time, NOT as a batch at the end of the pass: eight
            # one-second probes against a 5 s reuse age would publish the first
            # results already expired, so a healthy checkout would stay blank
            # because unrelated repositories were slow. It also puts each icon on
            # screen a tick earlier.
            CheckoutCache.publish(self.state_home, self.workspace_key, {key: fresh})
        return observed

    def _is_due(
        self,
        known: dict[str, CheckoutObservation],
        directory: str | Path,
        moment: float,
    ) -> bool:
        """Whether this checkout is worth a probe right now.

        Two independent brakes. The observation must be older than the refresh
        age — below the display cutoff, so a replacement lands before a paint
        stops drawing the old one. And an ATTEMPT inside the same window is
        enough to wait, even when it published nothing: `publish` swallows a
        write error by design, so a read-only state directory would otherwise
        re-probe the same checkout on every pass for ever.
        """
        key = CheckoutCache.key(directory)
        attempted = self._attempted.get(key)
        if attempted is not None and moment - attempted < self.REFRESH_AFTER_SECONDS:
            return False
        found = known.get(key)
        if found is None:
            return True
        return moment - found.at >= self.REFRESH_AFTER_SECONDS

    def _last_look(
        self, known: dict[str, CheckoutObservation], directory: str | Path
    ) -> float:
        """The later of "observed" and "attempted" — oldest look goes first."""
        return max(_age_of(known, directory), self._attempted.get(
            CheckoutCache.key(directory), float("-inf")
        ))

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
