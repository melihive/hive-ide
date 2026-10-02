"""Repair must never kill the pane it runs in, and must repair no more than it has to.

Most tests here drive a real tmux server on an isolated `-L` socket (a server
name, not a path; the server's state and panes live under the test's tmp_path),
the way `test_tmux_integration.py` does. A few are plain unit tests of the
caller-identity verdicts, the repair log and a transport-level tmux stub. The
incident these pin: the Hive skill ran `repair` from inside the agent pane; a
plan pane whose cwd had been deleted made repair rebuild the whole window, which
killed the agent that invoked it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace

import pytest

from hive_ide import __version__
from hive_ide.cli import main
from hive_ide.drivers import CommandDriver, DriverRegistry, bundled_drivers
from hive_ide.errors import HiveIdeError
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


def _window_ids(frame: Frame) -> list[str]:
    return frame.tmux(
        ["list-windows", "-t", frame.target, "-F", "#{window_id}"]
    ).stdout.split()


@pytest.fixture
def live(tmp_path, monkeypatch):
    """One session built in a real tmux window on a throwaway socket.

    `conftest` already strips this process's own tmux identity, so the frame
    under test starts from "caller is provably outside". Tests opt into a
    specific identity with `inside()` / `identity()`.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
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

        def identity(*, pane: str | None, marker: str | None, tmux: str | None) -> None:
            for key, value in (
                ("TMUX_PANE", pane),
                ("HIVE_IDE_TMUX_SOCKET", marker),
                ("TMUX", tmux),
            ):
                if value is None:
                    monkeypatch.delenv(key, raising=False)
                else:
                    monkeypatch.setenv(key, value)

        def inside(role: str) -> str:
            pane_id = frame.role_panes(record["id"])[role]
            identity(pane=pane_id, marker=socket, tmux=f"{frame.socket_path()},1,0")
            return pane_id

        def outside() -> None:
            identity(pane=None, marker=None, tmux=None)

        yield SimpleNamespace(
            store=store,
            frame=frame,
            record=record,
            socket=socket,
            workspace=workspace,
            window=frame.windows()[record["id"]],
            identity=identity,
            inside=inside,
            outside=outside,
            base=[
                "--state-home",
                str(store.home),
                "--workspace-key",
                store.workspace_key,
            ],
            repair=lambda registry=None: SessionRepair(
                store, frame, registry=registry
            ).repair(store.find_session(record["id"])),
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
    return SessionRepair.deferred_rebuild_marker(store.find_session(session_id))


def _stages(store: StateStore, session_id: str) -> list[str]:
    document = store.read("repairs", session_id) or {}
    return [entry["stage"] for entry in document.get("entries") or []]


def _pane_pids(frame: Frame, roles: dict[str, str]) -> dict[str, int | None]:
    return {role: frame._pane_pid(pane_id) for role, pane_id in roles.items()}


# -- caller identity: the tri-state truth table ----------------------------------------


THIS = "hive-ide-abc"
OTHER = "hive-ide-other"


def _tmux_var(socket: str, *, tmpdir: str = "/tmp") -> str:
    return f"{tmpdir}/tmux-{os.getuid()}/{socket},4242,0"


@pytest.mark.parametrize(
    ("pane", "marker", "tmux", "server", "location", "is_caller"),
    [
        # No tmux evidence at all: a plain terminal or a daemon. Provably outside.
        (None, None, None, False, False, False),
        # (a) A pane id with no server evidence: unknown.
        ("%3", None, None, None, None, None),
        # (b) The marker names this server but there is no pane id: the server is
        # known, the location and the pane are not.
        (None, THIS, None, True, None, None),
        # (c) The marker names another server while $TMUX names this one: unknown.
        ("%3", OTHER, _tmux_var(THIS), None, None, None),
        # The mirror conflict: marker says this server, $TMUX says another.
        ("%3", THIS, _tmux_var(OTHER), None, None, None),
        # Only $TMUX, and it names another server: incomplete, unknown.
        ("%3", None, _tmux_var(OTHER), None, None, None),
        # Only the marker, and it names another server: incomplete, unknown.
        ("%3", OTHER, None, None, None, None),
        # Marker and $TMUX agree on another server: provably outside.
        ("%3", OTHER, _tmux_var(OTHER), False, False, False),
        # Marker names this server, $TMUX absent: inside this server.
        ("%3", THIS, None, True, True, True),
        # $TMUX names this server, marker absent: inside this server.
        ("%3", None, _tmux_var(THIS), True, True, True),
        # Both agree on this server.
        ("%3", THIS, _tmux_var(THIS), True, True, True),
        # $TMUX present but unparsable is treated as absent.
        ("%3", THIS, ",1,0", True, True, True),
    ],
)
def test_caller_identity_truth_table(
    tmp_path, monkeypatch, pane, marker, tmux, server, location, is_caller
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace), socket=THIS)
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    for key, value in (("TMUX_PANE", pane), ("HIVE_IDE_TMUX_SOCKET", marker), ("TMUX", tmux)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(frame, "pane_ids", lambda _window_id: ["%2", "%3"])

    assert frame.caller_server() is server
    assert frame.caller_location("@7") is location
    assert frame.pane_is_caller("%3") is is_caller
    # A different pane is only ever "not the caller" once the caller is settled.
    assert frame.pane_is_caller("%4") is (False if is_caller is not None else None)


def test_caller_location_is_unknown_when_the_window_cannot_be_listed(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace), socket=THIS)
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", THIS)
    monkeypatch.setattr(frame, "pane_ids", lambda _window_id: None)

    assert frame.caller_server() is True
    assert frame.caller_location("@7") is None
    assert frame.pane_is_caller("%4") is False  # the pane verdict needs no listing


def test_tmux_variable_is_compared_by_full_socket_path(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace), socket=THIS)
    monkeypatch.setenv("TMUX_PANE", "%3")
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)
    monkeypatch.setattr(frame, "pane_ids", lambda _window_id: ["%3"])

    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    assert frame.socket_path() == f"/tmp/tmux-{os.getuid()}/{THIS}"
    monkeypatch.setenv("TMUX", _tmux_var(THIS))
    assert frame.caller_server() is True

    # Same basename on another tmux tmpdir is a different server: not a match,
    # and with nothing else to go on, unknown rather than outside.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "tmuxdir"))
    assert frame.socket_path() == f"{tmp_path / 'tmuxdir'}/tmux-{os.getuid()}/{THIS}"
    assert frame.caller_server() is None
    monkeypatch.setenv("TMUX", _tmux_var(THIS, tmpdir=str(tmp_path / "tmuxdir")))
    assert frame.caller_server() is True


# -- rebuild and the pane helpers obey the verdicts -------------------------------------


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


@pytest.mark.parametrize(
    ("label", "marker", "tmux"),
    [
        ("pane id without server evidence", None, None),
        ("marker names another server, $TMUX names this one", OTHER, "this"),
        ("marker names this server, $TMUX names another", "this", OTHER),
    ],
)
def test_unknown_caller_location_defers_rebuild_and_skips_pane_helpers(
    live, label, marker, tmux
):
    roles = live.frame.role_panes(live.record["id"])
    pids_before = _pane_pids(live.frame, roles)
    live.identity(
        pane=roles["agent"],
        marker=live.socket if marker == "this" else marker,
        tmux=(
            None
            if tmux is None
            else f"{live.frame.socket_path() if tmux == 'this' else '/tmp/tmux-0/' + tmux},1,0"
        ),
    )
    assert live.frame.caller_location(live.window) is None, label

    result = live.frame.rebuild(live.record)

    assert result == {
        "rebuilt": False,
        "deferred": True,
        "reason": "caller-location-unknown",
        "window": live.window,
    }
    assert _window_ids(live.frame) == [live.window]
    assert live.frame.respawn_agent(live.record, roles["agent"]) is False
    assert live.frame.respawn_role_pane(live.record, "plan", roles["plan"]) is False
    assert live.frame.respawn_role_pane(live.record, "sidebar", roles["sidebar"]) is False
    assert live.frame.refresh_plan_pane(live.record) is False
    assert live.frame.sidebar_refresh_target(live.record) is None
    assert _pane_pids(live.frame, roles) == pids_before


def test_marker_names_this_server_without_a_pane_id_defers(live):
    roles = live.frame.role_panes(live.record["id"])
    pids_before = _pane_pids(live.frame, roles)
    live.identity(pane=None, marker=live.socket, tmux=None)

    assert live.frame.rebuild(live.record)["reason"] == "caller-location-unknown"
    assert live.frame.respawn_agent(live.record, roles["agent"]) is False
    assert _window_ids(live.frame) == [live.window]
    assert _pane_pids(live.frame, roles) == pids_before


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
    assert "window: rebuilt for stale agent environment" in result["actions"]
    assert (
        "window: deferred rebuild completed (deferred for: stale agent environment)"
        in result["actions"]
    )
    replacement = live.frame.windows()[live.record["id"]]
    assert replacement != live.window
    assert live.window not in _window_ids(live.frame)
    assert replacement in _window_ids(live.frame)
    assert _marker(live.store, live.record["id"]) is None
    assert _stages(live.store, live.record["id"]) == [
        "planned",
        "deferred",
        "planned",
        "completed",
    ]


def test_marker_alone_never_rebuilds(live, monkeypatch):
    healthy = Frame.pane_hive_ide_env
    live.inside("agent")
    _stale_agent_env(monkeypatch)
    assert live.repair()["deferred"] == ["stale agent environment"]
    # The environment is healthy again: nothing warrants a rebuild any more.
    monkeypatch.setattr(Frame, "pane_hive_ide_env", healthy)
    live.outside()

    result = live.repair()

    assert result["rebuilt"] is False
    assert any(
        action.startswith("window: deferred rebuild no longer warranted; marker cleared")
        for action in result["actions"]
    )
    assert live.frame.windows()[live.record["id"]] == live.window
    assert _marker(live.store, live.record["id"]) is None


def test_marker_is_kept_when_the_outside_run_cannot_observe_the_panes(live, monkeypatch):
    SessionRepair.mark_deferred_rebuild(
        live.store, live.store.find_session(live.record["id"]), reason="earlier", op_id="op1"
    )
    live.outside()
    original = Frame.tmux

    def failing_role_listing(args, **kwargs):
        if args[:1] == ["list-panes"] and args[-1] == "#{@hive_ide_pane}\t#{pane_id}":
            return SimpleNamespace(returncode=1, stdout="", stderr="no server running")
        return original(live.frame, args, **kwargs)

    monkeypatch.setattr(live.frame, "tmux", failing_role_listing)

    result = live.repair()

    assert result["ok"] is True
    assert result["rebuilt"] is False
    assert not any("no longer warranted" in action for action in result["actions"])
    assert any(
        warning.startswith("deferred rebuild marker kept (deferred for: earlier)")
        for warning in result["warnings"]
    )
    assert _marker(live.store, live.record["id"])["reason"] == "earlier"
    monkeypatch.undo()
    assert live.frame.windows()[live.record["id"]] == live.window


def test_marker_is_kept_when_the_rebuild_itself_fails(live, monkeypatch):
    live.inside("agent")
    _stale_agent_env(monkeypatch)
    assert live.repair()["deferred"] == ["stale agent environment"]
    live.outside()
    monkeypatch.setattr(
        Frame,
        "build",
        lambda _self, _record: (_ for _ in ()).throw(HiveIdeError("replacement failed")),
    )

    result = live.repair()

    assert result["ok"] is False
    assert result["errors"] == ["replacement failed"]
    assert result["rebuilt"] is False
    assert _marker(live.store, live.record["id"])["reason"] == "stale agent environment"
    assert any(
        warning.startswith("deferred rebuild marker kept") and "replacement failed" in warning
        for warning in result["warnings"]
    )
    stages = _stages(live.store, live.record["id"])
    assert stages == ["planned", "deferred", "planned", "failed"]
    assert stages.count("failed") == 1
    assert live.frame.windows()[live.record["id"]] == live.window


def test_kill_window_failure_raises_and_leaves_both_windows(live, monkeypatch):
    live.outside()
    original = Frame.tmux

    def refusing_kill(args, **kwargs):
        if args[:1] == ["kill-window"]:
            return subprocess.CompletedProcess(args, 1, "", "kill refused")
        return original(live.frame, args, **kwargs)

    monkeypatch.setattr(live.frame, "tmux", refusing_kill)

    with pytest.raises(HiveIdeError) as raised:
        live.frame.rebuild(live.record)

    monkeypatch.undo()
    windows = _window_ids(live.frame)
    assert live.window in windows
    assert len(windows) == 2
    replacement = next(window for window in windows if window != live.window)
    assert live.window in str(raised.value)
    assert replacement in str(raised.value)
    assert "kill refused" in str(raised.value)


# -- deleted cwd, environment, unobservable panes -----------------------------------------


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
    pids_before = _pane_pids(live.frame, roles)

    live.inside("plan")
    assert live.frame.refresh_plan_pane(live.record) is False
    assert live.frame.respawn_role_pane(live.record, "plan", roles["plan"]) is False
    live.inside("agent")
    assert live.frame.respawn_agent(live.record, roles["agent"]) is False
    live.inside("sidebar")
    assert live.frame.refresh_sidebar_if_needed(live.record) is False

    assert _pane_pids(live.frame, roles) == pids_before


def test_unobservable_panes_never_authorize_a_rebuild(live, monkeypatch):
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


# -- a declined ensure() with no window must never build ----------------------------------


def test_declined_ensure_with_absent_window_never_builds(tmp_path, monkeypatch):
    """Transport-level stub: every tmux call is intercepted, so `windows()` is
    empty because `has-session` fails, not because it was mocked."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="GHOST",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["term"].resolve(
            name="GHOST", working_dir=str(workspace), conversation_reference=None
        ),
    )
    calls: list[list[str]] = []

    def stub_tmux(_self, args, **_kwargs):
        calls.append(list(args))
        if args[:1] == ["has-session"]:
            return subprocess.CompletedProcess(args, 1, "", "no server running")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(Frame, "tmux", stub_tmux)
    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)

    result = SessionRepair(store, Frame(store, socket="hive-ide-stub")).repair(record)

    assert result["ok"] is True
    assert result["rebuilt"] is False
    assert "window: absent; not built" in result["actions"]
    assert not any(
        call[:1] in (["new-session"], ["new-window"], ["kill-window"], ["respawn-pane"])
        for call in calls
    )
    assert _stages(store, record["id"]) == ["skipped"]


# -- deferred driver switch completes from outside ----------------------------------------


def test_deferred_driver_switch_completes_from_outside_via_driver_mismatch(
    live, monkeypatch, capsys
):
    inert = DriverRegistry(
        {
            **bundled_drivers(),
            "codex": CommandDriver(
                "codex", "Codex", ["hive-ide-absent-codex"], capabilities=("launch", "resume")
            ),
        }
    )
    monkeypatch.setattr("hive_ide.cli.configured_registry", lambda _config: inert)
    monkeypatch.setattr(
        CommandDriver,
        "detect",
        lambda self: SimpleNamespace(available=True, executable=self.command[0], detail=""),
    )
    roles = live.frame.role_panes(live.record["id"])
    assert Frame.driver_command_name(live.frame.agent_pane_start_command(live.record)) == "sh"
    live.inside("agent")

    assert main(
        [
            *live.base,
            "switch-driver",
            f"--session-id={live.record['id']}",
            "--driver=codex",
            f"--tmux-socket={live.socket}",
        ]
    ) == 0
    switched = json.loads(capsys.readouterr().out)

    assert switched["driver"]["id"] == "codex"
    assert switched["rebuild"]["deferred"] is True
    assert switched["rebuild"]["rebuilt"] is False
    assert (
        f"hive-ide repair --session-id {live.record['id']} --tmux-socket {live.socket}"
        in switched["rebuild"]["next_step"]
    )
    assert "outside the session window" in switched["rebuild"]["next_step"]
    assert _marker(live.store, live.record["id"])["reason"] == "driver-switch"
    assert live.frame.windows()[live.record["id"]] == live.window
    assert live.frame.role_panes(live.record["id"]) == roles

    # From inside, repair sees the mismatch but still cannot act on it.
    inside = live.repair(inert)
    assert inside["deferred"] == [
        "driver mismatch: pane runs sh, record launches hive-ide-absent-codex"
    ]
    assert live.frame.windows()[live.record["id"]] == live.window

    live.outside()
    assert main(
        [
            *live.base,
            "repair",
            f"--session-id={live.record['id']}",
            f"--tmux-socket={live.socket}",
        ]
    ) == 0
    finished = json.loads(capsys.readouterr().out)

    assert finished["ok"] is True
    assert finished["rebuilt"] is True
    assert (
        "window: rebuilt for driver mismatch: pane runs sh, record launches "
        "hive-ide-absent-codex"
    ) in finished["actions"]
    # The inside repair re-deferred and refreshed the marker's reason to the
    # latest cause, so the completion names the driver mismatch it rebuilt for.
    assert any(
        action.startswith("window: deferred rebuild completed (deferred for: driver mismatch")
        for action in finished["actions"]
    )
    replacement = live.frame.windows()[live.record["id"]]
    assert replacement != live.window
    assert live.window not in _window_ids(live.frame)
    assert _marker(live.store, live.record["id"]) is None
    record = live.store.find_session(live.record["id"])
    assert (
        Frame.driver_command_name(live.frame.agent_pane_start_command(record))
        == "hive-ide-absent-codex"
    )


# -- repair log -------------------------------------------------------------------------


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


def test_repair_log_append_rides_an_already_held_mutation_lock(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)

    assert store.mutation_lock_held() is False
    with store.mutation_lock():
        assert store.mutation_lock_held() is True
        # A nested entry must not deadlock on the lock this process already holds.
        SessionRepair.append_repair_log(store, "s", {"at": "t0", "stage": "skipped"})
        with store.mutation_lock():
            assert store.mutation_lock_held() is True
        assert store.mutation_lock_held() is True
    assert store.mutation_lock_held() is False
    assert len(store.read("repairs", "s")["entries"]) == 1
