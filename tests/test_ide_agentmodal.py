from __future__ import annotations

from hive_ide.agentmodal import IdeAgentModal
from hive_ide.drivers import bundled_drivers
from hive_ide.newmodal import IdeNewModal
from hive_ide.store import StateStore
import hive_ide.agentmodal as agentmodal_module


def _write(tmp_path, name="ALPHA", driver="codex"):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path, workspace)
    record = store.create_session(
        name=name,
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "/python", "version": "test"},
        driver=bundled_drivers()[driver].resolve(
            name=name, working_dir=str(workspace), conversation_reference=None
        ),
    )
    return workspace, record


def test_active_reads_protocol_record(tmp_path):
    workspace, record = _write(tmp_path)
    assert IdeAgentModal._active(tmp_path, str(workspace), record["id"]) == "codex"


def test_active_is_none_for_missing_session(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert IdeAgentModal._active(tmp_path, str(workspace), "missing-id") is None


def test_switch_uses_immutable_id(monkeypatch, tmp_path):
    workspace, record = _write(tmp_path)
    IdeNewModal._workspace_key = str(workspace)
    IdeNewModal._tmux_socket = "live-socket"
    calls = []
    monkeypatch.setattr(
        IdeNewModal,
        "_cli",
        staticmethod(lambda _state, args: (calls.append(args) or (True, ""))),
    )
    assert IdeAgentModal._switch(tmp_path, record["id"], "term") == (True, "")
    assert calls == [
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=term",
            "--tmux-socket=live-socket",
        ]
    ]
    IdeNewModal._tmux_socket = ""


def test_switch_can_request_handoff(monkeypatch, tmp_path):
    workspace, record = _write(tmp_path)
    IdeNewModal._workspace_key = str(workspace)
    calls = []
    monkeypatch.setattr(
        IdeNewModal,
        "_cli",
        staticmethod(lambda _state, args: (calls.append(args) or (True, ""))),
    )
    assert IdeAgentModal._switch(
        tmp_path, record["id"], "term", handoff=True
    ) == (True, "")
    assert calls == [
        [
            "switch-driver",
            f"--session-id={record['id']}",
            "--driver=term",
            "--handoff",
        ]
    ]


def test_switch_returns_failure_detail(monkeypatch, tmp_path):
    workspace, record = _write(tmp_path)
    IdeNewModal._workspace_key = str(workspace)
    monkeypatch.setattr(
        IdeNewModal,
        "_cli",
        staticmethod(lambda _state, _args: (False, "driver unavailable")),
    )
    assert IdeAgentModal._switch(tmp_path, record["id"], "claude") == (
        False,
        "driver unavailable",
    )


def test_agent_modal_reuses_new_modal_driver_list():
    assert [item[0] for item in IdeNewModal.TYPES] == [
        "claude",
        "codex",
        "antigravity",
        "term",
    ]


def test_fourth_driver_can_be_selected_by_digit(monkeypatch, tmp_path):
    workspace, record = _write(tmp_path, driver="codex")
    selected = {}
    monkeypatch.setattr(
        "hive_ide.agentmodal.sys.stdin",
        type("Input", (), {"isatty": lambda self: True, "fileno": lambda self: 0})(),
    )
    monkeypatch.setattr(agentmodal_module.termios, "tcgetattr", lambda _fd: [])
    monkeypatch.setattr(agentmodal_module.termios, "tcsetattr", lambda *_args: None)
    monkeypatch.setattr(agentmodal_module.tty, "setcbreak", lambda _fd: None)
    monkeypatch.setattr(IdeAgentModal, "_draw", staticmethod(lambda *_args: None))
    monkeypatch.setattr(IdeNewModal, "_getkey", staticmethod(lambda _fd: "4"))
    monkeypatch.setattr(
        IdeAgentModal,
        "_switch",
        staticmethod(
            lambda _state, session_id, driver, *, handoff=False: (
                selected.update(
                    session_id=session_id, driver=driver, handoff=handoff
                )
                or (True, "")
            )
        ),
    )

    assert IdeAgentModal.main(
        [
            "agentmodal",
            "--state-home",
            str(tmp_path),
            "--workspace-key",
            str(workspace),
            "--session-id",
            record["id"],
        ]
    ) == 0
    assert selected == {
        "session_id": record["id"],
        "driver": "term",
        "handoff": False,
    }


def test_modal_left_right_toggles_handoff(monkeypatch, tmp_path):
    workspace, record = _write(tmp_path, driver="codex")
    selected = {}
    keys = iter(["right", "4"])
    monkeypatch.setattr(
        "hive_ide.agentmodal.sys.stdin",
        type("Input", (), {"isatty": lambda self: True, "fileno": lambda self: 0})(),
    )
    monkeypatch.setattr(agentmodal_module.termios, "tcgetattr", lambda _fd: [])
    monkeypatch.setattr(agentmodal_module.termios, "tcsetattr", lambda *_args: None)
    monkeypatch.setattr(agentmodal_module.tty, "setcbreak", lambda _fd: None)
    monkeypatch.setattr(IdeAgentModal, "_draw", staticmethod(lambda *_args: None))
    monkeypatch.setattr(IdeNewModal, "_getkey", staticmethod(lambda _fd: next(keys)))
    monkeypatch.setattr(
        IdeAgentModal,
        "_switch",
        staticmethod(
            lambda _state, session_id, driver, *, handoff=False: (
                selected.update(
                    session_id=session_id, driver=driver, handoff=handoff
                )
                or (True, "")
            )
        ),
    )

    assert IdeAgentModal.main(
        [
            "agentmodal",
            "--state-home",
            str(tmp_path),
            "--workspace-key",
            str(workspace),
            "--session-id",
            record["id"],
        ]
    ) == 0
    assert selected == {
        "session_id": record["id"],
        "driver": "term",
        "handoff": True,
    }
