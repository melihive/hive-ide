"""Protocol-v1 JSON state store."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import SCHEMA_VERSION
from .agents import AgentResumeState
from .errors import SchemaVersionError, StateError, UsageError
from .paths import workspace_hash


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Atomic, workspace-scoped protocol state."""

    COLLECTIONS = frozenset(
        {"sessions", "archive", "status", "activity", "errors", "repairs"}
    )

    def __init__(self, home: str | Path, workspace_key: str):
        self.home = Path(home).expanduser().resolve()
        self.workspace_key = str(Path(workspace_key).expanduser().resolve())
        self.workspace_hash = workspace_hash(self.workspace_key)
        self.workspace_dir = self.home / "workspaces" / self.workspace_hash

    def collection(self, name: str) -> Path:
        if name not in self.COLLECTIONS:
            raise ValueError(f"Unknown state collection: {name}")
        return self.workspace_dir / name

    def path(self, collection: str, session_id: str) -> Path:
        return self.collection(collection) / f"{session_id}.json"

    def frame_error_path(self) -> Path:
        return self.workspace_dir / "frame-error.json"

    def config_snapshot_path(self) -> Path:
        return self.workspace_dir / "config.json"

    # Owner-aware, reentrant, per-path lock state for this process. `flock` locks
    # belong to an open file description, so a nested `open() + LOCK_EX` from the
    # thread that already holds the lock would block on itself; the owning thread
    # therefore nests by depth, every OTHER thread waits here until the owner has
    # released, and only then takes the real flock. The table is reset after a
    # fork: inherited bookkeeping describes the parent's threads, not ours.
    _LOCKS: dict[str, dict[str, Any]] = {}
    _LOCKS_GUARD = threading.Lock()
    _LOCKS_PID = os.getpid()

    @classmethod
    def _lock_state(cls, key: str) -> dict[str, Any]:
        with cls._LOCKS_GUARD:
            if cls._LOCKS_PID != os.getpid():
                cls._LOCKS = {}
                cls._LOCKS_PID = os.getpid()
            state = cls._LOCKS.get(key)
            if state is None:
                state = {
                    "owner": None,
                    "depth": 0,
                    "cond": threading.Condition(cls._LOCKS_GUARD),
                }
                cls._LOCKS[key] = state
            return state

    @contextmanager
    def mutation_lock(self, *, blocking: bool = True):
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        path = self.workspace_dir / ".mutation.lock"
        state = self._lock_state(str(path))
        cond: threading.Condition = state["cond"]
        me = (os.getpid(), threading.get_ident())
        with cond:
            if state["owner"] == me:
                state["depth"] += 1
                nested = True
            else:
                if not blocking and state["owner"] is not None:
                    raise StateError(
                        f"Cannot lock workspace state {path}: held by another thread"
                    )
                while state["owner"] is not None:
                    cond.wait()
                # Claim ownership before the flock so other threads queue here.
                state["owner"] = me
                state["depth"] = 1
                nested = False
        if nested:
            try:
                yield
            finally:
                with cond:
                    if state["owner"] == me and state["depth"] > 1:
                        state["depth"] -= 1
            return
        handle = None
        try:
            handle = path.open("a+", encoding="utf-8")
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(handle.fileno(), flags)
        except OSError as exc:
            if handle is not None:
                handle.close()
            with cond:
                state["owner"] = None
                state["depth"] = 0
                cond.notify_all()
            raise StateError(f"Cannot lock workspace state {path}: {exc}") from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
            finally:
                with cond:
                    state["owner"] = None
                    state["depth"] = 0
                    cond.notify_all()

    def mutation_lock_held(self) -> bool:
        """Is THIS thread inside `mutation_lock` for this workspace right now?"""
        state = self._lock_state(str(self.workspace_dir / ".mutation.lock"))
        with StateStore._LOCKS_GUARD:
            return state["owner"] == (os.getpid(), threading.get_ident())

    @staticmethod
    def new_session_id() -> str:
        return uuid.uuid4().hex

    def _validate(self, data: Any, path: Path) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise StateError(f"State document is not an object: {path}")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Unsupported schema_version {version!r} in {path}; expected {SCHEMA_VERSION}."
            )
        key = data.get("workspace_key")
        if key != self.workspace_key:
            raise StateError(f"Workspace identity mismatch in {path}.")
        return data

    def read_path(self, path: Path) -> dict[str, Any] | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateError(f"Cannot read state document {path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateError(f"Invalid JSON in state document {path}: {exc}") from exc
        return self._validate(data, path)

    def read(self, collection: str, session_id: str) -> dict[str, Any] | None:
        return self.read_path(self.path(collection, session_id))

    def write_path(self, path: Path, data: dict[str, Any]) -> Path:
        outgoing = dict(data)
        outgoing.setdefault("schema_version", SCHEMA_VERSION)
        outgoing.setdefault("workspace_key", self.workspace_key)
        self._drop_dead_legacy_plan(outgoing)
        self._validate(outgoing, path)
        payload = json.dumps(outgoing, indent=2, sort_keys=True) + "\n"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                try:
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        except OSError as exc:
            raise StateError(f"Cannot write state document {path}: {exc}") from exc
        return path

    @staticmethod
    def _drop_dead_legacy_plan(record: dict[str, Any]) -> bool:
        host = record.get("host")
        if not isinstance(host, dict):
            return False
        hive = host.get("hive")
        if not isinstance(hive, dict):
            return False
        legacy = hive.get("legacy_record")
        if not isinstance(legacy, dict) or "plan" not in legacy:
            return False
        legacy.pop("plan", None)
        return True

    def write(self, collection: str, session_id: str, data: dict[str, Any]) -> Path:
        return self.write_path(self.path(collection, session_id), data)

    def prune_dead_legacy_plan(
        self, *, collections: tuple[str, ...] = ("sessions", "archive")
    ) -> list[dict[str, str]]:
        pruned: list[dict[str, str]] = []
        for collection in collections:
            directory = self.collection(collection)
            for path in sorted(directory.glob("*.json")):
                record = self.read_path(path)
                if record is None:
                    continue
                if self._drop_dead_legacy_plan(record):
                    self.write_path(path, record)
                    pruned.append(
                        {
                            "collection": collection,
                            "session_id": str(record.get("id") or path.stem),
                        }
                    )
        return pruned

    def delete(self, collection: str, session_id: str) -> bool:
        try:
            self.path(collection, session_id).unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StateError(f"Cannot remove {collection} state for {session_id}: {exc}") from exc

    def list(self, collection: str) -> list[dict[str, Any]]:
        directory = self.collection(collection)
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError as exc:
            raise StateError(f"Cannot list state directory {directory}: {exc}") from exc
        records = [record for path in paths if (record := self.read_path(path)) is not None]
        records.sort(key=lambda item: (item.get("name") or "").casefold())
        records.sort(key=lambda item: item.get("last_active") or "", reverse=True)
        if collection == "sessions":
            records.sort(
                key=lambda item: (item.get("sleep") or {}).get("state") == "sleeping"
            )
        return records

    def refresh_stable_sources(self, *, collections: tuple[str, ...] = ("sessions",)) -> dict[str, Any]:
        """Best-effort stable source metadata repair.

        Stable package patch upgrades should not leave session records with stale
        version pins. This updates JSON metadata only; it never rebuilds tmux panes
        or touches driver state. Dev and explicit sources stay strict elsewhere.
        """
        from .source import inspect_interpreter

        refreshed: list[str] = []
        skipped: dict[str, str] = {}
        handshakes: dict[str, dict[str, Any] | None] = {}
        try:
            with self.mutation_lock(blocking=False):
                for collection in collections:
                    for record in self.list(collection):
                        source = record.get("source") or {}
                        if source.get("kind") != "stable":
                            continue
                        interpreter = source.get("interpreter")
                        if not isinstance(interpreter, str) or not interpreter:
                            continue
                        if interpreter not in handshakes:
                            try:
                                handshakes[interpreter] = inspect_interpreter(interpreter)
                            except Exception as exc:  # fail-open: listing/opening must survive
                                handshakes[interpreter] = None
                                skipped[interpreter] = str(exc)
                        handshake = handshakes.get(interpreter)
                        if not handshake:
                            continue
                        version = handshake.get("package_version")
                        if not isinstance(version, str) or version == source.get("version"):
                            continue
                        record["source"] = {**source, "version": version}
                        self.write(collection, record["id"], record)
                        refreshed.append(record["id"])
        except StateError as exc:
            return {"refreshed": [], "skipped": {"state": str(exc)}}
        return {"refreshed": refreshed, "skipped": skipped}

    def find_session(self, session_id: str, *, archived: bool = False) -> dict[str, Any] | None:
        return self.read("archive" if archived else "sessions", session_id)

    def find_by_name(self, name: str) -> dict[str, Any] | None:
        matches = [item for item in self.list("sessions") if item.get("name") == name]
        if len(matches) > 1:
            raise StateError(f"Multiple sessions have the display name {name!r}.")
        return matches[0] if matches else None

    def find_conversation_owner(
        self,
        *,
        driver_id: str,
        reference: str,
        exclude_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the active session that already owns a driver conversation.

        Conversation IDs are driver-local identity. Two active IDE sessions
        pointing at the same driver/ref pair means one sidebar item can resume
        another session's chat, so callers must treat an owner as a conflict.
        Archived sessions are excluded so users can intentionally adopt an old
        conversation after archiving its IDE wrapper.

        Every workspace under the state home is searched, this one first:
        conversation ids are global to the driver, so an owner in another
        workspace is just as much a conflict.
        """
        if not driver_id or not reference:
            return None
        for record in self._sessions_in_every_workspace():
            if record.get("id") == exclude_session_id:
                continue
            driver = record.get("driver") if isinstance(record, dict) else None
            resume = driver.get("resume") if isinstance(driver, dict) else None
            if (
                isinstance(driver, dict)
                and driver.get("id") == driver_id
                and isinstance(resume, dict)
                and resume.get("reference") == reference
            ):
                return record
            agents = record.get("agents")
            resume_ids = agents.get("resume_ids") if isinstance(agents, dict) else None
            if isinstance(resume_ids, dict) and resume_ids.get(driver_id) == reference:
                return record
        return None

    def _sessions_in_every_workspace(self) -> Iterator[dict[str, Any]]:
        yield from self._records_in_every_workspace("sessions")

    def _records_in_every_workspace(self, collection: str) -> Iterator[dict[str, Any]]:
        yield from self.list(collection)
        try:
            others = sorted(
                path
                for path in (self.home / "workspaces").iterdir()
                if path.is_dir() and path.name != self.workspace_hash
            )
        except OSError:
            return
        for workspace_dir in others:
            try:
                paths = sorted((workspace_dir / collection).glob("*.json"))
            except OSError:
                continue
            for path in paths:
                record = self._read_foreign_session(path)
                if record is not None:
                    yield record

    def conversation_references(self, *, driver_id: str) -> set[str]:
        """Every conversation a wrapper already references, in any workspace.

        Adoption asks "is this conversation already wrapped?", which is the same
        question `find_conversation_owner` asks and must be answered over the same
        ground: conversation ids are global to the driver, so a wrapper in another
        workspace counts, and a PARKED reference counts too — a session that has
        switched driver still owns the conversation it switched away from.

        Unlike `find_conversation_owner` this includes archived wrappers, because
        adoption is about creating a NEW wrapper for a conversation rather than
        resolving a conflict between two live ones.
        """
        references: set[str] = set()
        if not driver_id:
            return references
        for collection in ("sessions", "archive"):
            for record in self._records_in_every_workspace(collection):
                driver = record.get("driver")
                if isinstance(driver, dict) and driver.get("id") == driver_id:
                    resume = driver.get("resume")
                    reference = resume.get("reference") if isinstance(resume, dict) else None
                    if isinstance(reference, str) and reference:
                        references.add(reference)
                agents = record.get("agents")
                resume_ids = agents.get("resume_ids") if isinstance(agents, dict) else None
                parked = resume_ids.get(driver_id) if isinstance(resume_ids, dict) else None
                if isinstance(parked, str) and parked:
                    references.add(parked)
        return references

    @staticmethod
    def _read_foreign_session(path: Path) -> dict[str, Any] | None:
        """Read another workspace's session for ownership checks only.

        `read_path` rejects any record whose workspace differs from this store's,
        so foreign records get a lighter check: an object at the current schema.
        An unreadable file is skipped, which only loses the extra protection.
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return None
        return data

    def create_session(
        self,
        *,
        name: str,
        working_dir: str,
        source: dict[str, Any],
        driver: dict[str, Any],
        plan: dict[str, Any] | None = None,
        host: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_name = " ".join(name.split())
        if not clean_name:
            raise UsageError("Session name cannot be empty.")
        if self.find_by_name(clean_name):
            raise UsageError(f"Session {clean_name!r} already exists in this workspace.")
        now = utc_now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": self.new_session_id(),
            "name": clean_name,
            "workspace_key": self.workspace_key,
            "working_dir": str(Path(working_dir).expanduser().resolve()),
            "source": source,
            "driver": driver,
            "plan": plan or {"path": None, "active_task": None},
            "created_at": now,
            "last_active": now,
            "archived_at": None,
            "host": host or {},
        }
        driver_id = driver.get("id") if isinstance(driver, dict) else None
        agents = AgentResumeState(record)
        agents.mark_active(driver_id if isinstance(driver_id, str) else None)
        self.write("sessions", record["id"], record)
        return record

    def archive_session(self, session_id: str) -> dict[str, Any]:
        record = self.read("sessions", session_id)
        if record is None:
            archived = self.read("archive", session_id)
            if archived is None:
                raise UsageError(f"No session with id {session_id}.")
            return archived
        record["archived_at"] = utc_now()
        self.write("archive", session_id, record)
        self.delete("sessions", session_id)
        for collection in ("status", "activity", "errors"):
            self.delete(collection, session_id)
        return record

    def resume_session(self, session_id: str) -> dict[str, Any]:
        record = self.read("archive", session_id)
        if record is None:
            active = self.read("sessions", session_id)
            if active is None:
                raise UsageError(f"No archived session with id {session_id}.")
            return active
        record["archived_at"] = None
        record["last_active"] = utc_now()
        self.write("sessions", session_id, record)
        self.delete("archive", session_id)
        return record

    def purge_session(self, session_id: str) -> bool:
        found = any(
            self.read(collection, session_id) is not None
            for collection in ("sessions", "archive")
        )
        if not found:
            raise UsageError(f"No session with id {session_id}.")
        for collection in self.COLLECTIONS:
            self.delete(collection, session_id)
        return True
