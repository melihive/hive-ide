from pathlib import Path

from hive_ide.optionsmodal import IdeOptionsModal


def test_options_modal_offers_session_info_action():
    actions = IdeOptionsModal._actions({"driver": {"id": "term"}})
    assert ("card", "session info", "show the info modal") in actions
    assert ("plan-modal", "plan modal", "open the plan in a popup") in actions
    assert ("tasks-modal", "tasks modal", "open tasks at first unfinished") in actions
    assert ("scratchpad", "scratchpad", "open notes in a plan popup") in actions
    assert ("repair", "repair", "heal session state and redraw") in actions
    assert ("archive", "archive", "close and move to archive") in actions
    assert not any(action == "sleep" for action, _label, _note in actions)
    assert not any(action == "rebuild" for action, _label, _note in actions)


def test_options_modal_offers_sleep_for_agent_sessions_before_archive():
    actions = IdeOptionsModal._actions({"driver": {"id": "codex"}})

    assert ("sleep", "sleep agent", "stop agent, keep session listed") in actions
    assert [action for action, _label, _note in actions[-3:]] == [
        "repair",
        "sleep",
        "archive",
    ]


def test_options_modal_offers_driver_rename_for_claude_and_codex():
    codex_actions = IdeOptionsModal._actions({"driver": {"id": "codex"}})
    claude_actions = IdeOptionsModal._actions({"driver": {"id": "claude"}})
    term_actions = IdeOptionsModal._actions({"driver": {"id": "term"}})

    expected = ("driver-rename", "rename driver", "send /rename when agent is idle")
    assert expected in codex_actions
    assert expected in claude_actions
    assert not any(action == "driver-rename" for action, _label, _note in term_actions)
    assert [action for action, _label, _note in codex_actions[5:9]] == [
        "card",
        "agent",
        "rename",
        "driver-rename",
    ]


def test_options_modal_routes_common_actions(monkeypatch, tmp_path):
    calls = []
    background_calls = []

    def fake_cli(_skill_dir: Path, args: list[str]):
        calls.append(args)
        return True, ""

    def fake_background_cli(_skill_dir: Path, args: list[str]):
        background_calls.append(args)
        return True, ""

    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._cli", fake_cli)
    monkeypatch.setattr(
        "hive_ide.optionsmodal.IdeOptionsModal._background_cli", fake_background_cli
    )
    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._tmux_socket", "socket")

    assert IdeOptionsModal._command(tmp_path, "session-id", "chat") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "plan") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "plan-modal") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "tasks-modal") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "scratchpad") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "repair") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "sleep") == (True, "")
    assert IdeOptionsModal._command(tmp_path, "session-id", "archive") == (True, "")
    assert (
        IdeOptionsModal._command(tmp_path, "session-id", "rename", name="NEW NAME")
        == (True, "")
    )
    assert IdeOptionsModal._command(tmp_path, "session-id", "driver-rename") == (
        True,
        "",
    )

    assert background_calls == [
        [
            "--quiet",
            "plan-popup",
            "--mode=plan",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
        [
            "--quiet",
            "plan-popup",
            "--mode=tasks",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
        [
            "--quiet",
            "scratchpad",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
    ]
    assert calls == [
        [
            "--quiet",
            "chat",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
        [
            "--quiet",
            "plan",
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
            "sleep",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
        [
            "--quiet",
            "archive",
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
        [
            "--quiet",
            "driver-rename",
            "--session-id=session-id",
            "--tmux-socket=socket",
        ],
    ]


def test_options_modal_mouse_click_maps_action_rows():
    rows = IdeOptionsModal._action_rows({"driver": {"id": "term"}})
    assert IdeOptionsModal._mouse_selection(b"\x1b[<0;12;5M", rows) == 0
    assert IdeOptionsModal._mouse_selection(b"\x1b[<0;12;9M", rows) == 4
    assert IdeOptionsModal._mouse_selection(b"\x1b[<2;12;5M", rows) is None
    assert IdeOptionsModal._mouse_selection(b"\x1b[<0;12;4M", rows) is None


def test_options_modal_rejects_empty_rename(tmp_path):
    assert IdeOptionsModal._command(tmp_path, "session-id", "rename") == (
        False,
        "A new name is required.",
    )


def test_options_modal_rename_prompt_accepts_bs_key(monkeypatch):
    keys = iter(["bs", "bs", "X", "enter"])
    monkeypatch.setattr("hive_ide.optionsmodal.IdeOptionsModal._draw", lambda *args: None)
    monkeypatch.setattr("hive_ide.optionsmodal.IdeOptionsModal._getkey", lambda _fd: next(keys))

    assert (
        IdeOptionsModal._rename_prompt(
            0,
            {"name": "OLD"},
            "/workspace",
            0,
        )
        == ("OX", False)
    )


def test_options_modal_rename_prompt_supports_clear_line(monkeypatch):
    keys = iter(["\x15", "N", "e", "w", "enter"])
    monkeypatch.setattr("hive_ide.optionsmodal.IdeOptionsModal._draw", lambda *args: None)
    monkeypatch.setattr("hive_ide.optionsmodal.IdeOptionsModal._getkey", lambda _fd: next(keys))

    assert (
        IdeOptionsModal._rename_prompt(
            0,
            {"name": "OLD"},
            "/workspace",
            0,
        )
        == ("New", False)
    )


def test_options_modal_rename_driver_checkbox_defaults_by_driver(monkeypatch):
    monkeypatch.setattr("hive_ide.optionsmodal.IdeOptionsModal._draw", lambda *args: None)

    def prompt(driver: str, keys: list[str]):
        feed = iter(keys)
        monkeypatch.setattr(
            "hive_ide.optionsmodal.IdeOptionsModal._getkey", lambda _fd: next(feed)
        )
        return IdeOptionsModal._rename_prompt(
            0, {"name": "OLD", "driver": {"id": driver}}, "/workspace", 0
        )

    assert prompt("claude", ["enter"]) == ("OLD", True)
    assert prompt("codex", ["enter"]) == ("OLD", False)
    assert prompt("claude", ["\t", "enter"]) == ("OLD", False)
    assert prompt("codex", ["\t", "enter"]) == ("OLD", True)
    # Drivers without /rename support never get the checkbox.
    assert prompt("term", ["\t", "enter"]) == ("OLD", False)


def test_options_modal_offers_clear_plan_only_when_linked():
    expected = ("plan-clear", "clear plan", "unlink the session plan")
    linked = {"driver": {"id": "claude"}, "plan": {"path": "plans/x.md"}}
    unlinked = {"driver": {"id": "claude"}, "plan": {"path": None}}

    assert expected in IdeOptionsModal._actions(linked)
    assert expected not in IdeOptionsModal._actions(unlinked)


def test_options_modal_routes_clear_plan_and_named_driver_rename(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        "hive_ide.optionsmodal.IdeNewModal._cli",
        lambda _skill_dir, args: calls.append(args) or (True, ""),
    )
    monkeypatch.setattr("hive_ide.optionsmodal.IdeNewModal._tmux_socket", "")

    IdeOptionsModal._command(tmp_path, "sid", "plan-clear")
    IdeOptionsModal._command(tmp_path, "sid", "driver-rename", name="NEW")

    assert calls == [
        ["--quiet", "plan-set", "--session-id=sid", "--clear"],
        ["--quiet", "driver-rename", "--session-id=sid", "--name=NEW"],
    ]


def test_options_modal_draws_a_record_loaded_the_way_the_popup_loads_it(tmp_path, capsys):
    # The popup reads sessions through StateIO, which flattens `plan` to a path
    # string. Drawing must survive that shape, with and without a linked plan.
    from hive_ide.drivers import bundled_drivers
    from hive_ide.state_compat import StateIO
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="ALPHA",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": "python3", "version": "test"},
        driver=bundled_drivers()["claude"].resolve(
            name="ALPHA", working_dir=str(workspace), conversation_reference=None
        ),
    )
    clear = ("plan-clear", "clear plan", "unlink the session plan")

    loaded = StateIO.find_by_id(store.home, str(workspace), record["id"])[2]
    IdeOptionsModal._draw(loaded, str(workspace), 0)
    assert clear not in IdeOptionsModal._actions(loaded)

    record["plan"] = {"path": "plans/x.md", "active_task": None}
    store.write("sessions", record["id"], record)
    loaded = StateIO.find_by_id(store.home, str(workspace), record["id"])[2]
    assert isinstance(loaded["plan"], str)
    IdeOptionsModal._draw(loaded, str(workspace), 0)
    assert clear in IdeOptionsModal._actions(loaded)
    assert "Session options" in capsys.readouterr().out
