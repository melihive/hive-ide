from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hive_ide import PROTOCOL_VERSION, SCHEMA_VERSION
from hive_ide.activity import IdeActivity
from hive_ide.adapters import (
    DefaultCommandSurface,
    DefaultPlanAdapter,
    DefaultWorkspaceAdapter,
)
from hive_ide.cli import main
from hive_ide.config import _editor_argv
from hive_ide.drivers import bundled_drivers
from hive_ide.errors import SchemaVersionError, UsageError
from hive_ide.environments import EnvironmentManager, managed_interpreter
from hive_ide.frame import Frame
from hive_ide.hook import IdeHook
from hive_ide.hooks import HookInstaller
from hive_ide.info import _card, _keys
from hive_ide.python_cmd import PythonCommand
from hive_ide.seen import IdeSeen
from hive_ide.sidebar import IdeSidebar
from hive_ide.source import resolve_source
from hive_ide.store import StateStore


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": "test"}


def _term() -> dict:
    return bundled_drivers()["term"].resolve(
        name="TEST", working_dir=str(Path.cwd()), conversation_reference=None
    )


def test_store_is_workspace_scoped_and_id_keyed(tmp_path):
    first = StateStore(tmp_path, tmp_path / "one")
    second = StateStore(tmp_path, tmp_path / "two")
    record = first.create_session(
        name="TEST",
        working_dir=tmp_path,
        source=_source(),
        driver=_term(),
    )

    assert first.find_session(record["id"])["name"] == "TEST"
    assert second.list("sessions") == []
    assert first.path("sessions", record["id"]).is_file()
    assert not list(first.collection("sessions").glob("*.tmp"))


def test_rename_archive_and_resume_keep_the_same_id_path(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="BEFORE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    session_id = record["id"]
    active_path = store.path("sessions", session_id)

    assert main(
        [
            "--state-home",
            str(store.home),
            "--workspace-key",
            store.workspace_key,
            "rename",
            "--session-id",
            session_id,
            "--name",
            "AFTER",
        ]
    ) == 0
    capsys.readouterr()
    assert active_path.is_file()
    assert store.find_session(session_id)["name"] == "AFTER"
    assert {path.name for path in store.collection("sessions").glob("*.json")} == {
        f"{session_id}.json"
    }

    store.archive_session(session_id)
    assert not active_path.exists()
    assert store.path("archive", session_id).is_file()
    store.resume_session(session_id)
    assert active_path.is_file()
    assert not store.path("archive", session_id).exists()
    assert store.find_session(session_id)["name"] == "AFTER"


def test_cli_resume_repairs_and_selects_restored_session(tmp_path, monkeypatch, capsys):
    import hive_ide.cli as cli_module

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="RESTORE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    session_id = record["id"]
    store.archive_session(session_id)
    calls = []

    class FakeFrame:
        def __init__(self, _store, *, socket=None):
            self.socket = socket

        def ensure(self, restored):
            calls.append(("ensure", restored["id"]))
            return True

        def bind_keys(self):
            calls.append(("bind_keys", None))

        def select_session(self, restored_id):
            calls.append(("select_session", restored_id))
            return True

    monkeypatch.setattr(cli_module, "Frame", FakeFrame)

    assert main(
        [
            "--state-home",
            str(store.home),
            "--workspace-key",
            store.workspace_key,
            "resume",
            "--session-id",
            session_id,
        ]
    ) == 0
    capsys.readouterr()
    assert calls == [
        ("ensure", session_id),
        ("bind_keys", None),
        ("select_session", session_id),
    ]
    assert store.find_session(session_id) is not None


def test_store_refuses_incompatible_schema(tmp_path):
    store = StateStore(tmp_path, tmp_path / "workspace")
    path = store.path("sessions", "bad")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION + 1,
                "workspace_key": store.workspace_key,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SchemaVersionError):
        store.read("sessions", "bad")


def test_missing_config_uses_safe_defaults(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    missing = tmp_path / "does-not-exist.json"
    assert main(
        [
            "--state-home",
            str(state),
            "--config",
            str(missing),
            "--workspace-key",
            str(workspace),
            "create",
            "--name",
            "DEFAULTS",
            "--driver",
            "term",
        ]
    ) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["driver"]["id"] == "term"
    assert record["source"]["kind"] == "stable"


def test_cli_help_lists_commands_with_professional_summaries(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Repository-scoped tmux IDE" in out
    assert "open" in out
    assert "Open the tmux IDE" in out
    assert "current-chat" in out
    assert "Focus or resume the current session's agent pane." in out
    assert "Examples:" in out


def test_cli_subcommand_help_describes_options(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["current-chat", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Focus or resume the current session's agent pane." in out
    assert "--session-id" in out
    assert "--tmux-socket" in out


def test_cli_quiet_suppresses_final_json_result(capsys):
    assert main(["--quiet", "version"]) == 0

    assert capsys.readouterr().out == ""


def test_cli_current_plan_is_quiet_on_success(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": "plan.md", "active_task": None},
    )

    monkeypatch.setattr(
        Frame,
        "current_plan",
        lambda _self, session, focus=False: {
            "session_id": session["id"],
            "opened": "plan-pane",
        },
    )

    assert (
        main(
            [
                "--state-home",
                str(state),
                "--workspace-key",
                str(workspace),
                "current-plan",
                "--session-id",
                record["id"],
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_cli_current_chat_is_quiet_on_success(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CHAT",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )

    monkeypatch.setattr(
        Frame,
        "current_chat",
        lambda _self, session: {
            "session_id": session["id"],
            "opened": "existing-agent-pane",
        },
    )

    assert (
        main(
            [
                "--state-home",
                str(state),
                "--workspace-key",
                str(workspace),
                "current-chat",
                "--session-id",
                record["id"],
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_open_bootstraps_empty_workspace(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"

    def fake_open(self, *, no_attach=False):
        return {"opened": len(self.store.list("sessions")), "no_attach": no_attach}

    monkeypatch.setattr("hive_ide.cli.Frame.open", fake_open)

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "open",
            "--no-attach",
        ]
    ) == 0
    opened = json.loads(capsys.readouterr().out)
    store = StateStore(state, workspace)
    sessions = store.list("sessions")
    assert opened == {"opened": 1, "no_attach": True}
    assert len(sessions) == 1
    assert sessions[0]["name"] == "workspace"
    assert sessions[0]["working_dir"] == str(workspace)
    assert sessions[0]["driver"]["id"] == "term"


def test_adopt_imports_claude_sessions_for_workspace(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    project = home / ".claude" / "projects" / ("-" + "-".join(workspace.resolve().parts[1:]))
    project.mkdir(parents=True)
    first = project / "11111111-1111-4111-8111-111111111111.jsonl"
    second = project / "22222222-2222-4222-8222-222222222222.jsonl"
    first.write_text(
        json.dumps(
            {
                "sessionId": "11111111-1111-4111-8111-111111111111",
                "timestamp": "2026-07-28T10:00:00.000Z",
                "type": "assistant",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(
            {
                "sessionId": "22222222-2222-4222-8222-222222222222",
                "customTitle": "Hive Events Allowlist",
                "timestamp": "2026-07-28T11:00:00.000Z",
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "text",
                            "text": "Latest Hive Events allowlist work",
                        }
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "adopt",
            "--driver",
            "claude",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["created"]) == 2
    store = StateStore(state, workspace)
    sessions = store.list("sessions")
    assert sorted(item["driver"]["resume"]["reference"] for item in sessions) == [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    newest = next(
        item
        for item in sessions
        if item["driver"]["resume"]["reference"]
        == "22222222-2222-4222-8222-222222222222"
    )
    assert newest["driver"]["launch_argv"] == [
        "claude",
        "--resume",
        "22222222-2222-4222-8222-222222222222",
    ]
    assert newest["name"] == "Hive Events Allowlist"
    assert newest["host"]["adopted"]["updated_at"] == "2026-07-28T11:00:00.000Z"

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "adopt",
            "--driver",
            "claude",
        ]
    ) == 0
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["created"] == []
    assert rerun["skipped_existing"] == 2


def test_adopt_dry_run_exposes_title_and_first_message(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    project = home / ".claude" / "projects" / ("-" + "-".join(workspace.resolve().parts[1:]))
    project.mkdir(parents=True)
    (project / "session-1.jsonl").write_text(
        json.dumps(
            {
                "sessionId": "session-1",
                "customTitle": "Real session title",
                "timestamp": "2026-07-28T10:00:00.000Z",
                "type": "summary",
            }
        )
        + "\n"
        + json.dumps(
            {
                "sessionId": "session-1",
                "timestamp": "2026-07-28T10:01:00.000Z",
                "type": "user",
                "message": {"content": "First useful message"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "sessionId": "session-1",
                "timestamp": "2026-07-28T10:02:00.000Z",
                "type": "user",
                "message": {"content": "Later message"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "adopt",
            "--driver",
            "claude",
            "--dry-run",
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)

    item = result["created"][0]
    assert item["title"] == "Real session title"
    assert item["label"] == "Real session title"
    assert item["name"] == "Real session title"
    assert item["preview"] == "First useful message"


def test_create_adopt_imports_most_recent_claude_session(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    project = home / ".claude" / "projects" / ("-" + "-".join(workspace.resolve().parts[1:]))
    project.mkdir(parents=True)
    for reference, timestamp in [
        ("old-session", "2026-07-28T10:00:00.000Z"),
        ("new-session", "2026-07-28T11:00:00.000Z"),
    ]:
        (project / f"{reference}.jsonl").write_text(
            json.dumps({"sessionId": reference, "timestamp": timestamp}) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(home))

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "create",
            "--driver",
            "claude",
            "--adopt",
        ]
    ) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["driver"]["resume"]["reference"] == "new-session"
    assert record["driver"]["launch_argv"] == ["claude", "--resume", "new-session"]


def test_create_adopt_can_target_named_claude_session(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    project = home / ".claude" / "projects" / ("-" + "-".join(workspace.resolve().parts[1:]))
    project.mkdir(parents=True)
    for reference, timestamp in [
        ("old-session", "2026-07-28T10:00:00.000Z"),
        ("new-session", "2026-07-28T11:00:00.000Z"),
    ]:
        (project / f"{reference}.jsonl").write_text(
            json.dumps({"sessionId": reference, "timestamp": timestamp}) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(home))

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "create",
            "--name",
            "ADOPTED",
            "--driver",
            "claude",
            "--adopt",
            "--reference",
            "old-session",
        ]
    ) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["name"] == "ADOPTED"
    assert record["driver"]["resume"]["reference"] == "old-session"


def test_adopt_discovers_codex_sessions_for_workspace(tmp_path, capsys, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    state = tmp_path / "state"
    root = home / ".codex" / "sessions" / "2026" / "07" / "28"
    root.mkdir(parents=True)
    for reference, cwd, timestamp in [
        ("old-codex", workspace, "2026-07-28T10:00:00.000Z"),
        ("new-codex", workspace, "2026-07-28T11:00:00.000Z"),
        ("other-codex", other, "2026-07-28T12:00:00.000Z"),
    ]:
        (root / f"rollout-{reference}.jsonl").write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": timestamp,
                    "payload": {
                        "cwd": str(cwd),
                        "session_id": reference,
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "response_item",
                    "timestamp": timestamp,
                    "payload": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": f"Working on {reference} preview",
                            }
                        ],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HOME", str(home))

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "adopt",
            "--driver",
            "codex",
            "--dry-run",
        ]
    ) == 0
    preview = json.loads(capsys.readouterr().out)
    assert [item["reference"] for item in preview["created"]] == [
        "new-codex",
        "old-codex",
    ]
    assert preview["created"][0]["preview"] == "Working on new-codex preview"

    assert main(
        [
            "--state-home",
            str(state),
            "--workspace-key",
            str(workspace),
            "create",
            "--name",
            "CODEX ADOPT",
            "--driver",
            "codex",
            "--adopt",
            "--reference",
            "old-codex",
        ]
    ) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["name"] == "CODEX ADOPT"
    assert record["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(workspace),
        "old-codex",
    ]


def test_editor_resolution_is_custom_then_micro_then_less(monkeypatch):
    monkeypatch.setenv("HIVE_IDE_EDITOR", "nvim --clean")
    assert _editor_argv({}) == ["nvim", "--clean"]
    assert _editor_argv({"editor": ["vim", "-f"]}) == ["vim", "-f"]

    monkeypatch.delenv("HIVE_IDE_EDITOR")
    monkeypatch.setattr("hive_ide.config.shutil.which", lambda name: "/bin/micro")
    assert _editor_argv({}) == ["micro"]
    monkeypatch.setattr("hive_ide.config.shutil.which", lambda name: None)
    assert _editor_argv({}) == ["less"]


def test_bundled_driver_capabilities_are_explicit():
    drivers = bundled_drivers()
    assert set(drivers) == {"claude", "codex", "antigravity", "term"}
    assert drivers["claude"].resolve(
        name="X", working_dir="/tmp", conversation_reference="abc"
    )["launch_argv"] == ["claude", "--resume", "abc"]
    assert drivers["codex"].resolve(
        name="X", working_dir="/tmp", conversation_reference="abc"
    )["launch_argv"] == ["codex", "resume", "-C", "/tmp", "abc"]
    assert drivers["antigravity"].resolve(
        name="X", working_dir="/tmp", conversation_reference="continue"
    )["resume"]["strategy"] == "workspace_continue"
    assert drivers["term"].resolve(
        name="X", working_dir="/tmp", conversation_reference=None
    )["capabilities"] == ["launch"]


def test_frame_internal_commands_use_selected_environment_modules(tmp_path):
    store = StateStore(tmp_path, tmp_path / "workspace")
    frame = Frame(store)
    command = frame._module("sidebar", ["--example"])
    assert " -m hive_ide.sidebar " in f" {command} "
    assert " -I " not in f" {command} "
    assert "hive-ide" not in shlex.split(command)[1:]
    sidebar = frame._sidebar_command(
        {
            "id": "abc",
            "name": "TEST",
            "working_dir": store.workspace_key,
            "source": {"interpreter": sys.executable},
        }
    )
    assert "--state-home" in sidebar
    assert "--workspace-key" in sidebar
    assert "--session-id abc" in sidebar


def test_cli_runs_from_normal_python_environment():
    result = subprocess.run(
        [sys.executable, "-m", "hive_ide.cli", "version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["protocol_version"] == PROTOCOL_VERSION
    assert document["schema_version"] == SCHEMA_VERSION


def test_python_command_centralizes_internal_launch_policy():
    assert PythonCommand.cli_argv(["version"], python="/py") == [
        "/py",
        "-m",
        "hive_ide.cli",
        "version",
    ]
    command = PythonCommand.module_command("sidebar", ["--example"], python="/py")
    assert command == "/py -m hive_ide.sidebar --example"
    assert " -I " not in f" {command} "


def test_hook_writes_status_and_conversation_reference(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["codex"]
    record = store.create_session(
        name="HOOK",
        working_dir=workspace,
        source=_source(),
        driver=driver.resolve(
            name="HOOK", working_dir=str(workspace), conversation_reference=None
        ),
    )
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)
    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            "waiting",
            "--driver",
            "codex",
            '{"thread-id":"conversation-1"}',
        ]
    ) == 0
    status = store.read("status", record["id"])
    assert status["state"] == "waiting"
    assert status["conversation_reference"] == "conversation-1"
    updated = store.find_session(record["id"])
    assert updated["driver"]["resume"]["reference"] == "conversation-1"
    assert updated["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(workspace),
        "conversation-1",
    ]


def test_hook_relay_uses_tmux_server_when_available(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="RELAY",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", "hive-ide-test")
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "config.json"))
    monkeypatch.setattr("hive_ide.hook.subprocess.run", fake_run)

    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            "waiting",
            "--driver",
            "term",
            "{}",
        ]
    ) == 0
    assert store.read("status", record["id"]) is None
    argv, kwargs = calls[0]
    assert argv[:5] == ["tmux", "-L", "hive-ide-test", "run-shell", "-b"]
    assert kwargs["capture_output"] is True
    command = argv[5]
    assert "HIVE_IDE_WORKSPACE_KEY=" in command
    assert "HIVE_IDE_SESSION_ID=" in command
    assert "--state waiting --driver term --relayed '{}'" in command

    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET")
    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            "waiting",
            "--driver",
            "term",
            "--relayed",
            "{}",
        ]
    ) == 0
    assert store.read("status", record["id"])["state"] == "waiting"


def test_hook_identity_survives_display_name_change(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="BEFORE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    record["name"] = "AFTER"
    store.write("sessions", record["id"], record)
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setenv("HIVE_IDE_SESSION", "BEFORE")
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)

    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            "working",
            "--driver",
            "term",
            "{}",
        ]
    ) == 0
    status = store.read("status", record["id"])
    assert status["session_id"] == record["id"]
    assert store.find_session(record["id"])["name"] == "AFTER"


def test_compaction_hooks_set_and_clear_activity(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="COMPACT",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)

    common = [
        "--state-home",
        str(store.home),
        "--driver",
        "term",
    ]
    assert IdeHook.main([*common, "--activity", "compacting"]) == 0
    activity = store.read("activity", record["id"])
    assert activity["kind"] == "compacting"
    assert activity["state"] == "running"
    assert activity["label"] == "Compacting context"

    assert IdeHook.main([*common, "--activity", "clear"]) == 0
    assert store.read("activity", record["id"]) is None


def test_info_modal_card_uses_framed_sidebar_style(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = workspace / "plans" / "feature.md"
    plan.parent.mkdir()
    plan.write_text(
        "# Feature Plan\n\n> - **Kind:** feat\n\n- [x] one\n- [ ] two\n",
        encoding="utf-8",
    )
    record = {
        "id": "abc123",
        "name": "HIVE IDE",
        "working_dir": str(workspace),
        "last_active": "2026-07-28T00:00:00+00:00",
        "driver": {"id": "codex"},
        "plan": {"path": "plans/feature.md"},
        "source": {"kind": "dev", "version": "1.0.0"},
    }
    snapshot = {"sidebar": {"icons": {"drivers": {"codex": "◎"}}}}

    rendered = "\n".join(_card(record, snapshot))

    assert rendered.startswith("╭")
    assert "Hive IDE  HIVE IDE" in rendered
    assert "◎ agent    codex" in rendered
    assert "📁 folder   workspace" in rendered
    assert "📝 plan     Feature Plan" in rendered
    assert "status   feat · 50%" in rendered
    assert "path     plans/feature.md" in rendered


def test_info_modal_keys_match_old_prefix_map_style():
    rendered = "\n".join(_keys({"keys": {"bindings": {"card": "I"}}}))

    assert rendered.startswith("╭")
    assert "Hive IDE shortcuts   (under your tmux prefix)" in rendered
    assert "<prefix> I   session card" in rendered


def test_current_plan_and_conversation_mutations_are_id_targeted(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="TARGET",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    plan = workspace / "plans" / "example.md"
    plan.parent.mkdir()
    plan.write_text("# Example\n", encoding="utf-8")

    assert main(["current"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == record["id"]

    assert main(
        [
            "plan-set",
            f"--session-id={record['id']}",
            "--path=plans/example.md",
            "--active-task=Build",
        ]
    ) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["plan"] == {
        "path": "plans/example.md",
        "active_task": "Build",
    }

    assert main(
        [
            "attach-conversation",
            f"--session-id={record['id']}",
            "--driver=codex",
            "--reference=conversation-1",
        ]
    ) == 0
    attached = json.loads(capsys.readouterr().out)
    assert attached["id"] == record["id"]
    assert attached["driver"]["resume"]["reference"] == "conversation-1"
    assert attached["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(workspace),
        "conversation-1",
    ]

    assert main(
        [
            "status-event",
            f"--session-id={record['id']}",
            "--state=working",
            "--driver=codex",
            "--subagents-running=4",
        ]
    ) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["subagents"] == {"running": 4}
    assert status["subagents_running"] == 4


def test_current_plan_opens_linked_file_outside_the_frame(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    plan = workspace / "plans" / "example.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Example\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/example.md", "active_task": None},
    )
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {})
    monkeypatch.setenv("HIVE_IDE_EDITOR", "editor --wait")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hive_ide.frame.subprocess.run", fake_run)
    result = frame.current_plan(record)

    assert result["opened"] == "terminal"
    assert calls == [(["editor", "--wait", str(plan)], {})]


def test_current_chat_uses_recorded_resume_command_outside_frame(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CHAT",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hive_ide.frame.subprocess.run", fake_run)
    result = frame.current_chat(record)

    assert result == {
        "session_id": record["id"],
        "driver": "codex",
        "opened": "terminal",
    }
    assert calls == [
        (
            ["codex", "resume", "-C", str(workspace), "conversation-1"],
            {"cwd": str(workspace)},
        )
    ]


def test_current_chat_selects_existing_agent_pane_without_respawning(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CHAT",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    frame = Frame(store)
    calls = []
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    monkeypatch.setattr(
        frame,
        "tmux",
        lambda args, **_kwargs: calls.append(args)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = frame.current_chat(record)

    assert result == {
        "session_id": record["id"],
        "driver": "codex",
        "opened": "existing-agent-pane",
    }
    assert calls == [["select-pane", "-t", "%2"]]


def test_frame_select_session_selects_window_then_agent_pane(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="TARGET",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    frame = Frame(store)
    calls = []
    monkeypatch.setattr(frame, "windows", lambda: {record["id"]: "@7"})
    monkeypatch.setattr(
        frame,
        "tmux",
        lambda args, **_kwargs: calls.append(args)
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert frame.select_session(record["id"])
    assert calls[0] == ["select-window", "-t", "@7"]
    assert calls[-1] == ["select-pane", "-t", "@7.1"]


def test_stable_source_patch_upgrade_refreshes_session_record(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STALE",
        working_dir=workspace,
        source={
            "kind": "stable",
            "interpreter": sys.executable,
            "version": "1.0.8",
        },
        driver=_term(),
    )
    frame = Frame(store)
    monkeypatch.setattr(
        "hive_ide.frame.inspect_interpreter",
        lambda _interpreter: {
            "package_version": "1.0.10",
            "protocol_version": 1,
            "schema_version": 1,
        },
    )

    frame._refresh_source_if_needed(record, sys.executable)

    assert record["source"]["version"] == "1.0.10"
    assert store.find_session(record["id"])["source"]["version"] == "1.0.10"


def test_workspace_refresh_updates_stable_sources_without_touching_dev(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    stable_active = store.create_session(
        name="STABLE",
        working_dir=str(workspace),
        source={
            "kind": "stable",
            "interpreter": sys.executable,
            "version": "1.0.10",
        },
        driver=bundled_drivers()["term"].resolve(
            name="STABLE", working_dir=str(workspace), conversation_reference=None
        ),
    )
    stable_archived = store.create_session(
        name="ARCHIVE",
        working_dir=str(workspace),
        source={
            "kind": "stable",
            "interpreter": sys.executable,
            "version": "1.0.10",
        },
        driver=bundled_drivers()["term"].resolve(
            name="ARCHIVE", working_dir=str(workspace), conversation_reference=None
        ),
    )
    store.archive_session(stable_archived["id"])
    dev = store.create_session(
        name="DEV",
        working_dir=str(workspace),
        source={"kind": "dev", "interpreter": sys.executable, "version": "dev-old"},
        driver=bundled_drivers()["term"].resolve(
            name="DEV", working_dir=str(workspace), conversation_reference=None
        ),
    )
    monkeypatch.setattr(
        "hive_ide.source.inspect_interpreter",
        lambda _interpreter: {
            "interpreter": sys.executable,
            "package_version": "1.0.11",
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
    )

    result = store.refresh_stable_sources(collections=("sessions", "archive"))

    assert sorted(result["refreshed"]) == sorted(
        [stable_active["id"], stable_archived["id"]]
    )
    assert store.find_session(stable_active["id"])["source"]["version"] == "1.0.11"
    assert (
        store.find_session(stable_archived["id"], archived=True)["source"]["version"]
        == "1.0.11"
    )
    assert store.find_session(dev["id"])["source"]["version"] == "dev-old"


def test_list_self_heals_stable_source_metadata(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    record = store.create_session(
        name="STABLE",
        working_dir=str(workspace),
        source={
            "kind": "stable",
            "interpreter": sys.executable,
            "version": "1.0.10",
        },
        driver=bundled_drivers()["term"].resolve(
            name="STABLE", working_dir=str(workspace), conversation_reference=None
        ),
    )
    monkeypatch.setattr(
        "hive_ide.source.inspect_interpreter",
        lambda _interpreter: {
            "interpreter": sys.executable,
            "package_version": "1.0.11",
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
    )

    assert main(["--state-home", str(state), "--workspace-key", str(workspace), "ls"]) == 0
    result = json.loads(capsys.readouterr().out)

    assert result[0]["source"]["version"] == "1.0.11"
    assert store.find_session(record["id"])["source"]["version"] == "1.0.11"


def test_dev_source_version_skew_stays_loud(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="DEV",
        working_dir=workspace,
        source={
            "kind": "dev",
            "interpreter": sys.executable,
            "version": "1.0.8",
        },
        driver=_term(),
    )
    frame = Frame(store)
    monkeypatch.setattr(
        "hive_ide.frame.inspect_interpreter",
        lambda _interpreter: {
            "package_version": "1.0.10",
            "protocol_version": 1,
            "schema_version": 1,
        },
    )

    with pytest.raises(UsageError, match="source version changed"):
        frame._refresh_source_if_needed(record, sys.executable)


def test_purge_requires_confirmation_and_removes_all_session_state(
    tmp_path, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="PURGE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    base = [
        "--state-home",
        str(store.home),
        "--workspace-key",
        store.workspace_key,
        "purge",
        f"--session-id={record['id']}",
        "--tmux-socket=hive-ide-test-purge",
    ]

    assert main(base) == 2
    assert "requires --confirm" in capsys.readouterr().err
    assert store.find_session(record["id"]) is not None

    store.write(
        "status",
        record["id"],
        {
            "schema_version": 1,
            "workspace_key": store.workspace_key,
            "session_id": record["id"],
        },
    )
    assert main([*base, "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["purged"] is True
    assert all(
        not store.path(collection, record["id"]).exists()
        for collection in store.COLLECTIONS
    )


def test_workspace_lock_serializes_cli_mutations(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    env = dict(os.environ)
    env["HIVE_IDE_CONFIG"] = str(tmp_path / "missing-config.json")
    command = [
        sys.executable,
        "-m",
        "hive_ide.cli",
        "--state-home",
        str(store.home),
        "--workspace-key",
        store.workspace_key,
        "create",
        "--name=LOCKED",
        "--driver=term",
        f"--source={sys.executable}",
    ]

    with store.mutation_lock():
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.2)
        assert process.poll() is None

    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["name"] == "LOCKED"


def test_seen_acknowledges_waiting_status(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="SEEN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    store.write(
        "status",
        record["id"],
        {
            "schema_version": 1,
            "session_id": record["id"],
            "workspace_key": store.workspace_key,
            "state": "waiting",
            "driver": "term",
            "conversation_reference": None,
            "observed_at": "2026-07-23T00:00:00+00:00",
        },
    )
    assert IdeSeen.mark(store.home, store.workspace_key, record["id"])
    assert store.read("status", record["id"])["state"] == "idle"


def test_activity_is_environment_gated(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="ACTIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    for key in ("HIVE_IDE_STATE_HOME", "HIVE_IDE_WORKSPACE_KEY", "HIVE_IDE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    assert not IdeActivity.mark(IdeActivity.KIND_TASK)

    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    assert IdeActivity.mark(IdeActivity.KIND_TASK, label="Tests")
    assert store.read("activity", record["id"])["label"] == "Tests"
    assert IdeActivity.clear()


def test_default_adapters_are_complete_noops(tmp_path):
    workspace = DefaultWorkspaceAdapter().resolve(tmp_path)
    assert workspace.key == str(tmp_path.resolve())
    plan = DefaultPlanAdapter().resolve(None, workspace)
    assert plan == {"path": None, "active_task": None}
    assert DefaultPlanAdapter().active_task(plan) is None
    assert DefaultCommandSurface().before_create(workspace, "TEST") is None
    assert DefaultCommandSurface().after_archive({}) is None


def test_create_uses_the_configured_default_source(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "default_source": "dev",
                "sources": {"dev": {"interpreter": sys.executable}},
            }
        ),
        encoding="utf-8",
    )
    assert main(
        [
            "--state-home",
            str(tmp_path / "state"),
            "--config",
            str(config),
            "--workspace-key",
            str(workspace),
            "create",
            "--name=DEV DEFAULT",
            "--driver=term",
        ]
    ) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["source"]["kind"] == "dev"


def test_explicit_source_is_validated_in_isolated_mode():
    source = resolve_source(
        sys.executable, {}, default_interpreter="/not-used"
    )
    assert source["kind"] == "explicit"
    assert source["interpreter"] == sys.executable


def test_environment_setup_creates_stable_and_editable_dev(monkeypatch, tmp_path):
    calls = []
    manager = EnvironmentManager(tmp_path / "envs")
    dev = tmp_path / "checkout"
    dev.mkdir()
    (dev / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")

    def fake_run(argv, *, purpose):
        calls.append((argv, purpose))
        if argv[1:3] == ["-m", "venv"]:
            target = Path(argv[-1])
            (target / "bin").mkdir(parents=True)
            (target / "bin" / "python").touch()

    monkeypatch.setattr(manager, "_run", fake_run)
    monkeypatch.setattr(
        "hive_ide.environments.inspect_interpreter",
        lambda interpreter: {
            "interpreter": str(interpreter),
            "package_version": "1.2.3",
        },
    )
    result = manager.setup(stable_spec="hive-ide==1.2.3", dev_checkout=dev)

    assert result["stable"]["interpreter"] == str(
        managed_interpreter("stable", manager.home)
    )
    assert result["dev"]["editable"] is True
    installs = [argv for argv, _ in calls if "pip" in argv]
    assert installs == [
        [
            str(managed_interpreter("stable", manager.home)),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "hive-ide==1.2.3",
        ],
        [
            str(managed_interpreter("dev", manager.home)),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--editable",
            str(dev),
        ],
    ]


def test_environment_setup_force_refreshes_local_artifacts(monkeypatch, tmp_path):
    manager = EnvironmentManager(tmp_path / "envs")
    interpreter = tmp_path / "envs" / "stable" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()
    wheel = tmp_path / "hive_ide-0.1.0.dev0-py3-none-any.whl"
    wheel.touch()
    calls = []

    monkeypatch.setattr(
        manager,
        "_run",
        lambda argv, *, purpose: calls.append((argv, purpose)),
    )
    monkeypatch.setattr(
        "hive_ide.environments.inspect_interpreter",
        lambda selected: {
            "interpreter": str(selected),
            "package_version": "0.1.0.dev0",
        },
    )

    manager.install("stable", str(wheel))
    assert calls[0][0] == [
        str(interpreter),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--force-reinstall",
        str(wheel),
    ]


def test_dev_flip_changes_only_the_target_session(monkeypatch, tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "sources": {
                    "stable": {"interpreter": "/managed/stable/python"},
                    "dev": {"interpreter": "/managed/dev/python"},
                }
            }
        ),
        encoding="utf-8",
    )
    store = StateStore(state, workspace)
    first = store.create_session(
        name="FIRST",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/managed/stable/python", "version": "1"},
        driver=_term(),
    )
    second = store.create_session(
        name="SECOND",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/managed/stable/python", "version": "1"},
        driver=_term(),
    )
    second_path = store.path("sessions", second["id"])
    untouched = second_path.read_bytes()
    first_last_active = first["last_active"]
    rebuilt = []
    monkeypatch.setattr(
        "hive_ide.source.inspect_interpreter",
        lambda interpreter: {
            "interpreter": interpreter,
            "package_version": "2" if "dev" in interpreter else "1",
        },
    )
    monkeypatch.setattr(
        "hive_ide.cli.Frame.rebuild",
        lambda _frame, record: rebuilt.append(record["id"]),
    )

    common = [
        "--state-home",
        str(state),
        "--config",
        str(config),
        "--workspace-key",
        str(workspace),
        "source-set",
        "--session-id",
        first["id"],
    ]
    assert main([*common, "--source", "dev"]) == 0
    capsys.readouterr()
    assert store.find_session(first["id"])["source"] == {
        "kind": "dev",
        "interpreter": "/managed/dev/python",
        "version": "2",
    }
    assert store.find_session(first["id"])["last_active"] == first_last_active
    assert second_path.read_bytes() == untouched

    assert main([*common, "--source", "stable"]) == 0
    capsys.readouterr()
    assert store.find_session(first["id"])["source"]["kind"] == "stable"
    assert store.find_session(first["id"])["last_active"] == first_last_active
    assert second_path.read_bytes() == untouched
    assert rebuilt == [first["id"], first["id"]]

    assert main([*common, "--source", "dev", "--no-rebuild"]) == 0
    capsys.readouterr()
    assert store.find_session(first["id"])["source"]["kind"] == "dev"
    assert store.find_session(first["id"])["last_active"] == first_last_active
    assert second_path.read_bytes() == untouched
    assert rebuilt == [first["id"], first["id"]]


def test_source_set_repairs_session_with_missing_working_dir(monkeypatch, tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "missing"
    state = tmp_path / "state"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"sources": {"stable": {"interpreter": sys.executable}}}),
        encoding="utf-8",
    )
    store = StateStore(state, workspace)
    record = store.create_session(
        name="STALE",
        working_dir=missing,
        source={"kind": "dev", "interpreter": sys.executable, "version": "old"},
        driver=_term(),
    )
    monkeypatch.setattr(
        "hive_ide.cli.Frame.rebuild",
        lambda _frame, _record: pytest.fail("stale sessions must not rebuild"),
    )

    assert (
        main(
            [
                "--state-home",
                str(state),
                "--config",
                str(config),
                "--workspace-key",
                str(workspace),
                "source-set",
                "--session-id",
                record["id"],
                "--source",
                "stable",
            ]
        )
        == 0
    )
    capsys.readouterr()
    updated = store.find_session(record["id"])
    assert updated["source"]["kind"] == "stable"
    assert updated["source"]["interpreter"] == sys.executable


def test_hook_setup_merges_preserves_and_is_idempotent(monkeypatch, tmp_path):
    home = tmp_path / "home"
    stable = tmp_path / "stable" / "bin" / "python"
    stable.parent.mkdir(parents=True)
    stable.touch()
    claude = home / ".claude" / "settings.json"
    claude.parent.mkdir(parents=True)
    claude.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "echo keep"},
                                {
                                    "type": "command",
                                    "command": (
                                        "/old/python -m hive_ide.hook "
                                        "--state waiting --driver claude || true"
                                    ),
                                },
                            ]
                        }
                    ]
                },
                "other": {"kept": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hive_ide.hooks.inspect_interpreter",
        lambda _interpreter: {
            "interpreter": str(stable),
            "package_version": "1.2.3",
        },
    )
    installer = HookInstaller(
        home=home,
        stable_python=stable,
        selected_state_home=tmp_path / "state",
    )

    preview = installer.setup(apply=False)
    assert len(preview["changes"]) == 2
    assert not (home / ".codex" / "hooks.json").exists()
    assert json.loads(claude.read_text(encoding="utf-8"))["other"] == {"kept": True}

    applied = installer.setup(apply=True)
    assert len(applied["changes"]) == 2
    assert applied["codex_trust_required"] is True
    merged = json.loads(claude.read_text(encoding="utf-8"))
    commands = [
        handler["command"]
        for groups in merged["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert "echo keep" in commands
    assert not any("/old/python" in command for command in commands)
    assert all(
        command.endswith("|| true")
        for command in commands
        if "-m hive_ide.hook" in command
    )
    precompact_commands = [
        handler["command"]
        for group in merged["hooks"]["PreCompact"]
        for handler in group["hooks"]
    ]
    assert any("--activity compacting" in command for command in precompact_commands)
    assert merged["other"] == {"kept": True}
    assert claude.with_suffix(".json.hive-ide.bak").is_file()
    assert installer.setup(apply=True)["changes"] == []
    assert installer.verify() == []
    merged["hooks"].pop("Notification")
    claude.write_text(json.dumps(merged), encoding="utf-8")
    assert any("Notification" in finding for finding in installer.verify())


def test_hook_setup_rejects_malformed_existing_config(monkeypatch, tmp_path):
    home = tmp_path / "home"
    path = home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    stable = tmp_path / "python"
    stable.touch()
    monkeypatch.setattr(
        "hive_ide.hooks.inspect_interpreter",
        lambda _interpreter: {
            "interpreter": str(stable),
            "package_version": "1.2.3",
        },
    )
    installer = HookInstaller(home=home, stable_python=stable)
    with pytest.raises(UsageError, match="not valid JSON"):
        installer.setup(apply=False)


def test_hook_verify_reports_a_missing_stable_interpreter(tmp_path):
    findings = HookInstaller(
        home=tmp_path / "home",
        stable_python=tmp_path / "missing" / "python",
    ).verify()
    assert len(findings) == 1
    assert "not executable" in findings[0]


def test_skill_definition_installs_to_an_explicit_target(tmp_path, capsys):
    target = tmp_path / "skills" / "hive-ide" / "SKILL.md"
    assert main(["skill-install", "--target", str(target)]) == 0
    assert target.is_file()
    assert "name: hive-ide" in target.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["installed"] == str(target)


def test_session_error_has_sidebar_priority(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="ERROR",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    store.write(
        "errors",
        record["id"],
        {
            "schema_version": 1,
            "workspace_key": store.workspace_key,
            "session_id": record["id"],
            "component": "driver:test",
            "summary": "failed",
            "detail": "",
            "retryable": True,
            "recovery": "retry",
            "observed_at": "2026-07-23T00:00:00+00:00",
        },
    )
    legacy = {**record, "repo": store.workspace_key}
    assert IdeSidebar._status_dot(store.home, legacy)[0] == "!"
