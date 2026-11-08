from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import Mock

import pytest

from hive_ide import PROTOCOL_VERSION, __version__
from hive_ide.cli import main
from hive_ide.errors import UsageError
from hive_ide.pane_lease import LeaseError, LeaseStore, PaneLeases, process_start
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
        "features": [
            "pane-lease",
            "plan-set-defer-pane",
            "labels",
            "launch-prompt",
            "turn-sequencing",
            "driver-rename-guard",
            "wake-prompt",
        ],
    }


def test_generated_tokens_never_start_with_dash(tmp_path, monkeypatch):
    from hive_ide import pane_lease

    store = StateStore(tmp_path / "state", tmp_path)
    frame = Mock(store=store)
    frame.safe_working_dir.return_value = str(tmp_path)
    frame.role_panes.return_value = {"plan": "%1"}
    frame.pane_is_caller.return_value = False
    frame._environment.return_value = []
    frame._record_python.return_value = "/usr/bin/python3"
    frame._pane_pid.return_value = os.getpid()
    frame.tmux.return_value = subprocess.CompletedProcess([], 0, "off\n", "")
    monkeypatch.setattr(
        pane_lease.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, '{"features": ["pane-lease"]}', ""
        ),
    )
    random_token = pane_lease.secrets.token_urlsafe
    draws = 0

    def token_urlsafe(size):
        nonlocal draws
        draws += 1
        token = random_token(size)
        # Guarantee coverage of the formerly intermittent leading-dash case.
        return "-" + token[1:] if draws == 1 else token

    monkeypatch.setattr(pane_lease.secrets, "token_urlsafe", token_urlsafe)
    manager = PaneLeases(frame)
    tokens = []
    for _ in range(2000):
        result = manager.acquire(
            {"id": "s"}, role="plan", title="Tokens", argv=["unused"]
        )
        tokens.append(result["token"])
        manager.leases.clear(manager.leases.read("s"))
    assert len(tokens) == 2000
    assert all(not token.startswith("-") for token in tokens)


def test_cli_accepts_dash_token_with_equals(tmp_path, monkeypatch, capsys):
    release = Mock(return_value={"released": True})
    monkeypatch.setattr(PaneLeases, "release", release)
    assert (
        main(
            [
                "--state-home",
                str(tmp_path / "state"),
                "--workspace-key",
                str(tmp_path),
                "pane-release",
                "--lease",
                "lease-id",
                "--token=-abc",
            ]
        )
        == 0
    )
    release.assert_called_once_with("lease-id", "-abc")
    assert json.loads(capsys.readouterr().out) == {"released": True}


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

def test_a_pane_leased_refusal_names_its_holder_and_the_remedy(tmp_path):
    """`pane_leased` carries the remedy, not just the verdict.

    The payload always held `owner.label`, the pid and the lease id, but the
    refusal said only `pane_leased` — which reads as "the IDE is busy". Callers
    concluded the session was in use and stopped, when the usual holder is a
    monitor whose run has already finished.
    """
    store = StateStore(tmp_path, tmp_path / "workspace")
    leases = LeaseStore(store)
    leases.write(
        {
            "schema_version": 1,
            "lease_id": "L1",
            "token": "secret",
            "session_id": "session",
            "role": "plan",
            "pid": os.getpid(),
            "pid_start": process_start(os.getpid()),
            "owner": {"pid": os.getpid(), "label": "hive-monitor:runbook:demo-a1b2c3"},
        }
    )
    frame = Mock()
    frame.store = store
    with pytest.raises(LeaseError) as excinfo:
        PaneLeases(frame).check("session", "plan")
    payload = excinfo.value.payload
    assert payload["error"] == "pane_leased"
    message = payload.get("message") or ""
    assert "demo-a1b2c3" in message, message
    assert "runbook monitor --close" in message, message
    # The refusal is handed to a caller that failed authorization — it must not
    # become the channel that gives them the lease token.
    assert "secret" not in json.dumps(payload), payload


def test_an_unrecognised_lease_owner_gets_no_invented_remedy():
    """An unknown holder gets the generic remedy, never a made-up command.

    The close hint is derived from a label shape this package recognises. For
    anything else the honest answer is "ask the owner", because a confidently
    wrong remedy is worse than none: the caller runs it.
    """
    message = LeaseStore.refusal({"owner": {"pid": 42, "label": "someone-elses-tool"}})
    assert "monitor --close" not in message, message
    assert "Ask its owner to release it." in message, message
    assert "--force" in message, message


def test_a_lease_with_no_owner_at_all_still_says_what_to_do():
    """Three states: a known label, an unknown label, and no label.

    A lease written before `owner` existed, or by a caller that passed no label,
    must still produce a refusal a human can act on rather than an empty one.
    """
    by_pid = LeaseStore.refusal({"owner": {"pid": 42}})
    assert "pid 42" in by_pid and "--force" in by_pid, by_pid
    anonymous = LeaseStore.refusal({})
    assert "another holder" in anonymous and "--force" in anonymous, anonymous

def test_an_acquisition_refusal_does_not_offer_a_flag_pane_lease_lacks():
    """`pane-lease` has no `--force`, so its refusal must not name one.

    `PaneLeases.acquire` goes through the same `check`, so the shared remedy
    reached a command that cannot act on it: following the advice gets
    `unrecognized arguments: --force` and the lease is untouched. A remedy that
    does not exist is worse than none, because it reads as supported recovery.
    """
    lease = {"owner": {"pid": 7, "label": "hive-monitor:runbook:demo-a1b2c3"}}
    offered = LeaseStore.refusal(lease)
    withheld = LeaseStore.refusal(lease, force_option=False)
    assert "--force" in offered, offered
    assert "--force" not in withheld, withheld
    # Still actionable — it must say what CAN be done, not merely refuse.
    assert "close the holder" in withheld, withheld
    assert "runbook monitor --close" in withheld, withheld


def test_acquire_asks_for_the_no_force_refusal(tmp_path):
    """Pin the CALL, not just the helper.

    The helper defaults to offering `--force`, so `refusal()` alone stays green
    whichever way `acquire` calls `check`. This asserts the acquisition path opts
    out. It lets `acquire` fail immediately after the check (no pane on a stub
    frame), because the check is the only part under test here.
    """
    store = StateStore(tmp_path, tmp_path / "workspace")
    frame = Mock()
    frame.store = store
    frame.safe_working_dir.return_value = str(tmp_path)
    frame.role_panes.return_value = {}

    leases = PaneLeases(frame)
    seen = {}

    def spy(session_id, role="plan", *, force=False, force_option=True):
        seen.update(session_id=session_id, role=role, force_option=force_option)

    leases.check = spy
    with pytest.raises(LeaseError) as excinfo:
        leases.acquire(
            {"id": "session"}, role="plan", title="t", argv=["sh"], cwd=str(tmp_path)
        )
    assert excinfo.value.payload["error"] == "pane_not_found"
    assert seen.get("force_option") is False, (
        f"acquire offered --force, which `pane-lease` does not accept: {seen}")
