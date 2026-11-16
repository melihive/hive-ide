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


def test_draw_shows_explicit_handoff_mode_selector(capsys):
    IdeAgentModal._draw("SESSION", "/repo", "codex", 0, False)
    quick = capsys.readouterr().out
    assert "mode:" in quick
    assert "quick switch" in quick
    assert "handoff package" in quick

    IdeAgentModal._draw("SESSION", "/repo", "codex", 0, True)
    package = capsys.readouterr().out
    assert "mode:" in package
    assert "quick switch" in package
    assert "handoff package" in package


# -- fitting a short screen --------------------------------------------------------

import io
import os
from contextlib import redirect_stdout


def _agent_lines(rows: int, sel: int = 0) -> list[str]:
    buffer = io.StringIO()
    os.environ["LINES"] = str(rows)
    try:
        with redirect_stdout(buffer):
            IdeAgentModal._draw("ALPHA", "repo", "claude", sel, False)
    finally:
        os.environ.pop("LINES", None)
    text = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "")
    return [l for l in text.split("\n") if l.strip()]


def test_the_agent_modal_keeps_its_title_and_footer_on_a_short_screen():
    """Nine rows fits most places and clipped under ten, silently from the top."""
    for rows in (20, 10, 8, 6):
        lines = _agent_lines(rows)
        assert len(lines) <= rows, f"{rows} rows: drew {len(lines)}"
        assert "Change agent" in lines[0], f"{rows} rows: the title scrolled off"
        assert any("Esc" in l for l in lines), f"{rows} rows: the footer was dropped"


def test_the_agent_modal_keeps_the_selected_driver_visible():
    for sel in range(len(IdeNewModal.TYPES)):
        text = "\n".join(_agent_lines(8, sel))
        label = IdeNewModal.TYPES[sel][1]
        assert label in text, f"driver {sel} ({label}) was scrolled out of view"


def _agent_frame(rows: int, width: int, sel: int, handoff: bool = False):
    import io
    import os
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    os.environ["LINES"] = str(rows)
    os.environ["COLUMNS"] = str(width)
    try:
        with redirect_stdout(buffer):
            IdeAgentModal._draw("ALPHA", "hive-ide", "claude", sel, handoff)
    finally:
        os.environ.pop("LINES", None)
        os.environ.pop("COLUMNS", None)
    text = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "")
    return tuple(l for l in text.split("\n") if l.strip())


def test_which_agent_enter_will_switch_to_survives_a_small_screen():
    """The fourth modal gets the same guarantee as the options list.

    When the pinned chrome alone overflowed, the body was given up whole and
    every agent drew the same frame while Enter still switched to the invisible
    one. The picked row now outranks everything but the title and the footer.

    GUARANTEED, no exceptions: every interior at least 6 rows tall AND at least
    34 cells wide tells the four agents apart — swept, 0 violations.

    LIMIT, stated not hidden: 47 sizes below that still draw some alike, all at
    interiors 4-5 rows tall or 32 cells and narrower, where the footer wrapped
    at that width fills the screen first. Same trade as the options modal.
    """
    choices = len(IdeNewModal.TYPES)
    blind = []
    for rows in range(4, 25):
        for width in range(14, 84, 2):
            frames = {_agent_frame(rows, width, sel) for sel in range(choices)}
            if len(frames) != choices and rows >= 6 and width >= 34:
                blind.append(f"{width}x{rows}: {len(frames)} of {choices}")
    assert not blind, "\n  ".join(
        ["the picked agent was indistinguishable:"] + blind[:12]
    )
