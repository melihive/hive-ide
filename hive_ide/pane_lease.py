"""Workspace-serialized pane ownership and crash recovery (PLAN in protocol v1)."""

from __future__ import annotations

from functools import wraps
import hmac
import json
import os
import secrets
import shlex
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import HiveIdeError, UsageError
from .python_cmd import PythonCommand
from .store import StateStore, utc_now


def serialized(method):
    """Serialize frame checks and their mutations with acquisition/restoration."""

    @wraps(method)
    def locked(self, *args, **kwargs):
        with self.store.mutation_lock():
            return method(self, *args, **kwargs)

    return locked


class LeaseError(HiveIdeError):
    def __init__(self, error: str, *, status: int = 409, **detail: Any):
        self.payload = {"error": error, **detail}
        if error != "pane_leased":
            self.payload["status"] = status
        super().__init__(error)


def process_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # kill(pid, 0) also succeeds for zombies, which cannot restore a pane.
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


def process_start(pid: int | None) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def remain_on_exit_restore_args(pane: str, previous: dict) -> list[str]:
    # Older records did not track inheritance; preserve their saved value.
    if previous.get("remain_on_exit_local", True):
        return [
            "set-option",
            "-p",
            "-t",
            pane,
            "remain-on-exit",
            previous.get("remain_on_exit", "off"),
        ]
    return ["set-option", "-p", "-u", "-t", pane, "remain-on-exit"]


class LeaseStore:
    def __init__(self, store: StateStore):
        self.store = store

    def path(self, session_id: str, role: str = "plan") -> Path:
        if (
            role != "plan"
            or not session_id
            or Path(session_id).name != session_id
            or session_id in {".", ".."}
        ):
            raise UsageError(
                "Invalid pane lease session or role (v1 supports plan only)."
            )
        return (
            self.store.workspace_dir
            / "sessions"
            / session_id
            / "panes"
            / f"{role}.lease.json"
        )

    def read(self, session_id: str, role: str = "plan") -> dict | None:
        return self.store.read_path(self.path(session_id, role))

    def write(self, lease: dict) -> None:
        with self.store.mutation_lock():
            self.store.write_path(self.path(lease["session_id"], lease["role"]), lease)

    def clear(self, lease: dict) -> None:
        with self.store.mutation_lock():
            current = self.read(lease["session_id"], lease["role"])
            if current and current["lease_id"] == lease["lease_id"]:
                self.path(lease["session_id"], lease["role"]).unlink(missing_ok=True)

    @staticmethod
    def alive(lease: dict) -> bool:
        pid = lease.get("pid")
        return process_alive(pid) and (
            not lease.get("pid_start") or process_start(pid) == lease["pid_start"]
        )

    def live(self, session_id: str, role: str = "plan") -> dict | None:
        if role != "plan":
            return None
        lease = self.read(session_id, role)
        return lease if lease and self.alive(lease) else None

    @staticmethod
    def summary(lease: dict) -> dict:
        return {key: value for key, value in lease.items() if key != "token"}

    def list(self, session_id: str | None = None) -> list[dict]:
        paths = (
            [self.path(session_id)]
            if session_id
            else sorted(
                (self.store.workspace_dir / "sessions").glob("*/panes/*.lease.json")
            )
        )
        return [lease for path in paths if (lease := self.store.read_path(path))]

    def find(self, lease_id: str) -> dict:
        for lease in self.list():
            if lease["lease_id"] == lease_id:
                return lease
        raise LeaseError("lease_not_found", status=404, lease_id=lease_id)


class PaneLeases:
    def __init__(self, frame):
        self.frame = frame
        self.store = frame.store
        self.leases = LeaseStore(self.store)

    def check(
        self, session_id: str, role: str = "plan", *, force: bool = False
    ) -> None:
        if role != "plan":
            return
        with self.store.mutation_lock():
            lease = self.leases.read(session_id, role)
            if lease:
                if self.leases.alive(lease) and not force:
                    raise LeaseError("pane_leased", lease=self.leases.summary(lease))
                # Reap dead leases before allowing ordinary pane mutations.
                # Force is still subject to the ordinary caller guard.
                self.restore(lease, force=force)

    def _pane(self, lease: dict) -> str | None:
        roles = self.frame.role_panes(lease["session_id"])
        if roles is None:
            raise LeaseError(
                "pane_unobservable",
                status=503,
                message="Could not observe role panes; lease preserved.",
            )
        return roles.get(lease["role"])

    def _guard(self, pane: str) -> None:
        if self.frame.pane_is_caller(pane) is not False:
            raise LeaseError(
                "caller_pane",
                message=(
                    "The target may be this command's own pane; run from another pane "
                    "or a terminal outside the session. The caller-pane guard is never bypassed."
                ),
            )

    @staticmethod
    def kill_child(lease: dict) -> None:
        pid = lease.get("child_pid")
        if not pid or (
            lease.get("child_start") and process_start(pid) != lease["child_start"]
        ):
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @serialized
    def restore(self, lease: dict, *, force: bool = False) -> None:
        """Restore a dead/forcibly revoked lease under the mutation lock."""
        current = self.leases.read(lease["session_id"], lease["role"])
        if not current or current["lease_id"] != lease["lease_id"]:
            return
        if self.leases.alive(current) and not force:
            raise LeaseError("pane_leased", lease=self.leases.summary(current))
        pane = self._pane(current)
        if pane:
            self._guard(pane)
        if self.leases.alive(current):
            os.kill(current["pid"], signal.SIGKILL)
        self.kill_child(current)
        record = self.store.find_session(current["session_id"])
        # Clear only after restoration succeeds; failures remain repairable.
        if record and pane:
            if not self.frame.respawn_role_pane(
                record, current["role"], pane, _lease_restore=True
            ):
                raise LeaseError(
                    "restore_failed", message="Could not restore the leased pane."
                )
            result = self.frame.tmux(
                remain_on_exit_restore_args(pane, current["previous"])
            )
            if result.returncode:
                raise LeaseError(
                    "restore_failed",
                    message=result.stderr.strip() or "Could not restore pane options.",
                )
        elif record:
            # A destroyed window/pane is reconstructed through the usual frame path.
            self.leases.clear(current)
            try:
                if not self.frame.ensure(record):
                    self.frame.restore_missing_panes(record, (current["role"],))
            except Exception:
                self.leases.write(current)
                raise
        self.leases.clear(current)

    def acquire(
        self,
        record: dict,
        *,
        role: str,
        title: str,
        argv: list[str],
        cwd: str | None = None,
        owner_label: str | None = None,
    ) -> dict:
        if not argv:
            raise UsageError("pane-lease requires a command after --.")
        working_dir = (
            str(Path(cwd).expanduser().resolve())
            if cwd
            else self.frame.safe_working_dir(record)
        )
        if not Path(working_dir).is_dir():
            raise UsageError(f"Lease working directory does not exist: {working_dir}")
        with self.store.mutation_lock():
            self.leases.path(record["id"], role)  # Validate the requested lease role.
            self.check(record["id"], role)
            pane = (self.frame.role_panes(record["id"]) or {}).get(role)
            if not pane:
                raise LeaseError(
                    "pane_not_found", status=404, session_id=record["id"], role=role
                )
            self._guard(pane)
            # A pinned interpreter may predate this feature, regardless of metadata.
            try:
                probe = subprocess.run(
                    PythonCommand.cli_argv(
                        ["capabilities"], python=self.frame._record_python(record)
                    ),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                capabilities = json.loads(probe.stdout) if probe.returncode == 0 else {}
                supported = isinstance(
                    capabilities, dict
                ) and "pane-lease" in capabilities.get("features", [])
            except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
                supported = False
            if not supported:
                raise LeaseError(
                    "capability_unavailable",
                    message="The session's pinned interpreter does not support pane-lease.",
                )
            previous_title = self.frame.tmux(
                ["display-message", "-p", "-t", pane, "#{@hive_ide_title}"]
            ).stdout.strip()
            local_remain = self.frame.tmux(
                ["show-options", "-p", "-v", "-t", pane, "remain-on-exit"]
            ).stdout.strip()
            previous_remain = self.frame.tmux(
                ["show-options", "-p", "-v", "-A", "-t", pane, "remain-on-exit"]
            ).stdout.strip()
            lease = {
                "schema_version": 1,
                "lease_id": uuid.uuid4().hex,
                "token": "t" + secrets.token_urlsafe(32),
                "session_id": record["id"],
                "role": role,
                "pid": None,
                "child_pid": None,
                "argv": argv,
                "title": title,
                "owner": {"pid": os.getppid(), "label": owner_label},
                "acquired_at": utc_now(),
                "previous": {
                    "title": previous_title,
                    "remain_on_exit": previous_remain,
                    "remain_on_exit_local": bool(local_remain),
                },
            }
            self.leases.write(lease)
            try:
                self.frame.tmux(
                    ["set-option", "-p", "-t", pane, "remain-on-exit", "on"]
                )
                result = self.frame.tmux(
                    [
                        "respawn-pane",
                        "-k",
                        "-t",
                        pane,
                        "-c",
                        working_dir,
                        *self.frame._environment(record, role=role),
                        "-e",
                        f"HIVE_IDE_PANE_LEASE={lease['lease_id']}",
                        shlex.join(
                            PythonCommand.module_argv(
                                "pane_supervisor",
                                [
                                    "--lease",
                                    lease["lease_id"],
                                    "--role",
                                    role,
                                    "--",
                                    *argv,
                                ],
                                python=self.frame._record_python(record),
                            )
                        ),
                    ]
                )
                if result.returncode:
                    raise HiveIdeError(
                        result.stderr.strip() or "Could not start pane supervisor."
                    )
                lease["pid"] = self.frame._pane_pid(pane)
                lease["pid_start"] = process_start(lease["pid"])
                if not lease["pid"]:
                    raise HiveIdeError("tmux did not report the supervisor pid.")
                self.leases.write(lease)
                self.frame.tmux(["select-pane", "-T", title, "-t", pane])
                self.frame.tmux(
                    ["set-option", "-p", "-t", pane, "@hive_ide_title", title]
                )
            except Exception:
                self.restore(lease, force=True)
                raise
            return {
                "lease_id": lease["lease_id"],
                "token": lease["token"],
                "pane_id": pane,
                "session_id": record["id"],
                "role": role,
            }

    def release(self, lease_id: str, token: str) -> dict:
        # Never wait while holding the lock the supervisor needs to restore.
        with self.store.mutation_lock():
            lease = self.leases.find(lease_id)
            if not hmac.compare_digest(lease["token"], token):
                raise LeaseError(
                    "forbidden", status=403, message="Incorrect pane lease token."
                )
            if self.leases.alive(lease):
                lease["release_requested"] = True
                self.leases.write(lease)
                if lease.get("child_pid"):
                    try:
                        os.kill(lease["pid"], signal.SIGTERM)
                    except ProcessLookupError:
                        pass
            else:
                self.restore(lease)
                return {"lease_id": lease_id, "released": True}
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self.store.mutation_lock():
                current = self.leases.read(lease["session_id"], lease["role"])
                if not current or current["lease_id"] != lease_id:
                    return {"lease_id": lease_id, "released": True}
                if not self.leases.alive(current):
                    self.restore(current)
                    return {"lease_id": lease_id, "released": True}
            time.sleep(0.05)
        # Kill only the child group: the supervisor must survive to restore itself.
        with self.store.mutation_lock():
            current = self.leases.read(lease["session_id"], lease["role"])
            if current and current["lease_id"] == lease_id:
                self.kill_child(current)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.store.mutation_lock():
                current = self.leases.read(lease["session_id"], lease["role"])
                if not current or current["lease_id"] != lease_id:
                    return {"lease_id": lease_id, "released": True}
            time.sleep(0.05)
        raise LeaseError(
            "restore_pending",
            status=503,
            lease_id=lease_id,
            message="Child termination requested; run repair if the supervisor cannot restore.",
        )
