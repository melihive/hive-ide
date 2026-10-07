from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from hive_ide import __version__
from hive_ide.cli import main
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.pane_lease import LeaseError, LeaseStore, PaneLeases, process_alive
from hive_ide.repair import SessionRepair
from hive_ide.sidebar_plugins import PlanProvider
from hive_ide.store import StateStore

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is unavailable"
)


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail("Timed out waiting for pane lease transition")


@pytest.fixture(params=["stable", "dev"])
def pane(tmp_path, monkeypatch, request):
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[1]))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = workspace / "plan.md"
    plan.write_text("# ORIGINAL PLAN\n")
    store = StateStore(tmp_path / "state", workspace)
    store.write_path(store.config_snapshot_path(), {"editor": {"argv": ["/bin/cat"]}})
    record = store.create_session(
        name="LEASE",
        working_dir=workspace,
        source={
            "kind": request.param,
            "interpreter": sys.executable,
            "version": __version__,
        },
        driver=bundled_drivers()["term"].resolve(
            name="LEASE", working_dir=str(workspace), conversation_reference=None
        ),
        plan={"path": str(plan), "active_task": None},
    )
    frame = Frame(store, socket=f"hive-ide-lease-test-{uuid.uuid4().hex[:8]}")
    # Exercise the real spawn/role/env paths without unrelated frame setup.
    window = frame.build(record)
    frame.tmux(["resize-window", "-t", window, "-x", "180", "-y", "40"])
    frame.apply_columns(record)
    base = ["--state-home", str(store.home), "--workspace-key", str(workspace)]
    yield frame, record, PaneLeases(frame), base
    for lease in LeaseStore(store).list():
        PaneLeases.kill_child(lease)
    frame.tmux(["kill-server"])


def acquire(manager, record, code="import time; time.sleep(60)", **kwargs):
    result = manager.acquire(
        record,
        role="plan",
        title="Borrowed",
        argv=[sys.executable, "-c", code],
        **kwargs,
    )
    wait_for(lambda: (manager.leases.read(record["id"]) or {}).get("child_pid"))
    return result


def content(frame, pane_id):
    return frame.tmux(["capture-pane", "-p", "-t", pane_id]).stdout


def test_exit_restores_current_plan_and_environment(pane, tmp_path, capsys):
    frame, record, manager, base = pane
    output = tmp_path / "env.json"
    gate = tmp_path / "exit"
    code = (
        "import os,json,time,pathlib; "
        f"pathlib.Path({str(output)!r}).write_text(json.dumps(dict(os.environ))); "
        f"gate=pathlib.Path({str(gate)!r})\nwhile not gate.exists(): time.sleep(.05)"
    )
    result = acquire(manager, record, code, cwd=str(tmp_path), owner_label="test")
    env = json.loads(wait_for(lambda: output.read_text() if output.exists() else None))
    assert env["HIVE_IDE_PANE_ROLE"] == "plan"
    assert env["HIVE_IDE_PANE_LEASE"] == result["lease_id"]
    assert env["HIVE_IDE_SESSION_ID"] == record["id"]
    assert main([*base, "pane-status", "--session-id", record["id"]]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["leases"][0]["alive"] is True
    assert "token" not in status["leases"][0]
    assert PlanProvider().value(frame.store.home, record) == "leased"
    assert frame.current_plan(record, focus=True)["reason"] == "pane_leased"
    updated_plan = tmp_path / "new.md"
    updated_plan.write_text("# CURRENT RESTORED PLAN\n")
    record["plan"]["path"] = str(updated_plan)
    frame.store.write("sessions", record["id"], record)
    gate.touch()
    wait_for(lambda: manager.leases.read(record["id"]) is None)
    wait_for(lambda: "CURRENT RESTORED PLAN" in content(frame, result["pane_id"]))
    assert (
        frame.tmux(
            ["display-message", "-p", "-t", result["pane_id"], "#{@hive_ide_title}"]
        ).stdout.strip()
        == "CURRENT RESTORED PLAN"
    )
    assert (
        frame.tmux(
            ["display-message", "-p", "-t", result["pane_id"], "#{pane_dead}"]
        ).stdout.strip()
        == "0"
    )


def test_release_tokens_and_second_acquire(pane):
    frame, record, manager, base = pane
    result = acquire(manager, record)
    with pytest.raises(LeaseError) as error:
        manager.release(result["lease_id"], "wrong")
    assert error.value.payload["status"] == 403
    with pytest.raises(LeaseError) as error:
        acquire(manager, record)
    assert error.value.payload["error"] == "pane_leased"
    assert "token" not in error.value.payload["lease"]
    assert manager.release(result["lease_id"], result["token"])["released"]
    wait_for(lambda: "ORIGINAL PLAN" in content(frame, result["pane_id"]))
    with pytest.raises(LeaseError) as error:
        manager.release(result["lease_id"], result["token"])
    assert error.value.payload["status"] == 404


def test_repair_plan_set_and_rebuild_preserve_live_lease(pane, tmp_path, capsys):
    frame, record, manager, base = pane
    result = acquire(manager, record)
    lease = manager.leases.read(record["id"])
    repaired = SessionRepair(frame.store, frame).repair(record)
    assert repaired["ok"], repaired
    assert repaired["pane_leased"]["lease_id"] == result["lease_id"]
    assert process_alive(lease["child_pid"])
    assert frame.rebuild(record)["reason"] == "pane_leased"
    for operation in (
        lambda: frame.refresh_plan_pane(record),
        lambda: frame.current_plan(record),
        lambda: frame.build(record),
    ):
        with pytest.raises(LeaseError):
            operation()
    assert frame.restore_missing_panes(record, ("plan",)) == ()
    args = [
        *base,
        "plan-set",
        "--session-id",
        record["id"],
        "--tmux-socket",
        frame.socket,
        "--clear",
    ]
    assert main(args) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "pane_leased"
    assert frame.store.find_session(record["id"])["plan"]["path"]
    assert main([*args, "--force"]) == 0
    capsys.readouterr()
    assert manager.leases.read(record["id"]) is None
    wait_for(lambda: "No plan linked" in content(frame, result["pane_id"]))


def test_repair_dead_supervisor(pane):
    frame, record, manager, base = pane
    result = acquire(manager, record)
    lease = manager.leases.read(record["id"])
    os.kill(lease["pid"], signal.SIGKILL)
    wait_for(lambda: not manager.leases.alive(lease))
    repaired = SessionRepair(frame.store, frame).repair(record)
    assert repaired["ok"], repaired
    assert manager.leases.read(record["id"]) is None
    wait_for(lambda: "ORIGINAL PLAN" in content(frame, result["pane_id"]))


def test_concurrent_cli_acquire_exactly_one_wins(pane, capsys):
    frame, record, manager, base = pane
    command = [
        sys.executable,
        "-m",
        "hive_ide.cli",
        *base,
        "pane-lease",
        "--session-id",
        record["id"],
        "--role",
        "plan",
        "--title",
        "Concurrent",
        "--tmux-socket",
        frame.socket,
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
    ]
    processes = [
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    results = [process.communicate(timeout=10) for process in processes]
    assert sorted(process.returncode for process in processes) == [0, 2], results
    winner = next(
        json.loads(out)
        for process, (out, err) in zip(processes, results)
        if process.returncode == 0
    )
    loser = next(
        json.loads(out)
        for process, (out, err) in zip(processes, results)
        if process.returncode == 2
    )
    assert loser["error"] == "pane_leased"
    assert manager.release(winner["lease_id"], winner["token"])["released"]


def test_fast_startup_failure_restores(pane):
    frame, record, manager, base = pane
    result = manager.acquire(
        record, role="plan", title="Missing", argv=["/no/such/program"]
    )
    wait_for(lambda: "could not run" in content(frame, result["pane_id"]))
    wait_for(lambda: manager.leases.read(record["id"]) is None)
    wait_for(lambda: "ORIGINAL PLAN" in content(frame, result["pane_id"]))


def test_release_escalates_stubborn_child_and_restores(pane, tmp_path):
    frame, record, manager, base = pane
    ready = tmp_path / "ready"
    result = acquire(
        manager,
        record,
        f"import signal,time,pathlib; signal.signal(signal.SIGTERM, signal.SIG_IGN); pathlib.Path({str(ready)!r}).touch(); time.sleep(60)",
    )
    wait_for(ready.exists)
    lease = manager.leases.read(record["id"])
    assert manager.release(result["lease_id"], result["token"])["released"]
    assert not process_alive(lease["child_pid"])
    wait_for(lambda: "ORIGINAL PLAN" in content(frame, result["pane_id"]))


def test_interactive_child_has_foreground_tty_and_accepts_input(pane, tmp_path):
    frame, record, manager, base = pane
    output = tmp_path / "input"
    result = acquire(
        manager,
        record,
        "import os,pathlib; assert all(os.isatty(fd) for fd in (0,1,2)); "
        "assert os.tcgetpgrp(0)==os.getpgrp(); "
        f"value=input('LEASE INPUT: '); pathlib.Path({str(output)!r}).write_text(value)",
    )
    wait_for(lambda: "LEASE INPUT" in content(frame, result["pane_id"]))
    frame.tmux(["send-keys", "-t", result["pane_id"], "hello", "Enter"])
    wait_for(output.exists)
    assert output.read_text() == "hello"
    wait_for(lambda: manager.leases.read(record["id"]) is None)
    wait_for(lambda: "ORIGINAL PLAN" in content(frame, result["pane_id"]))


def test_rebuild_force_revokes_but_caller_guard_remains(pane, monkeypatch):
    frame, record, manager, base = pane
    result = acquire(manager, record)
    monkeypatch.setenv("TMUX", f"{frame.socket_path()},123,0")
    monkeypatch.setenv("TMUX_PANE", result["pane_id"])
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", frame.socket)
    assert frame.rebuild(record, force=True)["reason"] == "caller-inside-window"
    with pytest.raises(LeaseError) as error:
        frame.refresh_plan_pane(record, force=True)
    assert error.value.payload["error"] == "caller_pane"
    assert manager.leases.live(record["id"])
    for key in ("TMUX", "TMUX_PANE", "HIVE_IDE_TMUX_SOCKET"):
        monkeypatch.delenv(key)
    assert frame.rebuild(record, force=True)["rebuilt"]
    assert manager.leases.read(record["id"]) is None
    assert set(frame.role_panes(record["id"])) == {"sidebar", "agent", "plan"}


def test_dead_lease_reacquire_and_release_recover(pane):
    frame, record, manager, base = pane
    first = acquire(manager, record)
    lease = manager.leases.read(record["id"])
    os.kill(lease["pid"], signal.SIGKILL)
    wait_for(lambda: not manager.leases.alive(lease))
    second = acquire(manager, record)
    assert second["lease_id"] != first["lease_id"]
    lease = manager.leases.read(record["id"])
    os.kill(lease["pid"], signal.SIGKILL)
    wait_for(lambda: not manager.leases.alive(lease))
    assert manager.release(second["lease_id"], second["token"])["released"]
    wait_for(lambda: "ORIGINAL PLAN" in content(frame, second["pane_id"]))


def test_cli_json_round_trip_and_literal_child_arguments(pane, tmp_path, capsys):
    frame, record, manager, base = pane
    output = tmp_path / "argv.json"
    args = [
        *base,
        "pane-lease",
        "--session-id",
        record["id"],
        "--role",
        "plan",
        "--title",
        "CLI monitor",
        "--tmux-socket",
        frame.socket,
        "--",
        sys.executable,
        "-c",
        f"import sys,pathlib,json,time; pathlib.Path({str(output)!r}).write_text(json.dumps(sys.argv[1:])); time.sleep(60)",
        "--quiet",
        "two words",
        "$(literal)",
    ]
    assert main(args) == 0
    acquired = json.loads(capsys.readouterr().out)
    wait_for(output.exists)
    assert json.loads(output.read_text()) == ["--quiet", "two words", "$(literal)"]
    assert main([*base, "pane-status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["leases"][0]["alive"]
    assert main(["capabilities"]) == 0
    capabilities = json.loads(capsys.readouterr().out)
    # Preserve exact scratch-server responses for the implementation handoff.
    artifact = Path(__file__).resolve().parents[1] / "temp" / "phase9-test-json.json"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "pane-lease": acquired,
                "pane-status": status,
                "capabilities": capabilities,
            },
            indent=2,
        )
        + "\n"
    )
    release = [
        *base,
        "pane-release",
        "--lease",
        acquired["lease_id"],
        "--tmux-socket",
        frame.socket,
        "--token",
    ]
    assert main([*release, "wrong"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == 403
    assert main([*release, acquired["token"]]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "lease_id": acquired["lease_id"],
        "released": True,
    }


def test_no_plan_and_silent_nonzero_restore(pane):
    frame, record, manager, base = pane
    record["plan"] = {"path": None, "active_task": None}
    frame.store.write("sessions", record["id"], record)
    result = manager.acquire(
        record, role="plan", title="Silent", argv=["/bin/sh", "-c", "exit 17"]
    )
    wait_for(lambda: "status 17" in content(frame, result["pane_id"]))
    wait_for(lambda: manager.leases.read(record["id"]) is None)
    wait_for(lambda: "No plan linked" in content(frame, result["pane_id"]))


def test_all_spawned_and_restored_panes_expose_role(pane):
    frame, record, manager, base = pane
    roles = frame.role_panes(record["id"])
    for role, pane_id in roles.items():
        wait_for(
            lambda: (
                (frame.pane_hive_ide_env(pane_id) or {}).get("HIVE_IDE_PANE_ROLE")
                == role
            )
        )
    frame.tmux(["kill-pane", "-t", roles["plan"]])
    assert frame.restore_missing_panes(record, ("plan",)) == ("plan",)
    pane_id = frame.role_panes(record["id"])["plan"]
    wait_for(
        lambda: (
            (frame.pane_hive_ide_env(pane_id) or {}).get("HIVE_IDE_PANE_ROLE") == "plan"
        )
    )
    assert frame.refresh_plan_pane(record)
    wait_for(
        lambda: (
            (frame.pane_hive_ide_env(pane_id) or {}).get("HIVE_IDE_PANE_ROLE") == "plan"
        )
    )


def test_pinned_interpreter_invoked_for_probe_and_supervisor(pane, tmp_path):
    import shlex

    frame, record, manager, base = pane
    invoked = tmp_path / "interpreter-calls"
    wrapper = tmp_path / "pinned-python"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {shlex.quote(str(invoked))}\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n'
    )
    wrapper.chmod(0o755)
    record["source"]["interpreter"] = str(wrapper)
    frame.store.write("sessions", record["id"], record)
    result = acquire(manager, record)
    commands = invoked.read_text()
    assert "hive_ide.cli capabilities" in commands
    assert "hive_ide.pane_supervisor --lease" in commands
    assert manager.release(result["lease_id"], result["token"])["released"]


def test_unsupported_pinned_interpreter_preserves_default(pane, tmp_path):
    frame, record, manager, base = pane
    record["source"]["interpreter"] = "/bin/false"
    pane_id = frame.role_panes(record["id"])["plan"]
    before = frame._pane_pid(pane_id)
    with pytest.raises(LeaseError) as error:
        manager.acquire(record, role="plan", title="Unsupported", argv=["sleep", "60"])
    assert error.value.payload["error"] == "capability_unavailable"
    assert frame._pane_pid(pane_id) == before
    assert manager.leases.read(record["id"]) is None
