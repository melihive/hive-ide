from pathlib import Path

from hive_ide.optionsmodal import IdeOptionsModal


def test_options_modal_offers_session_info_action():
    assert ("card", "session info", "show the info modal") in IdeOptionsModal.ACTIONS
    assert ("repair", "repair", "heal session state and redraw") in IdeOptionsModal.ACTIONS
    assert not any(action == "rebuild" for action, _label, _note in IdeOptionsModal.ACTIONS)


def test_options_modal_routes_common_actions(monkeypatch, tmp_path):
    calls = []

    def fake_cli(_skill_dir: Path, args: list[str]):
        calls.append(args)
        return True, ""

    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._cli", fake_cli)
    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._tmux_socket", "socket")

    assert IdeOptionsModal._command(tmp_path, "session-id", "chat") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "plan") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "repair") == (True, "")
    assert (
        IdeOptionsModal._command(tmp_path, "session-id", "rename", name="NEW NAME")
        == (True, "")
    )

    assert calls == [
        [
            "--quiet",
            "current-chat",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
        [
            "--quiet",
            "current-plan",
            "--session-id=session-id",
            "--tmux-socket=socket",
            "--focus",
        ],
        [
            "--quiet",
            "repair",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
        [
            "--quiet",
            "rename",
            "--session-id=session-id",
            "--name=NEW NAME",
            "--tmux-socket=socket",
        ],
    ]


def test_options_modal_rejects_empty_rename(tmp_path):
    assert IdeOptionsModal._command(tmp_path, "session-id", "rename") == (
        False,
        "A new name is required.",
    )


def test_options_modal_rename_prompt_accepts_bs_key(monkeypatch):
    keys = iter(["bs", "bs", "X", "enter"])
    monkeypatch.setattr("hive_ide.optionsmodal.IdeOptionsModal._draw", lambda *args: None)
    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._getkey", lambda _fd: next(keys))

    assert (
        IdeOptionsModal._rename_prompt(
            0,
            {"name": "OLD"},
            "/workspace",
            0,
        )
        == "OX"
    )


def test_options_modal_rename_prompt_supports_clear_line(monkeypatch):
    keys = iter(["\x15", "N", "e", "w", "enter"])
    monkeypatch.setattr("hive_ide.optionsmodal.IdeOptionsModal._draw", lambda *args: None)
    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._getkey", lambda _fd: next(keys))

    assert (
        IdeOptionsModal._rename_prompt(
            0,
            {"name": "OLD"},
            "/workspace",
            0,
        )
        == "New"
    )
