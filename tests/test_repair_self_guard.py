"""Repair must never kill the pane it runs in, and must repair no more than it has to.

Every test here drives a real tmux server on an isolated `-L` socket under the
test's tmp_path, the way `test_tmux_integration.py` does. The incident these
pin: the Hive skill ran `repair` from inside the agent pane; a plan pane whose
cwd had been deleted made repair rebuild the whole window, which killed the
agent that invoked it.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from hive_ide import __version__
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.repair import SessionRepair
from hive_ide.store import StateStore


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": __version__}


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.05):
    """Poll a condition on the live tmux server; the terminal condition is the
    predicate itself, never wall-clock freshness."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def _pane_cwd(frame: Frame, pane_id: str) -> str:
    return frame.tmux(
        ["display-message", "-p", "-t", pane_id, "#{pane_current_path}"]
    ).stdout.strip()


@pytest.fixture
def live(tmp_path, monkeypatch):
    """One session built in a real tmux window on a throwaway socket.

    This process may itself run inside an IDE pane; its own `TMUX_PANE`,
    `TMUX` and `HIVE_IDE_TMUX_SOCKET` are cleared so the frame under test starts
    from "caller is outside". Tests opt into "inside" with `inside()`.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    for key in ("TMUX_PANE", "TMUX", "HIVE_IDE_TMUX_SOCKET"):
        monkeypatch.delenv(key, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["term"]
    record = store.create_session(
        name="ALPHA",
        working_dir=workspace,
        source=_source(),
        driver=driver.resolve(
            name="ALPHA", working_dir=str(workspace), conversation_reference=None
        ),
    )
    socket = f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    frame = Frame(store, socket=socket)
    try:
        assert frame.ensure(record)
        roles = frame.role_panes(record["id"])
        assert roles is not None and set(roles) == {"sidebar", "agent", "plan"}

        def inside(role: str) -> str:
            pane_id = frame.role_panes(record["id"])[role]
            monkeypatch.setenv("TMUX_PANE", pane_id)
            monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", socket)
            return pane_id

        def outside() -> None:
            monkeypatch.delenv("TMUX_PANE", raising=False)
            monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)

        yield SimpleNamespace(
            store=store,
            frame=frame,
            record=record,
            socket=socket,
            workspace=workspace,
            window=frame.windows()[record["id"]],
            inside=inside,
            outside=outside,
            repair=lambda: SessionRepair(store, frame).repair(
                store.find_session(record["id"])
            ),
        )
    finally:
        frame.tmux(["kill-server"])


def _stale_agent_env(monkeypatch) -> None:
    """Make the agent pane look like it belongs to another session: the one
    repair branch that wants a whole-window rebuild without touching cwds."""
    monkeypatch.setattr(
        Frame,
        "pane_hive_ide_env",
        lambda _self, _pane_id: {"HIVE_IDE_SESSION_ID": "someone-else"},
    )


def _marker(store: StateStore, session_id: str):
    record = store.find_session(session_id)
    return ((record.get("host") or {}).get("repair") or {}).get("deferred_rebuild")


def _stages(store: StateStore, session_id: str) -> list[str]:
    document = store.read("repairs", session_id) or {}
    return [entry["stage"] for entry in document.get("entries") or []]


def test_rebuild_from_inside_the_window_is_deferred_and_marked(live, monkeypatch):
    agent_pane = live.inside("agent")
    roles_before = live.frame.role_panes(live.record["id"])

    direct = live.frame.rebuild(live.record)

    assert direct == {
        "rebuilt": False,
        "deferred": True,
        "reason": "caller-inside-window",
        "window": live.window,
    }
    assert live.frame.windows()[live.record["id"]] == live.window
    assert live.frame.role_panes(live.record["id"]) == roles_before

    _stale_agent_env(monkeypatch)
    result = live.repair()

    assert result["ok"] is True
    assert result["rebuilt"] is False
    assert result["deferred"] == ["stale agent environment"]
    assert (
        "window: rebuild deferred (caller inside window): stale agent environment"
        in result["actions"]
    )
    assert any("from outside the window" in warning for warning in result["warnings"])
    assert live.frame.windows()[live.record["id"]] == live.window
    assert live.frame.role_panes(live.record["id"])["agent"] == agent_pane
    marker = _marker(live.store, live.record["id"])
    assert marker["reason"] == "stale agent environment"
    assert marker["op_id"] and marker["requested_at"]
    entries = (live.store.read("repairs", live.record["id"]) or {})["entries"]
    assert [entry["stage"] for entry in entries] == ["planned", "deferred"]
    assert entries[-1]["caller_pane"] == agent_pane
    assert entries[-1]["caller_in_window"] is True
    assert entries[-1]["target_window"] == live.window
    assert entries[-1]["branch"] == "stale-agent-environment"
    assert entries[-1]["pane_roles"] == roles_before
    assert {pane["role"] for pane in entries[-1]["pane_cwds"]} == {
        "sidebar",
        "agent",
        "plan",
    }
    assert not any("HIVE_IDE_" in key for entry in entries for key in entry)


def test_rebuild_from_outside_replaces_the_window_and_clears_the_marker(
    live, monkeypatch
):
    live.inside("agent")
    _stale_agent_env(monkeypatch)
    deferred = live.repair()
    assert deferred["deferred"] == ["stale agent environment"]
    assert _marker(live.store, live.record["id"]) is not None

    live.outside()
    result = live.repair()

    assert result["ok"] is True
    assert result["rebuilt"] is True
    assert result["deferred"] == []
    assert any(
        action.startswith("window: deferred rebuild re-evaluated from outside")
        for action in result["actions"]
    )
    assert "window: rebuilt for stale agent environment" in result["actions"]
    replacement = live.frame.windows()[live.record["id"]]
    assert replacement != live.window
    window_ids = live.frame.tmux(
        ["list-windows", "-t", live.frame.target, "-F", "#{window_id}"]
    ).stdout.split()
    assert live.window not in window_ids
    assert replacement in window_ids
    assert _marker(live.store, live.record["id"]) is None
    assert _stages(live.store, live.record["id"]) == [
        "planned",
        "deferred",
        "planned",
        "completed",
    ]


def test_marker_alone_never_rebuilds(live, monkeypatch):
    live.inside("agent")
    _stale_agent_env(monkeypatch)
    assert live.repair()["deferred"] == ["stale agent environment"]
    monkeypatch.undo()
    # The environment is healthy again: nothing warrants a rebuild any more.
    for key in ("TMUX_PANE", "TMUX", "HIVE_IDE_TMUX_SOCKET"):
        monkeypatch.delenv(key, raising=False)

    result = live.repair()

    assert result["rebuilt"] is False
    assert "window: deferred rebuild no longer warranted; marker cleared" in result["actions"]
    assert live.frame.windows()[live.record["id"]] == live.window
    assert _marker(live.store, live.record["id"]) is None


def test_deleted_plan_pane_cwd_respawns_only_the_plan_pane(live, tmp_path):
    roles = live.frame.role_panes(live.record["id"])
    doomed = tmp_path / "doomed-worktree"
    doomed.mkdir()
    # The incident shape: the plan pane was respawned with the worktree as cwd,
    # then the worktree was deleted underneath it.
    respawned = live.frame.tmux(
        ["respawn-pane", "-k", "-t", roles["plan"], "-c", str(doomed), "sh"]
    )
    assert respawned.returncode == 0, respawned.stderr
    assert _wait_for(lambda: _pane_cwd(live.frame, roles["plan"]) == str(doomed))
    doomed.rmdir()
    assert _wait_for(lambda: _pane_cwd(live.frame, roles["plan"]).endswith(" (deleted)"))

    result = live.repair()

    assert result["ok"] is True
    assert result["rebuilt"] is False
    assert result["deferred"] == []
    assert "plan pane: respawned (cwd was deleted)" in result["actions"]
    assert not any(action.startswith("window: rebuilt") for action in result["actions"])
    assert any(
        warning.startswith("plan pane cwd no longer exists")
        and warning.endswith("repair will respawn that pane in place")
        for warning in result["warnings"]
    )
    assert live.frame.windows()[live.record["id"]] == live.window
    after = live.frame.role_panes(live.record["id"])
    assert after["agent"] == roles["agent"]
    assert after["plan"] == roles["plan"]
    assert _wait_for(
        lambda: _pane_cwd(live.frame, roles["plan"]) == str(live.workspace.resolve())
    )
    assert _stages(live.store, live.record["id"]) == ["planned", "completed"]


def test_respawned_panes_carry_the_record_session_id_not_the_tmux_session_env(live):
    roles = live.frame.role_panes(live.record["id"])
    assert (
        live.frame.tmux(
            ["set-environment", "-t", live.frame.target, "HIVE_IDE_SESSION_ID", "someone-else"]
        ).returncode
        == 0
    )

    # Control: a bare respawn inherits the tmux session environment. This is the
    # leak the per-record environment on every respawn exists to beat.
    control = live.frame.tmux(
        ["respawn-pane", "-k", "-t", roles["plan"], "sh"]
    )
    assert control.returncode == 0, control.stderr
    assert _wait_for(
        lambda: live.frame.pane_hive_ide_env(roles["plan"]).get("HIVE_IDE_SESSION_ID")
        == "someone-else"
    )

    assert live.frame.refresh_plan_pane(live.record) is True
    assert live.frame.respawn_agent(live.record, roles["agent"]) is True
    assert live.frame.respawn_role_pane(live.record, "sidebar", roles["sidebar"]) is True

    for role in ("plan", "agent", "sidebar"):
        observed = _wait_for(
            lambda role=role: (
                live.frame.pane_hive_ide_env(roles[role]).get("HIVE_IDE_SESSION_ID")
                == live.record["id"]
            )
        )
        assert observed, f"{role} pane did not get the record's session id"
    assert live.frame.role_panes(live.record["id"]) == roles


def test_respawn_helpers_refuse_the_callers_own_pane(live):
    roles = live.frame.role_panes(live.record["id"])
    pids_before = {
        role: live.frame._pane_pid(pane_id) for role, pane_id in roles.items()
    }

    live.inside("plan")
    assert live.frame.refresh_plan_pane(live.record) is False
    assert live.frame.respawn_role_pane(live.record, "plan", roles["plan"]) is False
    live.inside("agent")
    assert live.frame.respawn_agent(live.record, roles["agent"]) is False
    live.inside("sidebar")
    assert live.frame.refresh_sidebar_if_needed(live.record) is False

    assert {
        role: live.frame._pane_pid(pane_id) for role, pane_id in roles.items()
    } == pids_before


def test_unobservable_panes_never_authorize_a_rebuild(live, monkeypatch):
    live.inside("agent")
    live.outside()
    original = Frame.tmux
    roles_before = live.frame.role_panes(live.record["id"])

    def failing_list_panes(args, **kwargs):
        if args[:1] == ["list-panes"] and args[-1] == "#{@hive_ide_pane}\t#{pane_id}":
            return SimpleNamespace(returncode=1, stdout="", stderr="no server running")
        return original(live.frame, args, **kwargs)

    monkeypatch.setattr(live.frame, "tmux", failing_list_panes)
    assert live.frame.role_panes(live.record["id"]) is None

    result = live.repair()

    assert result["ok"] is True
    assert result["rebuilt"] is False
    assert result["deferred"] == []
    assert (
        f"could not observe panes of window {live.window}; no rebuild"
        in result["warnings"]
    )
    assert "window: panes unobservable; nothing destructive attempted" in result["actions"]
    assert not any(action.startswith("window: rebuilt") for action in result["actions"])
    monkeypatch.undo()
    assert live.frame.windows()[live.record["id"]] == live.window
    assert live.frame.role_panes(live.record["id"]) == roles_before
    assert _stages(live.store, live.record["id"]) == ["skipped"]


def test_repair_log_keeps_only_the_newest_fifty_entries(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    for index in range(60):
        SessionRepair.append_repair_log(
            store, "session-1", {"at": f"t{index:02d}", "stage": "skipped", "op_id": str(index)}
        )

    document = store.read("repairs", "session-1")
    assert document["session_id"] == "session-1"
    assert len(document["entries"]) == SessionRepair.REPAIR_LOG_LIMIT == 50
    assert document["entries"][0]["at"] == "t10"
    assert document["entries"][-1]["at"] == "t59"


def test_caller_on_this_server_falls_back_to_the_tmux_variable(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace), socket="hive-ide-abc")
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)
    monkeypatch.setenv("TMUX_PANE", "%3")

    monkeypatch.delenv("TMUX", raising=False)
    assert frame.caller_on_this_server() is False
    assert frame.is_caller_pane("%3") is False

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/hive-ide-abc,4242,0")
    assert frame.caller_on_this_server() is True
    assert frame.is_caller_pane("%3") is True
    assert frame.is_caller_pane("%4") is False

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,4242,0")
    assert frame.caller_on_this_server() is False

    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", "hive-ide-abc")
    assert frame.caller_on_this_server() is True
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", "hive-ide-other")
    assert frame.caller_on_this_server() is False
