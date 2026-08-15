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
from hive_ide.errors import HiveIdeError, SchemaVersionError, UsageError
from hive_ide.environments import EnvironmentManager, managed_interpreter
from hive_ide.frame import Frame
from hive_ide.hook import IdeHook
from hive_ide.hooks import HookInstaller
from hive_ide.info import _card, _keys
from hive_ide.python_cmd import PythonCommand
from hive_ide.repair import SessionRepair
from hive_ide.seen import IdeSeen
from hive_ide.sidebar import IdeSidebar
from hive_ide.source import resolve_source
from hive_ide.store import StateStore
from hive_ide.workspace_map import WorkspaceMap


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": "test"}


def _term() -> dict:
    return bundled_drivers()["term"].resolve(
        name="TEST", working_dir=str(Path.cwd()), conversation_reference=None
    )


def test_terminal_title_uses_workspace_name_without_ssh_suffix(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_IDE_HOST_NAME", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.setenv("HOSTNAME", "vivo")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    frame = Frame(StateStore(tmp_path / "state", workspace))

    assert frame._terminal_title() == "workspace IDE"


def test_terminal_title_appends_configured_ssh_host_name(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_IDE_HOST_NAME", "vivo.tailnet.example")
    monkeypatch.setenv("SSH_CONNECTION", "100.64.12.34 51322 100.64.1.10 22")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    frame = Frame(StateStore(tmp_path / "state", workspace))

    assert frame._terminal_title() == "workspace IDE vivo"


def test_terminal_title_appends_local_host_name_for_ssh_session(tmp_path, monkeypatch):
    monkeypatch.delenv("HIVE_IDE_HOST_NAME", raising=False)
    monkeypatch.setenv("SSH_CONNECTION", "100.64.12.34 51322 100.64.1.10 22")
    monkeypatch.setenv("HOSTNAME", "vivo")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    frame = Frame(StateStore(tmp_path / "state", workspace))

    assert frame._terminal_title() == "workspace IDE vivo"


def test_pane_titles_use_workspace_session_and_plan_heading(tmp_path):
    workspace = tmp_path / "sample-project"
    workspace.mkdir()
    plan = workspace / "plans" / "team" / "Simon" / "plans" / "hive" / "feat" / "hive-ide-pkg.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Hive IDE Package\n\n## Why\n\n## Tasks\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="HIVE IDE PYPI",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": str(plan.relative_to(workspace)), "active_task": None},
    )

    assert Frame(store)._pane_titles(record) == {
        "sidebar": "sample-project",
        "agent": "HIVE IDE PYPI",
        "plan": "Hive IDE Package",
    }


def test_pane_titlebars_highlight_only_the_active_title(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace))
    calls: list[list[str]] = []

    def fake_tmux(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    frame._normalize_pane_titlebars()

    assert ["set-option", "-g", "pane-border-status", "top"] in calls
    assert ["set-option", "-g", "pane-active-border-style", "fg=colour51"] in calls
    assert ["set-option", "-g", "pane-border-style", "fg=colour238"] in calls
    assert any(
        call[:3] == ["set-option", "-g", "pane-border-format"]
        and "#{?pane_active,#[fg=colour16;bg=colour51;bold],#[fg=colour244]}" in call[3]
        and "#{@hive_ide_title}" in call[3]
        and "#{pane_title}" not in call[3]
        and " > " not in call[3]
        for call in calls
    )


def test_pane_titlebar_format_keeps_title_in_active_branch(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace))
    calls: list[list[str]] = []

    def fake_tmux(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    frame._normalize_pane_titlebars()
    fmt = next(call[3] for call in calls if call[:3] == ["set-option", "-g", "pane-border-format"])
    active_branch = fmt.split(",", 1)[0]

    assert "#{@hive_ide_title}" not in active_branch
    assert "#{@hive_ide_title}" in fmt
    assert fmt.count("#{@hive_ide_title}") == 1
    assert "#[fg=colour16,bg=colour51,bold]" not in fmt
    assert "#[fg=colour16;bg=colour51;bold]" in fmt


def test_pane_titlebar_format_does_not_use_shell_mutable_title(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace))
    calls: list[list[str]] = []

    def fake_tmux(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    frame._normalize_pane_titlebars()

    assert any(
        call[:3] == ["set-option", "-g", "pane-border-format"]
        and "#[fg=colour244]" in call[3]
        and "#{@hive_ide_title}" in call[3]
        and "#{pane_title}" not in call[3]
        for call in calls
    )


def test_workspace_map_lists_known_workspaces_and_missing_dirs(tmp_path, capsys):
    first = tmp_path / "repos" / "one"
    second = tmp_path / "repos" / "two"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    store = StateStore(tmp_path / "state", first)
    store.create_session(
        name="FIRST",
        working_dir=first,
        source=_source(),
        driver=_term(),
    )
    missing = second / "gone"
    other = StateStore(tmp_path / "state", second)
    other.create_session(
        name="BROKEN",
        working_dir=missing,
        source=_source(),
        driver=_term(),
    )

    data = WorkspaceMap(tmp_path / "state").build()

    assert data["totals"]["workspaces"] == 2
    assert data["totals"]["active_sessions"] == 2
    assert data["totals"]["missing_dirs"] == 1

    text = WorkspaceMap(tmp_path / "state").render()
    assert "one" in text
    assert "BROKEN" in text
    assert "missing-dir" in text

    assert main(
        [
            "--state-home",
            str(tmp_path / "state"),
            "map",
            "--root",
            str(first.parent),
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "FIRST" in out
    assert "BROKEN" in out

    assert main(
        [
            "--state-home",
            str(tmp_path / "state"),
            "map",
            "--workspace",
            str(first),
        ]
    ) == 0
    out = capsys.readouterr().out
    assert "FIRST" in out
    assert "BROKEN" not in out


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


def test_store_drops_dead_legacy_plan_without_touching_live_legacy_keys(tmp_path):
    store = StateStore(tmp_path, tmp_path / "workspace")
    record = {
        "schema_version": SCHEMA_VERSION,
        "id": "session-id",
        "name": "SESSION",
        "workspace_key": store.workspace_key,
        "working_dir": store.workspace_key,
        "source": _source(),
        "driver": _term(),
        "plan": {"path": "plans/canonical.md", "active_task": None},
        "host": {
            "hive": {
                "legacy_record": {
                    "plan": "plans/stale.md",
                    "plan_status": "merged",
                    "subagents": {"running": 2},
                    "worktree_merged": True,
                }
            }
        },
    }

    store.write("sessions", record["id"], record)
    stored = store.read("sessions", record["id"])

    legacy = stored["host"]["hive"]["legacy_record"]
    assert "plan" not in legacy
    assert legacy["plan_status"] == "merged"
    assert legacy["subagents"] == {"running": 2}
    assert legacy["worktree_merged"] is True


def test_repair_all_prunes_dead_legacy_plan_from_active_and_archive(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    active = store.create_session(
        name="ACTIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    archived = store.create_session(
        name="ARCHIVED",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    store.archive_session(archived["id"])
    active["host"] = {
        "hive": {
            "legacy_record": {"plan": "stale-a.md", "subagents": {"running": 1}}
        }
    }
    archived["host"] = {
        "hive": {
            "legacy_record": {"plan": "stale-b.md", "plan_status": "done"}
        }
    }
    store.path("sessions", active["id"]).write_text(
        json.dumps(active), encoding="utf-8"
    )
    store.path("archive", archived["id"]).write_text(
        json.dumps(archived), encoding="utf-8"
    )
    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: True)

    result = SessionRepair(store, Frame(store, socket="test")).repair_all()

    assert result["ok"] is True
    assert result["pruned_legacy_plans"] == [
        {"collection": "sessions", "session_id": active["id"]},
        {"collection": "archive", "session_id": archived["id"]},
    ]
    active_legacy = store.find_session(active["id"])["host"]["hive"]["legacy_record"]
    archive_legacy = store.find_session(archived["id"], archived=True)["host"]["hive"]["legacy_record"]
    assert "plan" not in active_legacy
    assert active_legacy["subagents"] == {"running": 1}
    assert "plan" not in archive_legacy
    assert archive_legacy["plan_status"] == "done"


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


def test_rename_refreshes_claude_display_name_in_launch_command(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="BEFORE",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="BEFORE",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )

    assert main(
        [
            "--state-home",
            str(store.home),
            "--workspace-key",
            store.workspace_key,
            "rename",
            "--session-id",
            record["id"],
            "--name",
            "AFTER",
        ]
    ) == 0
    capsys.readouterr()

    updated = store.find_session(record["id"])
    assert updated["driver"]["launch_argv"] == [
        "claude",
        "--resume",
        "conversation-1",
        "--name",
        "AFTER",
    ]


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
    assert "chat" in out
    assert "Focus or resume the current session's agent pane." in out
    assert "force-rebuild" in out
    assert "Rebuild one tmux window" not in out
    assert "ensure" not in out
    assert "Examples:" in out


def test_cli_subcommand_help_describes_options(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["chat", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Focus or resume the current session's agent pane." in out
    assert "--session-id" in out
    assert "--tmux-socket" in out


def test_relayout_help_is_frame_level_not_session_level(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["relayout", "--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "Reapply or adopt the tmux frame geometry." in out
    assert "--mode" in out
    assert "--tmux-socket" in out
    assert "--session-id" not in out


def test_cli_quiet_suppresses_final_json_result(capsys):
    assert main(["--quiet", "version"]) == 0

    assert capsys.readouterr().out == ""


def test_cli_quiet_is_accepted_after_subcommand(capsys):
    assert main(["version", "--quiet"]) == 0

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

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {})
    focus_values = []

    def fake_current_plan(_self, session, focus=False):
        focus_values.append(focus)
        return {
            "session_id": session["id"],
            "opened": "plan-pane",
        }

    monkeypatch.setattr(Frame, "current_plan", fake_current_plan)

    assert (
        main(
            [
                "--state-home",
                str(state),
                "--workspace-key",
                str(workspace),
                "plan",
                "--session-id",
                record["id"],
                "--focus",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert focus_values == [True]


def test_cli_current_plan_repairs_missing_working_dir_before_open(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = workspace / "plans" / "linked.md"
    plan.parent.mkdir()
    plan.write_text("# Linked\n", encoding="utf-8")
    missing = tmp_path / "removed-worktree"
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=missing,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/linked.md", "active_task": None},
    )
    opened = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {})

    def fake_current_plan(_self, session, focus=False):
        opened.append(session["working_dir"])
        return {"session_id": session["id"], "opened": "plan-pane"}

    monkeypatch.setattr(Frame, "current_plan", fake_current_plan)

    assert (
        main(
            [
                "--state-home",
                str(state),
                "--workspace-key",
                str(workspace),
                "plan",
                "--session-id",
                record["id"],
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == ""
    assert opened == [str(workspace.resolve())]
    assert store.find_session(record["id"])["working_dir"] == str(workspace.resolve())


def test_cli_scratchpad_noops_without_linked_plan(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    record = store.create_session(
        name="NOPLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": None, "active_task": None},
    )

    assert (
        main(
            [
                "--state-home",
                str(state),
                "--workspace-key",
                str(workspace),
                "scratchpad",
                "--session-id",
                record["id"],
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == ""


def test_cli_current_chat_is_quiet_on_success(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
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
                "chat",
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


def test_adopt_requires_explicit_reference_or_limit(tmp_path, capsys, monkeypatch):
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
    ) == 2
    err = capsys.readouterr().err
    assert "requires --reference=<conversation-id> or --limit=<count>" in err
    assert StateStore(state, workspace).list("sessions") == []


def test_adopt_imports_claude_sessions_with_explicit_limit(tmp_path, capsys, monkeypatch):
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
            "--limit=2",
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
        "--name",
        "Hive Events Allowlist",
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
            "--dry-run",
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
    assert record["driver"]["launch_argv"] == [
        "claude",
        "--resume",
        "new-session",
        "--name",
        "Untitled Claude session",
    ]


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
        name="X", working_dir="/tmp", conversation_reference=None
    )["launch_argv"] == ["claude", "--name", "X"]
    assert drivers["claude"].resolve(
        name="X", working_dir="/tmp", conversation_reference="abc"
    )["launch_argv"] == ["claude", "--resume", "abc", "--name", "X"]
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
    assert updated["agents"]["resume_ids"]["codex"] == "conversation-1"
    assert updated["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(workspace),
        "conversation-1",
    ]


def test_hook_does_not_overwrite_existing_conversation_reference(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["claude"]
    record = store.create_session(
        name="HOOK",
        working_dir=workspace,
        source=_source(),
        driver=driver.resolve(
            name="HOOK", working_dir=str(workspace), conversation_reference="manual-1"
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
            "claude",
            '{"session_id":"background-1"}',
        ]
    ) == 0

    status = store.read("status", record["id"])
    assert status["conversation_reference"] == "background-1"
    updated = store.find_session(record["id"])
    assert updated["driver"]["resume"]["reference"] == "manual-1"
    assert updated["agents"]["resume_ids"]["claude"] == "manual-1"
    assert updated["driver"]["launch_argv"] == [
        "claude",
        "--resume",
        "manual-1",
        "--name",
        "HOOK",
    ]


def test_hook_does_not_claim_conversation_owned_by_another_session(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["codex"]
    owner = store.create_session(
        name="HIVE IDE PYPI",
        working_dir=workspace,
        source=_source(),
        driver=driver.resolve(
            name="HIVE IDE PYPI",
            working_dir=str(workspace),
            conversation_reference="shared-codex",
        ),
    )
    target = store.create_session(
        name="HIVE DRIVE",
        working_dir=workspace,
        source=_source(),
        driver=driver.resolve(
            name="HIVE DRIVE", working_dir=str(workspace), conversation_reference=None
        ),
    )
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", target["id"])
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)

    assert IdeHook.main(
        [
            "--state-home",
            str(store.home),
            "--state",
            "working",
            "--driver",
            "codex",
            '{"thread-id":"shared-codex"}',
        ]
    ) == 0

    status = store.read("status", target["id"])
    assert status["conversation_reference"] == "shared-codex"
    updated = store.find_session(target["id"])
    assert "codex" not in updated.get("agents", {}).get("resume_ids", {})
    assert updated["driver"]["resume"]["reference"] is None
    assert store.find_session(owner["id"])["driver"]["resume"]["reference"] == "shared-codex"


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


def test_repair_rehomes_missing_working_dir_to_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "gone"
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STALE",
        working_dir=missing,
        source=_source(),
        driver=_term(),
    )
    original_last_active = record["last_active"]
    calls = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, rec: calls.append(rec["working_dir"]) or True)
    monkeypatch.setattr(Frame, "windows", lambda _self: {})
    monkeypatch.setattr(Frame, "bind_keys", lambda _self: None)

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["working_dir"] == str(workspace.resolve())
    assert calls == [str(workspace.resolve())]
    updated = store.find_session(record["id"])
    assert updated["working_dir"] == str(workspace.resolve())
    assert updated["last_active"] == original_last_active
    assert updated["host"]["repair"]["previous_working_dir"] == str(missing.resolve())


def test_repair_migrates_stale_legacy_plan_key(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STALE LEGACY",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    record["host"] = {
        "hive": {
            "legacy_record": {
                "plan": "plans/stale.md",
                "plan_status": "done",
                "subagents": {"running": 1},
                "worktree_merged": True,
            }
        }
    }
    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: True)

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert "host: removed dead legacy_record.plan" in result["actions"]
    legacy = store.find_session(record["id"])["host"]["hive"]["legacy_record"]
    assert "plan" not in legacy
    assert legacy["plan_status"] == "done"
    assert legacy["subagents"] == {"running": 1}
    assert legacy["worktree_merged"] is True


def test_repair_reports_source_interpreter_that_cannot_import_package(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    broken_python = tmp_path / "broken-python"
    broken_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    broken_python.chmod(0o755)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="BROKEN SOURCE",
        working_dir=workspace,
        source={"kind": "dev", "interpreter": str(broken_python), "version": "old"},
        driver=_term(),
    )
    ensured = []
    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: ensured.append(True))

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is False
    assert ensured == []
    assert result["errors"] == [
        "source interpreter invalid: "
        f"Interpreter {broken_python.resolve()} cannot import hive_ide. "
        "Install the package into that environment."
    ]
    stored_error = store.read("errors", record["id"])
    assert "source interpreter invalid" in stored_error["detail"]


def test_repair_accepts_importable_source_interpreter(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="GOOD SOURCE",
        working_dir=workspace,
        source={"kind": "dev", "interpreter": sys.executable, "version": "test"},
        driver=_term(),
    )
    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: True)

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["errors"] == []


def test_repair_refreshes_resume_command_after_rehoming_working_dir(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "gone"
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STALE CODEX",
        working_dir=missing,
        source=_source(),
        driver={
            "id": "codex",
            "label": "Codex",
            "capabilities": ["launch", "resume", "status", "conversation_check"],
            "launch_argv": ["codex", "resume", "-C", str(missing), "conversation-1"],
            "resume": {"strategy": "conversation_id", "reference": "conversation-1"},
        },
    )

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: True)
    monkeypatch.setattr(Frame, "windows", lambda _self: {})

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert "driver: refreshed launch command" in result["actions"]
    updated = store.find_session(record["id"])
    assert updated["working_dir"] == str(workspace.resolve())
    assert updated["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(workspace.resolve()),
        "conversation-1",
    ]


def test_repair_warns_for_live_agent_without_status_hooks(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="HOOKLESS",
        working_dir=workspace,
        source=_source(),
        driver={
            "id": "codex",
            "label": "Codex",
            "capabilities": ["launch", "resume", "status"],
            "launch_argv": ["codex"],
        },
    )
    original_last_active = record["last_active"]

    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(
        record, apply=False
    )

    assert result["ok"] is True
    assert result["warnings"] == [
        "status hooks have not reported for this live agent pane; "
        "visible chat activity may not update sidebar status or order "
        "until the agent pane is restarted with the current hook environment"
    ]
    assert store.find_session(record["id"])["last_active"] == original_last_active


def test_repair_warns_for_stale_partial_status_hooks(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STALE HOOK",
        working_dir=workspace,
        source=_source(),
        driver={
            "id": "codex",
            "label": "Codex",
            "capabilities": ["launch", "resume", "status"],
            "launch_argv": ["codex"],
            "resume": {"strategy": "conversation_id", "reference": "conversation-1"},
        },
    )
    record["last_active"] = "2026-07-31T11:04:39+00:00"
    store.write("sessions", record["id"], record)
    store.write(
        "status",
        record["id"],
        {
            "schema_version": SCHEMA_VERSION,
            "workspace_key": store.workspace_key,
            "session_id": record["id"],
            "driver": "codex",
            "state": "working",
            "observed_at": "2026-07-31T10:51:25+00:00",
            "conversation_reference": None,
        },
    )

    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(
        record, apply=False
    )

    assert result["ok"] is True
    assert result["warnings"] == [
        "status hook observed_at is older than session last_active; "
        "activity ordering may be stale until the agent emits a fresh status event",
        "status hook did not report the remembered conversation reference; "
        "resume still uses the session record, but status diagnostics are partial",
    ]


def test_cli_repair_repairs_missing_working_dir_before_build(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "removed"
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STALE",
        working_dir=missing,
        source=_source(),
        driver=_term(),
    )
    ensured = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, rec: ensured.append(rec["working_dir"]) or True)
    monkeypatch.setattr(Frame, "windows", lambda _self: {})
    monkeypatch.setattr(Frame, "bind_keys", lambda _self: None)

    assert main(
        [
            "--state-home",
            str(store.home),
            "--workspace-key",
            str(workspace),
            "repair",
            "--session-id",
            record["id"],
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["actions"][0].startswith("working_dir:")
    assert ensured == [str(workspace.resolve())]


def test_cli_repair_name_overrides_ambient_session_id(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    current = store.create_session(
        name="CURRENT",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    target = store.create_session(
        name="HIVE DRIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    repaired = []

    def fake_repair(_self, record, *, apply=True):
        repaired.append((record["id"], apply))
        return {"ok": True, "session_id": record["id"], "name": record["name"]}

    monkeypatch.setenv("HIVE_IDE_SESSION_ID", current["id"])
    monkeypatch.setattr(SessionRepair, "repair", fake_repair)
    monkeypatch.setattr(Frame, "bind_keys", lambda _self: None)

    assert main(
        [
            "--state-home",
            str(store.home),
            "--workspace-key",
            str(workspace),
            "repair",
            "--name",
            "HIVE DRIVE",
            "--dry-run",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == target["id"]
    assert repaired == [(target["id"], False)]


def test_cli_repair_does_not_rebind_frame_keys(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="LIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )

    monkeypatch.setattr(
        SessionRepair,
        "repair",
        lambda _self, rec, *, apply=True: {
            "ok": True,
            "session_id": rec["id"],
            "name": rec["name"],
            "applied": apply,
        },
    )
    monkeypatch.setattr(
        Frame,
        "bind_keys",
        lambda _self: pytest.fail("repair must not rebind frame keys"),
    )

    assert main(
        [
            "--state-home",
            str(store.home),
            "--workspace-key",
            str(workspace),
            "repair",
            "--session-id",
            record["id"],
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == record["id"]
    assert payload["applied"] is True


def test_repair_rebuilds_window_when_required_pane_is_missing(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="BROKEN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    rebuilt = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "plan": "%3"},
    )
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == ["window: rebuilt for missing panes: agent"]
    assert rebuilt == [record["id"]]


def test_repair_restores_missing_plan_without_rebuilding_live_agent(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="BROKEN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    restored = []
    rebuilt = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2"},
    )
    monkeypatch.setattr(
        Frame,
        "restore_missing_panes",
        lambda _self, _record, missing: restored.extend(missing) or tuple(missing),
    )
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == ["window: restored panes: plan"]
    assert restored == ["plan"]
    assert rebuilt == []


def test_repair_retitles_healthy_existing_window(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="LIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    retitled = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(Frame, "pane_hive_ide_env", lambda _self, _pane_id: {})
    monkeypatch.setattr(Frame, "agent_pane_command", lambda _self, _record: None)
    monkeypatch.setattr(
        Frame,
        "retitle_panes",
        lambda _self, repaired: retitled.append(repaired["id"]) or True,
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == ["window: retitled panes"]
    assert retitled == [record["id"]]


def test_repair_preserves_live_panes_when_only_cwd_differs(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    old_worktree = tmp_path / "old-worktree"
    old_worktree.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="LIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    rebuilt = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=(
                f"sidebar\t{old_worktree}\n"
                f"agent\t{old_worktree}\n"
                f"plan\t{old_worktree}\n"
            ),
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == ["window: pane cwd differs; live panes preserved"]
    assert result["warnings"] == [
        f"sidebar pane cwd differs from session working_dir: {old_worktree} != "
        f"{workspace.resolve()}; repair preserves the live pane",
        f"agent pane cwd differs from session working_dir: {old_worktree} != "
        f"{workspace.resolve()}; repair preserves the live pane",
        f"plan pane cwd differs from session working_dir: {old_worktree} != "
        f"{workspace.resolve()}; repair preserves the live pane",
    ]
    assert rebuilt == []


def test_repair_respawns_non_terminal_agent_pane_that_fell_back_to_shell(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CLAUDE",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="CLAUDE",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    record["source"] = {}
    respawned = []
    rebuilt = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(Frame, "agent_pane_command", lambda _self, _record: "fish")
    monkeypatch.setattr(Frame, "agent_pane_pid", lambda _self, _record: 123)
    monkeypatch.setattr(
        Frame,
        "_process_tree",
        lambda _pid: [123],
    )
    monkeypatch.setattr(
        SessionRepair,
        "_process_cmdline",
        lambda _pid: None,
    )
    monkeypatch.setattr(
        Frame,
        "respawn_agent",
        lambda _self, repaired, pane_id: respawned.append((repaired["id"], pane_id)),
    )
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=(
                f"sidebar\t{workspace}\n"
                f"agent\t{workspace}\n"
                f"plan\t{workspace}\n"
            ),
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == ["agent: respawned exited driver pane"]
    assert respawned == [(record["id"], "%2")]
    assert rebuilt == []


def test_repair_preserves_shell_wrapper_with_live_driver_child(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CODEX",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CODEX",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    record["source"] = {}
    respawned = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(Frame, "agent_pane_command", lambda _self, _record: "sh")
    monkeypatch.setattr(Frame, "agent_pane_pid", lambda _self, _record: 123)
    monkeypatch.setattr(
        Frame,
        "_process_tree",
        lambda _pid: [123, 456],
    )
    monkeypatch.setattr(
        SessionRepair,
        "_process_cmdline",
        lambda _pid: "node /home/test/.npm/bin/codex resume conversation-1",
    )
    monkeypatch.setattr(
        Frame,
        "respawn_agent",
        lambda _self, repaired, pane_id: respawned.append((repaired["id"], pane_id)),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=(
                f"sidebar\t{workspace}\n"
                f"agent\t{workspace}\n"
                f"plan\t{workspace}\n"
            ),
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert "agent: respawned exited driver pane" not in result["actions"]
    assert respawned == []


def test_repair_preserves_shell_wrapper_with_nested_live_driver_child(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CODEX",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CODEX",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    record["source"] = {}
    respawned = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(Frame, "agent_pane_command", lambda _self, _record: "sh")
    monkeypatch.setattr(Frame, "agent_pane_pid", lambda _self, _record: 123)
    monkeypatch.setattr(Frame, "_process_tree", lambda _pid: [123, 456, 789])
    monkeypatch.setattr(
        SessionRepair,
        "_process_cmdline",
        lambda pid: {
            456: "python3 wrapper.py",
            789: "node /home/test/.npm/bin/codex resume conversation-1",
        }.get(pid),
    )
    monkeypatch.setattr(
        Frame,
        "respawn_agent",
        lambda _self, repaired, pane_id: respawned.append((repaired["id"], pane_id)),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=(
                f"sidebar\t{workspace}\n"
                f"agent\t{workspace}\n"
                f"plan\t{workspace}\n"
            ),
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert "agent: respawned exited driver pane" not in result["actions"]
    assert respawned == []


def test_repair_preserves_live_shell_driver_despite_stale_agent_env(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    owner = store.create_session(
        name="OWNER",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    record = store.create_session(
        name="CODEX",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CODEX",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    record["source"] = {}
    rebuilt = []
    respawned = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(Frame, "agent_pane_command", lambda _self, _record: "sh")
    monkeypatch.setattr(Frame, "agent_pane_pid", lambda _self, _record: 123)
    monkeypatch.setattr(
        Frame,
        "pane_hive_ide_env",
        lambda _self, _pane_id: {"HIVE_IDE_SESSION_ID": owner["id"]},
    )
    monkeypatch.setattr(
        Frame,
        "_process_tree",
        lambda _pid: [123, 456],
    )
    monkeypatch.setattr(
        SessionRepair,
        "_process_cmdline",
        lambda _pid: "node /home/test/.npm/bin/codex resume conversation-1",
    )
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )
    monkeypatch.setattr(
        Frame,
        "respawn_agent",
        lambda _self, repaired, pane_id: respawned.append((repaired["id"], pane_id)),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=(
                f"sidebar\t{workspace}\n"
                f"agent\t{workspace}\n"
                f"plan\t{workspace}\n"
            ),
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == [
        "agent: stale environment observed; live driver preserved"
    ]
    assert any("repair will rebuild the window" in warning for warning in result["warnings"])
    assert rebuilt == []
    assert respawned == []


def test_repair_does_not_respawn_terminal_session_shell_pane(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="TERM",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    respawned = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(Frame, "agent_pane_command", lambda _self, _record: "bash")
    monkeypatch.setattr(
        Frame,
        "respawn_agent",
        lambda _self, repaired, pane_id: respawned.append((repaired["id"], pane_id)),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=(
                f"sidebar\t{workspace}\n"
                f"agent\t{workspace}\n"
                f"plan\t{workspace}\n"
            ),
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert "agent: respawned exited driver pane" not in result["actions"]
    assert respawned == []


def test_repair_dry_run_reports_deleted_sidebar_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    deleted = tmp_path / "removed-worktree"
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="LIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )

    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=f"sidebar\t{deleted} (deleted)\nagent\t{workspace}\nplan\t{workspace}\n",
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(
        record, apply=False
    )

    assert result["ok"] is True
    assert result["actions"] == []
    assert result["warnings"] == [
        f"sidebar pane cwd no longer exists: {deleted} (deleted); "
        "repair will rebuild the window from the session record"
    ]


def test_repair_rebuilds_window_with_deleted_pane_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    deleted = tmp_path / "removed-worktree"
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="LIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    rebuilt = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=f"sidebar\t{deleted} (deleted)\nagent\t{workspace}\nplan\t{workspace}\n",
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == ["window: rebuilt for deleted pane cwd"]
    assert rebuilt == [record["id"]]


def test_repair_rebuilds_window_with_stale_agent_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    owner = store.create_session(
        name="HIVE DRIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    record = store.create_session(
        name="HIVE IDE PYPI",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    rebuilt = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(
        Frame,
        "pane_hive_ide_env",
        lambda _self, _pane_id: {"HIVE_IDE_SESSION_ID": owner["id"]},
    )
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda _self, _args: SimpleNamespace(
            returncode=0,
            stdout=(
                f"sidebar\t{workspace}\n"
                f"agent\t{workspace}\n"
                f"plan\t{workspace}\n"
            ),
            stderr="",
        ),
    )

    result = SessionRepair(store, Frame(store, socket="test")).repair(record)

    assert result["ok"] is True
    assert result["actions"] == ["window: rebuilt for stale agent environment"]
    assert result["warnings"] == [
        "agent pane environment belongs to another IDE session: "
        f"HIVE DRIVE ({owner['id']}); expected HIVE IDE PYPI ({record['id']}); "
        "repair will rebuild the window"
    ]
    assert rebuilt == [record["id"]]


def test_force_rebuild_replaces_public_rebuild_command(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="TARGET",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    rebuilt = []

    monkeypatch.setattr(Frame, "ensure", lambda _self, _record: False)
    monkeypatch.setattr(Frame, "windows", lambda _self: {record["id"]: "@7"})
    monkeypatch.setattr(
        Frame,
        "role_panes",
        lambda _self, _session_id: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    monkeypatch.setattr(Frame, "bind_keys", lambda _self: None)
    monkeypatch.setattr(
        Frame,
        "rebuild",
        lambda _self, repaired: rebuilt.append(repaired["id"]),
    )

    base = ["--state-home", str(store.home), "--workspace-key", str(workspace)]
    assert main([*base, "force-rebuild", "--session-id", record["id"]]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["rebuilt"] is True
    assert rebuilt == [record["id"]]

    with pytest.raises(SystemExit) as exc:
        main([*base, "rebuild", "--session-id", record["id"]])
    assert exc.value.code != 0


def test_info_card_surfaces_session_error(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="BROKEN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    error = {
        "summary": "Session needs repair",
        "component": "repair",
        "recovery": "Run hive-ide repair --session-id <id>.",
    }

    rendered = "\n".join(_card(record, {}, error))

    assert "Session needs repair" in rendered
    assert "Run hive-ide repair" in rendered


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


def test_switch_driver_resumes_previous_driver_conversation(tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr("hive_ide.drivers.shutil.which", lambda command: f"/usr/bin/{command}")
    record = store.create_session(
        name="DM HIVE",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="DM HIVE",
            working_dir=str(workspace),
            conversation_reference="claude-original",
        ),
    )

    assert main(
        [
            "attach-conversation",
            f"--session-id={record['id']}",
            "--driver=codex",
            "--reference=codex-fallback",
        ]
    ) == 0
    codex_record = json.loads(capsys.readouterr().out)
    assert codex_record["driver"]["id"] == "codex"
    assert codex_record["agents"]["resume_ids"] == {
        "claude": "claude-original",
        "codex": "codex-fallback",
    }

    assert main(
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=claude",
        ]
    ) == 0
    claude_record = json.loads(capsys.readouterr().out)
    assert claude_record["driver"]["id"] == "claude"
    assert claude_record["driver"]["resume"]["reference"] == "claude-original"
    assert claude_record["driver"]["launch_argv"] == [
        "claude",
        "--resume",
        "claude-original",
        "--name",
        "DM HIVE",
    ]
    assert claude_record["agents"]["resume_ids"]["codex"] == "codex-fallback"

    assert main(
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=codex",
        ]
    ) == 0
    codex_again = json.loads(capsys.readouterr().out)
    assert codex_again["driver"]["id"] == "codex"
    assert codex_again["driver"]["resume"]["reference"] == "codex-fallback"
    assert codex_again["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(workspace),
        "codex-fallback",
    ]


def test_switch_driver_does_not_resume_another_sessions_conversation(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr("hive_ide.drivers.shutil.which", lambda command: f"/usr/bin/{command}")
    codex = bundled_drivers()["codex"]
    claude = bundled_drivers()["claude"]
    owner = store.create_session(
        name="HIVE IDE PYPI",
        working_dir=workspace,
        source=_source(),
        driver=codex.resolve(
            name="HIVE IDE PYPI",
            working_dir=str(workspace),
            conversation_reference="shared-codex",
        ),
    )
    record = store.create_session(
        name="HIVE DRIVE",
        working_dir=workspace,
        source=_source(),
        driver=claude.resolve(
            name="HIVE DRIVE",
            working_dir=str(workspace),
            conversation_reference="hive-drive-claude",
        ),
    )
    record["agents"]["resume_ids"]["codex"] = "shared-codex"
    store.write("sessions", record["id"], record)

    assert main(
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=codex",
        ]
    ) == 0
    switched = json.loads(capsys.readouterr().out)
    assert switched["driver"]["id"] == "codex"
    assert switched["driver"]["resume"]["reference"] is None
    assert switched["driver"]["launch_argv"] == ["codex"]
    assert "codex" not in switched["agents"]["resume_ids"]
    assert store.find_session(owner["id"])["driver"]["resume"]["reference"] == "shared-codex"


def test_attach_conversation_rejects_reference_owned_by_another_session(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr("hive_ide.drivers.shutil.which", lambda command: f"/usr/bin/{command}")
    codex = bundled_drivers()["codex"]
    owner = store.create_session(
        name="HIVE IDE PYPI",
        working_dir=workspace,
        source=_source(),
        driver=codex.resolve(
            name="HIVE IDE PYPI",
            working_dir=str(workspace),
            conversation_reference="shared-codex",
        ),
    )
    target = store.create_session(
        name="HIVE DRIVE",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )

    assert main(
        [
            "attach-conversation",
            f"--session-id={target['id']}",
            "--driver=codex",
            "--reference=shared-codex",
        ]
    ) == 2
    err = capsys.readouterr().err
    assert "already attached to session 'HIVE IDE PYPI'" in err
    assert store.find_session(owner["id"])["driver"]["resume"]["reference"] == "shared-codex"


def test_switch_driver_rehomes_worktree_cwd_to_workspace_root(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    worktree = workspace / "worktree" / "feature"
    worktree.mkdir(parents=True)
    store = StateStore(tmp_path / "state", workspace)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr(
        "hive_ide.drivers.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr("hive_ide.cli.Frame.rebuild", lambda _self, _record: None)
    record = store.create_session(
        name="FEATURE",
        working_dir=worktree,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="FEATURE",
            working_dir=str(worktree),
            conversation_reference="claude-original",
        ),
    )
    record["agents"]["resume_ids"]["codex"] = "codex-fallback"
    store.write("sessions", record["id"], record)

    assert main(
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=codex",
        ]
    ) == 0
    switched = json.loads(capsys.readouterr().out)

    assert switched["working_dir"] == str(workspace.resolve())
    assert switched["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(workspace.resolve()),
        "codex-fallback",
    ]


def test_switch_driver_handoff_records_context_and_reaches_pane_env(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = workspace / "plans" / "handoff.md"
    plan.parent.mkdir()
    plan.write_text("# Handoff\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr("hive_ide.drivers.shutil.which", lambda command: f"/usr/bin/{command}")
    record = store.create_session(
        name="DM HIVE",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="DM HIVE",
            working_dir=str(workspace),
            conversation_reference="claude-original",
        ),
        plan={"path": "plans/handoff.md", "active_task": "Phase 26"},
    )

    assert main(
        [
            "attach-conversation",
            f"--session-id={record['id']}",
            "--driver=codex",
            "--reference=codex-fallback",
        ]
    ) == 0
    capsys.readouterr()

    calls = []
    monkeypatch.setattr(
        "hive_ide.cli.Frame.rebuild",
        lambda _self, rec: calls.append(rec),
    )
    assert main(
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=claude",
            "--handoff",
        ]
    ) == 0
    switched = json.loads(capsys.readouterr().out)
    handoff = switched["handoff"]
    assert handoff["from_driver"] == "codex"
    assert handoff["to_driver"] == "claude"
    assert handoff["previous_resume_reference"] == "codex-fallback"
    assert handoff["target_resume_reference"] == "claude-original"
    assert handoff["plan"] == "plans/handoff.md"
    assert handoff["active_task"] == "Phase 26"
    assert "You are now the active driver" in handoff["target_driver_prompt"]
    assert "plans/handoff.md" in handoff["target_driver_prompt"]
    assert "Phase 26" in handoff["target_driver_prompt"]
    assert calls and calls[0]["handoff"] == handoff

    env = Frame(store)._environment(switched)
    assert any(item.startswith("HIVE_IDE_HANDOFF_JSON=") for item in env)
    command = Frame._agent_command(switched)
    assert "HIVE IDE handoff" in command
    assert "You are now the active driver" in command
    assert "Handoff prompt sent to claude" in command
    assert "hive-ide: driver command failed" in command


def test_switch_driver_handoff_prompt_is_passed_to_codex_resume(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    monkeypatch.setenv("HIVE_IDE_STATE_HOME", str(store.home))
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_CONFIG", str(tmp_path / "missing-config.json"))
    monkeypatch.setattr("hive_ide.drivers.shutil.which", lambda command: f"/usr/bin/{command}")
    record = store.create_session(
        name="HIVE DRIVE",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="HIVE DRIVE",
            working_dir=str(workspace),
            conversation_reference="claude-original",
        ),
        plan={"path": "plans/hive-drive.md"},
    )
    record["agents"]["resume_ids"]["codex"] = "codex-original"
    store.write("sessions", record["id"], record)

    monkeypatch.setattr("hive_ide.cli.Frame.rebuild", lambda _self, _record: None)
    assert main(
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=codex",
            "--handoff",
        ]
    ) == 0
    switched = json.loads(capsys.readouterr().out)

    argv = Frame._argv_with_handoff_prompt(switched["driver"]["launch_argv"], switched)
    assert argv[:5] == ["codex", "resume", "-C", str(workspace), "codex-original"]
    assert argv[-1] == switched["handoff"]["target_driver_prompt"]
    command = Frame._agent_command(switched)
    assert "Handoff prompt sent to codex" in command
    assert "You are now the active driver" in command


def test_handoff_is_consumed_after_successful_frame_build(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="HANDOFF",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
    )
    record["handoff"] = {
        "session_id": record["id"],
        "session_name": record["name"],
        "from_driver": "claude",
        "to_driver": "term",
        "working_dir": str(workspace),
    }
    store.write("sessions", record["id"], record)
    frame = Frame(store)
    calls = []

    def fake_tmux(args, **kwargs):
        calls.append((args, kwargs))
        if args and args[0] in {"new-session", "new-window"}:
            return SimpleNamespace(returncode=0, stdout="@7\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(frame, "tmux", fake_tmux)
    monkeypatch.setattr(frame, "_refresh_source_if_needed", lambda *_args: None)
    monkeypatch.setattr(frame, "_tag", lambda *_args: None)
    monkeypatch.setattr(frame, "_apply_columns", lambda *_args: None)

    assert frame.build(record) == "@7"

    stored = store.read("sessions", record["id"])
    assert stored is not None
    assert "handoff" not in stored
    assert "handoff" not in record
    assert any(
        "HIVE_IDE_HANDOFF_JSON=" in item
        for args, _kwargs in calls
        for item in args
    )


def test_codex_subagent_hooks_maintain_structured_count(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CODEX",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CODEX",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)

    def send(action: str, agent_id: str) -> None:
        payload = json.dumps(
            {
                "agent_id": agent_id,
                "agent_type": "general",
                "hook_event_name": f"Subagent{action.title()}",
            }
        )
        assert (
            IdeHook.main(
                [
                    "--state-home",
                    str(store.home),
                    "--subagent",
                    action,
                    "--driver",
                    "codex",
                    payload,
                ]
            )
            == 0
        )

    send("start", "agent-a")
    status = store.read("status", record["id"])
    assert status["subagents"] == {"running": 1, "ids": ["agent-a"]}
    assert status["subagents_running"] == 1

    send("start", "agent-b")
    status = store.read("status", record["id"])
    assert status["subagents"] == {"running": 2, "ids": ["agent-a", "agent-b"]}
    assert status["subagents_running"] == 2

    send("stop", "agent-a")
    status = store.read("status", record["id"])
    assert status["subagents"] == {"running": 1, "ids": ["agent-b"]}
    assert status["subagents_running"] == 1

    send("stop", "agent-b")
    status = store.read("status", record["id"])
    assert status["subagents"] == {"running": 0, "ids": []}
    assert status["subagents_running"] == 0


def test_codex_subagent_hooks_count_anonymous_lifecycle_events(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CODEX",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="CODEX",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )
    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", store.workspace_key)
    monkeypatch.setenv("HIVE_IDE_SESSION_ID", record["id"])
    monkeypatch.delenv("HIVE_IDE_TMUX_SOCKET", raising=False)

    def send(action: str) -> None:
        assert (
            IdeHook.main(
                [
                    "--state-home",
                    str(store.home),
                    "--subagent",
                    action,
                    "--driver",
                    "codex",
                ]
            )
            == 0
        )

    send("start")
    status = store.read("status", record["id"])
    assert status["subagents"] == {
        "running": 1,
        "ids": [],
        "anonymous_running": 1,
    }
    assert status["subagents_running"] == 1

    send("start")
    status = store.read("status", record["id"])
    assert status["subagents"] == {
        "running": 2,
        "ids": [],
        "anonymous_running": 2,
    }
    assert status["subagents_running"] == 2

    send("stop")
    status = store.read("status", record["id"])
    assert status["subagents"] == {
        "running": 1,
        "ids": [],
        "anonymous_running": 1,
    }
    assert status["subagents_running"] == 1

    send("stop")
    status = store.read("status", record["id"])
    assert status["subagents"] == {"running": 0, "ids": []}
    assert status["subagents_running"] == 0


def test_plan_set_resolves_relative_plan_from_workspace_when_working_dir_is_missing(
    tmp_path, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan = workspace / "plans" / "root-plan.md"
    plan.parent.mkdir()
    plan.write_text("# Root Plan\n", encoding="utf-8")
    missing = tmp_path / "deleted-worktree"
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STALE",
        working_dir=missing,
        source=_source(),
        driver=_term(),
    )

    assert (
        main(
            [
                "--state-home",
                str(store.home),
                "--workspace-key",
                str(workspace),
                "plan-set",
                f"--session-id={record['id']}",
                "--path=plans/root-plan.md",
            ]
        )
        == 0
    )

    updated = json.loads(capsys.readouterr().out)
    assert updated["plan"]["path"] == "plans/root-plan.md"


def test_plan_set_resolves_worktree_session_plan_from_workspace_first(
    tmp_path, capsys
):
    workspace = tmp_path / "workspace"
    worktree = tmp_path / "worktree" / "feature"
    workspace.mkdir()
    worktree.mkdir(parents=True)
    plan = workspace / "plans" / "team" / "Simon" / "_archive" / "rolled.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Rolled Plan\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="WORKTREE",
        working_dir=worktree,
        source=_source(),
        driver=_term(),
    )

    assert (
        main(
            [
                "--state-home",
                str(store.home),
                "--workspace-key",
                str(workspace),
                "plan-set",
                f"--session-id={record['id']}",
                "--path=plans/team/Simon/_archive/rolled.md",
            ]
        )
        == 0
    )

    updated = json.loads(capsys.readouterr().out)
    assert updated["plan"]["path"] == "plans/team/Simon/_archive/rolled.md"


def test_plan_set_refuses_missing_relative_plan_for_worktree_session(tmp_path):
    workspace = tmp_path / "workspace"
    worktree = tmp_path / "worktree" / "feature"
    workspace.mkdir()
    worktree.mkdir(parents=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="WORKTREE",
        working_dir=worktree,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/existing.md", "active_task": None},
    )

    assert (
        main(
            [
                "--state-home",
                str(store.home),
                "--workspace-key",
                str(workspace),
                "plan-set",
                f"--session-id={record['id']}",
                "--path=plans/missing.md",
            ]
        )
        == 2
    )
    assert store.find_session(record["id"])["plan"]["path"] == "plans/existing.md"


def test_frame_plan_path_resolves_worktree_session_plan_from_workspace_first(tmp_path):
    workspace = tmp_path / "workspace"
    worktree = tmp_path / "worktree" / "feature"
    workspace_plan = workspace / "plans" / "rolled.md"
    worktree_plan = worktree / "plans" / "rolled.md"
    workspace_plan.parent.mkdir(parents=True)
    worktree_plan.parent.mkdir(parents=True)
    workspace_plan.write_text("# Workspace Plan\n", encoding="utf-8")
    worktree_plan.write_text("# Worktree Copy\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="WORKTREE",
        working_dir=worktree,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/rolled.md", "active_task": None},
    )

    assert Frame.plan_path(record) == workspace_plan.resolve()


def test_frame_plan_path_falls_back_to_working_dir_when_workspace_plan_missing(tmp_path):
    workspace = tmp_path / "workspace"
    worktree = tmp_path / "worktree" / "feature"
    worktree_plan = worktree / "plans" / "local.md"
    workspace.mkdir()
    worktree_plan.parent.mkdir(parents=True)
    worktree_plan.write_text("# Local Plan\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="WORKTREE",
        working_dir=worktree,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/local.md", "active_task": None},
    )

    assert Frame.plan_path(record) == worktree_plan.resolve()


def test_frame_plan_path_refuses_missing_relative_plan_for_worktree_session(tmp_path):
    workspace = tmp_path / "workspace"
    worktree = tmp_path / "worktree" / "feature"
    workspace.mkdir()
    worktree.mkdir(parents=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="WORKTREE",
        working_dir=worktree,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/missing.md", "active_task": None},
    )

    with pytest.raises(UsageError, match="Plan file does not exist"):
        Frame.plan_path(record)


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


def test_plan_pane_opens_micro_readonly_by_default(tmp_path, monkeypatch):
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
    monkeypatch.setenv("HIVE_IDE_EDITOR", "micro")

    command = frame._plan_command(record)

    assert shlex.join(["micro", "-readonly", "true", str(plan)]) in command


def test_plan_pane_uses_vim_readonly_flag(tmp_path, monkeypatch):
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
    monkeypatch.setenv("HIVE_IDE_EDITOR", "nvim --clean")

    command = frame._plan_command(record, line=9)

    assert shlex.join(["nvim", "-R", "--clean", "+9", str(plan)]) in command


def test_plan_pane_leaves_unknown_editor_argv_unchanged(tmp_path, monkeypatch):
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
    monkeypatch.setenv("HIVE_IDE_EDITOR", "code --wait")

    command = frame._plan_command(record)

    assert shlex.join(["code", "--wait", str(plan)]) in command
    assert "-readonly" not in command
    assert " -R " not in command


def test_current_plan_marks_live_micro_readonly_without_respawning(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    plan = workspace / "plans" / "example.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Example\n\n- [ ] Task\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/example.md", "active_task": None},
    )
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"plan": "%3"})
    monkeypatch.setenv("TMUX_PANE", "%2")
    calls = []

    def fake_tmux(argv):
        calls.append(argv)
        if argv == ["display-message", "-p", "-t", "%3", "#{pane_current_command}"]:
            return SimpleNamespace(returncode=0, stdout="micro\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    result = frame.current_plan(record, focus=True)

    assert result["opened"] == "plan-pane"
    assert ["send-keys", "-t", "%3", "C-e"] in calls
    assert ["send-keys", "-t", "%3", "set readonly true", "Enter"] in calls
    assert ["send-keys", "-t", "%3", "goto 3", "Enter"] in calls
    assert not any(call[:2] == ["respawn-pane", "-k"] for call in calls)


def test_plan_focus_line_targets_first_unfinished_checkbox(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(
        "\n".join(
            [
                "# Plan",
                "",
                "### Phase 1",
                "",
                "- [x] Done",
                "- [ ] First open task",
                "- [ ] Second open task",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert Frame.plan_focus_line(plan) == 6


def test_plan_focus_line_targets_last_finished_checkbox_when_all_done(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(
        "\n".join(
            [
                "# Plan",
                "",
                "## Tasks",
                "",
                "- [x] First done task",
                "- [X] Last done task",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert Frame.plan_focus_line(plan) == 6


def test_scratchpad_is_inserted_before_tasks(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(
        "\n".join(
            [
                "# Plan",
                "",
                "## Why",
                "",
                "Context.",
                "",
                "## Tasks",
                "",
                "- [ ] Do it",
                "",
                "## Status Log",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    line = Frame.ensure_scratchpad(plan)

    text = plan.read_text(encoding="utf-8")
    assert line == 7
    assert text.index("## Scratchpad") < text.index("## Tasks")
    assert text.count("## Scratchpad") == 1


def test_scratchpad_is_inserted_before_status_log_when_tasks_missing(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n## Why\n\nContext.\n\n## Status Log\n", encoding="utf-8")

    line = Frame.ensure_scratchpad(plan)

    text = plan.read_text(encoding="utf-8")
    assert line == 7
    assert text.index("## Scratchpad") < text.index("## Status Log")


def test_scratchpad_reuses_existing_section(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(
        "\n".join(
            [
                "# Plan",
                "",
                "## Scratchpad",
                "",
                "Human note.",
                "",
                "## Tasks",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert Frame.ensure_scratchpad(plan) == 3
    assert plan.read_text(encoding="utf-8").count("## Scratchpad") == 1


def test_scratchpad_appends_when_plan_has_no_tasks_or_log(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text("# Plan\n\n## Why\n", encoding="utf-8")

    line = Frame.ensure_scratchpad(plan)

    assert line == 5
    assert plan.read_text(encoding="utf-8").endswith("\n## Scratchpad\n\n")


def test_scratchpad_popup_uses_micro_at_scratchpad_line(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    plan = workspace / "plans" / "example.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Example\n\n## Tasks\n\n- [ ] Task\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/example.md", "active_task": None},
    )
    frame = Frame(store)
    monkeypatch.setattr("hive_ide.frame.shutil.which", lambda command: "/usr/bin/micro")
    calls = []

    def fake_tmux(argv):
        calls.append(argv)
        if argv == ["display-message", "-p", "#{client_width}"]:
            return SimpleNamespace(returncode=0, stdout="160", stderr="")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    result = frame.scratchpad(record)

    assert result["opened"] == "scratchpad-popup"
    assert "## Scratchpad" in plan.read_text(encoding="utf-8")
    assert calls == [
        ["display-message", "-p", "#{client_width}"],
        [
            "display-popup",
            "-E",
            "-w",
            "72%",
            "-h",
            "70%",
            shlex.join(["micro", "+3", str(plan)]),
        ]
    ]


def test_plan_popup_opens_plan_at_top(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    plan = workspace / "plans" / "example.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Example\n\n## Tasks\n\n- [ ] Task\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/example.md", "active_task": None},
    )
    frame = Frame(store)
    monkeypatch.setattr("hive_ide.frame.shutil.which", lambda command: "/usr/bin/micro")
    calls = []

    def fake_tmux(argv):
        calls.append(argv)
        if argv == ["display-message", "-p", "#{client_width}"]:
            return SimpleNamespace(returncode=0, stdout="160", stderr="")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    result = frame.plan_popup(record, mode="plan")

    assert result["opened"] == "plan-popup"
    assert result["line"] == 1
    assert calls[-1][-1] == shlex.join(["micro", str(plan)])


def test_tasks_popup_opens_first_unfinished_task(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    plan = workspace / "plans" / "example.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Example\n\n## Tasks\n\n- [x] Done\n- [ ] Next\n",
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/example.md", "active_task": None},
    )
    frame = Frame(store)
    monkeypatch.setattr("hive_ide.frame.shutil.which", lambda command: "/usr/bin/micro")
    calls = []

    def fake_tmux(argv):
        calls.append(argv)
        if argv == ["display-message", "-p", "#{client_width}"]:
            return SimpleNamespace(returncode=0, stdout="160", stderr="")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    result = frame.plan_popup(record, mode="tasks")

    assert result["opened"] == "tasks-popup"
    assert result["line"] == 6
    assert calls[-1][-1] == shlex.join(["micro", "+6", str(plan)])


def test_tasks_popup_noops_without_tasks_section(tmp_path):
    workspace = tmp_path / "workspace"
    plan = workspace / "plans" / "example.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Example\n\n## Why\n\nContext.\n", encoding="utf-8")
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="PLAN",
        working_dir=workspace,
        source=_source(),
        driver=_term(),
        plan={"path": "plans/example.md", "active_task": None},
    )

    assert Frame(store).plan_popup(record, mode="tasks") == {
        "ok": False,
        "reason": "no_tasks",
        "session_id": record["id"],
        "plan": str(plan),
    }


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


def test_current_chat_allows_plain_agent_without_conversation_reference(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="CHAT",
            working_dir=str(workspace),
            conversation_reference=None,
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

    assert result["opened"] == "terminal"
    assert calls == [(["claude", "--name", "CHAT"], {"cwd": str(workspace)})]


def test_current_chat_selects_plain_agent_pane_without_conversation_reference(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="CHAT",
            working_dir=str(workspace),
            conversation_reference=None,
        ),
    )
    frame = Frame(store)
    calls = []
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {"agent": "%2"})
    monkeypatch.setattr(
        frame,
        "tmux",
        lambda args, **_kwargs: calls.append(args)
        or SimpleNamespace(returncode=0, stdout="claude\n", stderr=""),
    )

    result = frame.current_chat(record)

    assert result["opened"] == "existing-agent-pane"
    assert calls == [
        ["display-message", "-p", "-t", "%2", "#{pane_current_command}"],
        ["select-pane", "-t", "%2"],
    ]


def test_current_chat_opens_plain_claude_when_resume_fails(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
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
        if argv == ["claude", "--resume", "conversation-1", "--name", "CHAT"]:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hive_ide.frame.subprocess.run", fake_run)
    result = frame.current_chat(record)

    assert result == {
        "session_id": record["id"],
        "driver": "claude",
        "opened": "claude",
    }
    assert calls == [
        (["claude", "--resume", "conversation-1", "--name", "CHAT"], {"cwd": str(workspace)}),
        (["claude", "--name", "CHAT"], {"cwd": str(workspace)}),
    ]


def test_current_chat_never_uses_claude_agents_when_resume_fails(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="CHAT",
            working_dir=str(workspace),
            conversation_reference="stale-conversation",
        ),
    )
    frame = Frame(store)
    monkeypatch.setattr(frame, "role_panes", lambda _session_id: {})
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv == ["claude", "--resume", "stale-conversation", "--name", "CHAT"]:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("hive_ide.frame.subprocess.run", fake_run)
    result = frame.current_chat(record)

    assert result["opened"] == "claude"
    assert calls == [
        (["claude", "--resume", "stale-conversation", "--name", "CHAT"], {"cwd": str(workspace)}),
        (["claude", "--name", "CHAT"], {"cwd": str(workspace)}),
    ]


def test_claude_agent_pane_uses_normal_resume_command(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CHAT",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="CHAT",
            working_dir=str(workspace),
            conversation_reference="conversation-1",
        ),
    )

    command = Frame._agent_command(record)

    assert "claude --resume conversation-1 --name CHAT; status=$?" in command
    assert "hive-ide: driver command failed" in command
    assert "claude agents" not in command
    assert command.endswith('; exec "${SHELL:-/bin/sh}"')


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
    assert calls == [
        ["display-message", "-p", "-t", "%2", "#{pane_current_command}"],
        ["select-pane", "-t", "%2"],
    ]


def test_driver_rename_sends_slash_command_to_agent_pane(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="HIVE DRIVE",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["codex"].resolve(
            name="HIVE DRIVE",
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

    result = frame.driver_rename(record)

    assert result == {
        "session_id": record["id"],
        "driver": "codex",
        "name": "HIVE DRIVE",
        "sent": "/rename",
    }
    assert calls == [
        ["send-keys", "-l", "-t", "%2", "/rename HIVE DRIVE"],
        ["send-keys", "-t", "%2", "Enter"],
    ]


def test_driver_rename_supports_claude_agent_pane(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="CLAUDE SESSION",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["claude"].resolve(
            name="CLAUDE SESSION",
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

    result = frame.driver_rename(record)

    assert result["driver"] == "claude"
    assert calls == [
        ["send-keys", "-l", "-t", "%2", "/rename CLAUDE SESSION"],
        ["send-keys", "-t", "%2", "Enter"],
    ]


def test_driver_rename_rejects_unsupported_driver(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="TERM SESSION",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["term"].resolve(
            name="TERM SESSION",
            working_dir=str(workspace),
            conversation_reference=None,
        ),
    )

    with pytest.raises(UsageError, match="supported only for Claude and Codex"):
        Frame(store).driver_rename(record)


def test_current_chat_respawns_dead_agent_shell_pane(tmp_path, monkeypatch):
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

    def fake_tmux(args, **_kwargs):
        calls.append(args)
        stdout = "sh\n" if args[:4] == ["display-message", "-p", "-t", "%2"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    result = frame.current_chat(record)

    assert result["opened"] == "agent-pane"
    assert any(call[:3] == ["respawn-pane", "-k", "-t"] for call in calls)
    assert calls[-1] == ["select-pane", "-t", "%2"]


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


def test_rebuild_keeps_existing_window_when_replacement_build_fails(tmp_path, monkeypatch):
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
        or SimpleNamespace(returncode=0, stdout="3\n", stderr=""),
    )
    monkeypatch.setattr(
        frame,
        "build",
        lambda _record: (_ for _ in ()).throw(HiveIdeError("replacement failed")),
    )

    with pytest.raises(HiveIdeError, match="replacement failed"):
        frame.rebuild(record)

    assert ["kill-window", "-t", "@7"] not in calls


def test_rebuild_wakes_inactive_agent_pane_and_restores_focus(tmp_path, monkeypatch):
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
    monkeypatch.setattr(frame, "build", lambda _record: "@9")

    def fake_tmux(args, **_kwargs):
        calls.append(args)
        if args == [
            "display-message",
            "-p",
            "-t",
            frame.target,
            "#{window_id}.#{pane_index}",
        ]:
            return SimpleNamespace(returncode=0, stdout="@5.1\n", stderr="")
        if args == ["display-message", "-p", "-t", "@7", "#{window_index}"]:
            return SimpleNamespace(returncode=0, stdout="2\n", stderr="")
        if args == ["display-message", "-p", "-t", "@7", "#{window_active}"]:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        if args == ["display-message", "-p", "-t", "@9", "#{window_index}"]:
            return SimpleNamespace(returncode=0, stdout="2\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(frame, "tmux", fake_tmux)

    frame.rebuild(record)

    assert ["select-window", "-t", "@9"] in calls
    assert ["select-pane", "-t", "@9.1"] in calls
    assert ["send-keys", "-t", "@9.1", "C-l"] in calls
    assert calls[-1] == ["select-pane", "-t", "@5.1"]


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
    original_last_active = record["last_active"]
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
    assert store.find_session(record["id"])["last_active"] == original_last_active


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
    assert rebuilt == []


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


def test_working_dir_set_updates_metadata_without_rebuilding_live_window(
    monkeypatch, tmp_path, capsys
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    linked = tmp_path / "linked"
    linked.mkdir()
    state = tmp_path / "state"
    store = StateStore(state, workspace)
    record = store.create_session(
        name="LIVE",
        working_dir=workspace,
        source=_source(),
        driver={
            "id": "codex",
            "label": "Codex",
            "capabilities": ["launch", "resume", "status", "conversation_check"],
            "launch_argv": ["codex", "resume", "-C", str(workspace), "conversation-1"],
            "resume": {"strategy": "conversation_id", "reference": "conversation-1"},
        },
    )
    monkeypatch.setattr(
        "hive_ide.cli.Frame.rebuild",
        lambda _frame, _record: pytest.fail("working-dir-set must not rebuild"),
    )
    monkeypatch.setattr(
        "hive_ide.cli.Frame.ensure",
        lambda _frame, session: session["working_dir"] == str(linked.resolve()),
    )
    monkeypatch.setattr("hive_ide.cli.Frame.windows", lambda _frame: {})
    monkeypatch.setattr("hive_ide.cli.Frame.bind_keys", lambda _frame: None)

    assert (
        main(
            [
                "--state-home",
                str(state),
                "--workspace-key",
                str(workspace),
                "working-dir-set",
                "--session-id",
                record["id"],
                "--working-dir",
                str(linked),
            ]
        )
        == 0
    )

    capsys.readouterr()
    updated = store.find_session(record["id"])
    assert updated["working_dir"] == str(linked.resolve())
    assert updated["driver"]["launch_argv"] == [
        "codex",
        "resume",
        "-C",
        str(linked.resolve()),
        "conversation-1",
    ]


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
    codex = json.loads((home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    codex_commands = [
        handler["command"]
        for groups in codex["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert any("--subagent start" in command for command in codex_commands)
    assert any("--subagent stop" in command for command in codex_commands)
    claude_commands = [
        handler["command"]
        for groups in merged["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert any("--subagent start" in command for command in claude_commands)
    assert any("--subagent stop" in command for command in claude_commands)
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


def test_hook_installer_uses_configured_stable_python(monkeypatch, tmp_path):
    config_home = tmp_path / "config"
    stable = tmp_path / "global" / "python"
    stable.parent.mkdir(parents=True)
    stable.touch()
    config_path = config_home / "hive-ide" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"sources": {"stable": {"interpreter": str(stable)}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setattr(
        "hive_ide.hooks.inspect_interpreter",
        lambda interpreter: {
            "interpreter": str(interpreter),
            "package_version": "1.2.3",
        },
    )

    installer = HookInstaller(home=tmp_path / "home")

    assert installer.stable_python == stable.absolute()
    assert installer.verify() != ["Selected interpreter is not executable: " + str(managed_interpreter("stable"))]


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
