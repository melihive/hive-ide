"""Protocol-v1 JSON state store."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION
from .errors import SchemaVersionError, StateError, UsageError
from .paths import workspace_hash


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Atomic, workspace-scoped protocol state."""

    COLLECTIONS = frozenset({"sessions", "archive", "status", "activity", "errors"})

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

    @contextmanager
    def mutation_lock(self, *, blocking: bool = True):
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        path = self.workspace_dir / ".mutation.lock"
        try:
            with path.open("a+", encoding="utf-8") as handle:
                flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(handle.fileno(), flags)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise StateError(f"Cannot lock workspace state {path}: {exc}") from exc

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

    def write(self, collection: str, session_id: str, data: dict[str, Any]) -> Path:
        return self.write_path(self.path(collection, session_id), data)

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
