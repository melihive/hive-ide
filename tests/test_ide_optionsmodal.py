from pathlib import Path

from hive_ide.optionsmodal import IdeOptionsModal


def test_options_modal_offers_session_info_action():
    assert ("card", "session info", "show the info modal") in IdeOptionsModal.ACTIONS


def test_options_modal_routes_common_actions(monkeypatch, tmp_path):
    calls = []

    def fake_cli(_skill_dir: Path, args: list[str]):
        calls.append(args)
        return True, ""

    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._cli", fake_cli)
    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._tmux_socket", "socket")

    assert IdeOptionsModal._command(tmp_path, "session-id", "chat") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "plan") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "rebuild") == (True, "")
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
            "rebuild",
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
