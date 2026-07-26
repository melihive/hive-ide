from __future__ import annotations

from pathlib import Path

from hive_ide.drivers import bundled_drivers
from hive_ide.newmodal import IdeNewModal
from hive_ide.store import StateStore


def _record(store: StateStore, name: str = "TAKEN") -> dict:
    return store.create_session(
        name=name,
        working_dir=store.workspace_key,
        source={"kind": "stable", "interpreter": "/python", "version": "test"},
        driver=bundled_drivers()["term"].resolve(
            name=name, working_dir=store.workspace_key, conversation_reference=None
        ),
    )


def test_build_args_maps_all_bundled_drivers():
    for driver in ("claude", "codex", "antigravity", "term"):
        assert IdeNewModal._build_args("SESSION", driver) == [
            "create",
            "--name=SESSION",
            f"--driver={driver}",
        ]


def test_validate_rejects_empty_invalid_and_duplicate(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _record(StateStore(tmp_path, workspace))
    assert IdeNewModal._validate(tmp_path, str(workspace), "   ")[0] is None
    assert IdeNewModal._validate(tmp_path, str(workspace), "bad:name")[0] is None
    assert IdeNewModal._validate(tmp_path, str(workspace), "WAY TOO LONG A NAME")[0] is None
    name, error = IdeNewModal._validate(tmp_path, str(workspace), "taken")
    assert name is None and "exists" in error


def test_do_create_uses_public_cli_and_ensure(monkeypatch, tmp_path):
    calls = []
    outputs = iter(
        [
            (True, '{"id": "session-id"}'),
            (True, '{"built": true, "session_id": "session-id"}'),
        ]
    )
    monkeypatch.setattr(
        IdeNewModal,
        "_cli",
        staticmethod(lambda _state, args: (calls.append(args) or next(outputs))),
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *args, **kwargs: type(
            "Result",
            (),
            {"stdout": "@7\tsession-id\n", "returncode": 0},
        )(),
    )
    ok, message = IdeNewModal._do_create(tmp_path, "SESSION", "antigravity")
    assert ok and message == ""
    assert calls == [
        ["create", "--name=SESSION", "--driver=antigravity"],
        ["ensure", "--session-id=session-id"],
    ]


def test_do_create_surfaces_failure_without_ensure(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        IdeNewModal,
        "_cli",
        staticmethod(lambda _state, args: (calls.append(args) or (False, "driver unavailable"))),
    )
    ok, message = IdeNewModal._do_create(tmp_path, "SESSION", "claude")
    assert not ok and message == "driver unavailable"
    assert calls == [["create", "--name=SESSION", "--driver=claude"]]


def test_do_create_refuses_success_without_an_id(monkeypatch, tmp_path):
    monkeypatch.setattr(
        IdeNewModal,
        "_cli",
        staticmethod(lambda _state, _args: (True, "{}")),
    )
    ok, message = IdeNewModal._do_create(tmp_path, "SESSION", "term")
    assert not ok
    assert "session id" in message


def test_unexpanded_tmux_format_is_resolved(monkeypatch):
    monkeypatch.setattr(IdeNewModal, "_ask_tmux", staticmethod(lambda _fmt: "REAL"))
    assert IdeNewModal._resolve("#{window_name}", "#{window_name}") == "REAL"


def test_modal_exposes_the_four_bundled_drivers():
    assert [item[0] for item in IdeNewModal.TYPES] == [
        "claude",
        "codex",
        "antigravity",
        "term",
    ]
