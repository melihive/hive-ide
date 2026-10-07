from __future__ import annotations

import json
import os

import pytest

from hive_ide import PROTOCOL_VERSION, __version__
from hive_ide.cli import main
from hive_ide.errors import UsageError
from hive_ide.pane_lease import LeaseError, LeaseStore, process_start
from hive_ide.store import StateStore


def test_lease_store_atomic_records_and_identity(tmp_path):
    store = StateStore(tmp_path, tmp_path / "workspace")
    leases = LeaseStore(store)
    lease = {
        "schema_version": 1,
        "lease_id": "first",
        "token": "secret",
        "session_id": "session",
        "role": "plan",
        "pid": os.getpid(),
        "pid_start": process_start(os.getpid()),
    }
    leases.write(lease)
    assert (
        leases.path("session").relative_to(store.workspace_dir).as_posix()
        == "sessions/session/panes/plan.lease.json"
    )
    assert leases.live("session")["lease_id"] == "first"
    assert "token" not in leases.summary(lease)
    replacement = {**lease, "lease_id": "second"}
    leases.write(replacement)
    leases.clear(lease)
    assert leases.find("second")["lease_id"] == "second"
    with pytest.raises(LeaseError):
        leases.find("first")
    leases.write({**replacement, "pid_start": "different"})
    assert leases.live("session") is None
    leases.clear(replacement)
    assert leases.list() == []
    for session in ("..", "../outside", ""):
        with pytest.raises(UsageError):
            leases.path(session)


def test_lease_writes_take_workspace_lock(tmp_path, monkeypatch):
    store = StateStore(tmp_path, tmp_path / "workspace")
    original = store.write_path

    def checked(path, data):
        assert store.mutation_lock_held()
        return original(path, data)

    monkeypatch.setattr(store, "write_path", checked)
    LeaseStore(store).write({"session_id": "s", "role": "plan", "lease_id": "a"})


def test_capabilities(capsys):
    assert main(["capabilities"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "protocol_version": PROTOCOL_VERSION,
        "version": __version__,
        "features": ["pane-lease"],
    }


def test_supervisor_restoration_failure_execs_shell(tmp_path, monkeypatch, capsys):
    from hive_ide import pane_supervisor

    store = StateStore(tmp_path / "state", tmp_path)
    lease = {
        "session_id": "s",
        "role": "plan",
        "lease_id": "a",
        "release_requested": True,
    }
    LeaseStore(store).write(lease)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", str(tmp_path))
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", "s")
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", "absent")
    monkeypatch.setenv("TMUX_PANE", "%1")
    monkeypatch.setattr(pane_supervisor.signal, "signal", lambda *args: None)
    monkeypatch.setattr(pane_supervisor.os, "isatty", lambda fd: False)
    calls = []
    monkeypatch.setattr(pane_supervisor.os, "execl", lambda *args: calls.append(args))
    pane_supervisor.supervise("a", "plan", ["unused"])
    assert calls and "opening a shell" in capsys.readouterr().out
    assert LeaseStore(store).read("s") is None
