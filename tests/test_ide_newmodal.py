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


def test_build_args_can_adopt_a_specific_conversation():
    assert IdeNewModal._build_args(
        "SESSION", "codex", adopt_reference="conversation-1"
    ) == [
        "create",
        "--name=SESSION",
        "--driver=codex",
        "--adopt",
        "--reference=conversation-1",
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


def test_do_create_uses_public_cli_and_repair(monkeypatch, tmp_path):
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
    tmux_calls = []

    def fake_run(args, **_kwargs):
        tmux_calls.append(args)
        return type("Result", (), {"stdout": "@7\tsession-id\n", "returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)
    ok, message = IdeNewModal._do_create(tmp_path, "SESSION", "antigravity")
    assert ok and message == ""
    assert calls == [
        ["create", "--name=SESSION", "--driver=antigravity"],
        ["repair", "--session-id=session-id"],
    ]
    assert ["tmux", "select-window", "-t", "@7"] in tmux_calls
    assert ["tmux", "select-pane", "-t", "@7.1"] in tmux_calls


def test_do_create_can_adopt_selected_conversation(monkeypatch, tmp_path):
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
    tmux_calls = []

    def fake_run(args, **_kwargs):
        tmux_calls.append(args)
        return type("Result", (), {"stdout": "@7\tsession-id\n", "returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)
    ok, message = IdeNewModal._do_create(
        tmp_path, "SESSION", "claude", adopt_reference="conversation-1"
    )
    assert ok and message == ""
    assert calls[0] == [
        "create",
        "--name=SESSION",
        "--driver=claude",
        "--adopt",
        "--reference=conversation-1",
    ]
    assert ["tmux", "select-window", "-t", "@7"] in tmux_calls
    assert ["tmux", "select-pane", "-t", "@7.1"] in tmux_calls


def test_do_create_surfaces_failure_without_repair(monkeypatch, tmp_path):
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


def test_modal_adoption_support_is_explicit():
    assert IdeNewModal._supports_adopt("claude")
    assert IdeNewModal._supports_adopt("codex")
    assert not IdeNewModal._supports_adopt("antigravity")
    assert not IdeNewModal._supports_adopt("term")


def test_modal_filters_adoptable_conversations():
    state = {
        "filter": "billing",
        "adopt_items": [
            {"label": "CLAUDE old", "reference": "aaa", "preview": "old archive cleanup"},
            {"label": "CODEX new", "reference": "bbb", "preview": "billing api fix"},
        ],
    }
    assert IdeNewModal._filtered_conversations(state) == [
        {"label": "CODEX new", "reference": "bbb", "preview": "billing api fix"}
    ]


def test_adopt_row_note_uses_relative_time_and_preview_only(monkeypatch):
    item = {
        "reference": "22222222-2222-4222-8222-222222222222",
        "updated_at": "2026-07-28T11:00:00+00:00",
        "preview": "Latest Hive Events allowlist work",
    }

    monkeypatch.setattr(IdeNewModal, "_rel_time", staticmethod(lambda _iso: "2h"))
    updated = item.get("updated_at")
    when = IdeNewModal._rel_time(updated if isinstance(updated, str) else None)
    preview = str(item.get("preview") or "").strip()
    note = " · ".join(part for part in (when, preview) if part)

    assert note == "2h · Latest Hive Events allowlist work"


# -- the adopt list sizes itself to the screen -------------------------------------
#
# It windowed a hardcoded 10 rows whatever the terminal was — the same mistake as
# sizing a popup by percentage, one layer down: on a short screen ten adopt rows
# plus the stage's chrome overflows, and the overflow leaves from the top.

import io
import os
from contextlib import redirect_stdout

from hive_ide import modal_io


def _draw_adopt(rows: int, conversations: int, sel: int = 0) -> list[str]:
    state = {
        "stage": "adopt",
        "name": "ALPHA",
        "err": "",
        "ty": 0,
        "mode_sel": 0,
        "adopt_sel": sel,
        "filter": "",
        # `adopt_items`, which is what `_filtered_conversations` reads — my first
        # version said "conversations" and silently rendered an empty list, so
        # both screen sizes drew the same thing and the test looked like a fix
        # that had not worked.
        "adopt_items": [
            {"title": f"conversation {i}", "updated_at": None, "preview": ""}
            for i in range(conversations)
        ],
    }
    buffer = io.StringIO()
    os.environ["LINES"] = str(rows)
    try:
        with redirect_stdout(buffer):
            IdeNewModal._draw(state)
    finally:
        os.environ.pop("LINES", None)
    text = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "")
    return [l for l in text.split("\n") if l.strip()]


def test_the_adopt_list_shrinks_with_the_screen():
    """Fewer rows on a short screen, not the same ten and an overflow."""
    tall = _draw_adopt(40, conversations=30)
    short = _draw_adopt(14, conversations=30)

    assert len(short) <= 14, f"14 rows: drew {len(short)}"
    assert len(short) < len(tall), (
        "the adopt list drew the same height on a 14-row screen as on a 40-row one"
    )


def test_the_new_session_modal_keeps_its_title_and_footer():
    for rows in (40, 14, 10, 7):
        lines = _draw_adopt(rows, conversations=30)
        # WITHIN the screen, not merely titled correctly. Without this the fit
        # backstop could be removed entirely and this test stayed green, because
        # the screen-sized adopt window kept the output small on its own — the two
        # mechanisms masked each other and a mutation run found it.
        assert len(lines) <= rows, f"{rows} rows: drew {len(lines)} lines"
        assert "New IDE session" in lines[0], f"{rows} rows: the title scrolled off"
        assert any("Esc" in l or "working" in l for l in lines), (
            f"{rows} rows: the footer was dropped"
        )


def test_the_adopt_window_reports_a_range_that_fits_the_screen():
    """The list's own "N-M of T" line is what proves the WINDOW sized itself.

    Asserting only that the modal fits cannot distinguish a screen-sized window
    from a hardcoded one that the backstop then clips — both "fit". The range line
    is the window's own account of itself, so a hardcoded ten rows shows up as
    "1-10 of 30" on a fourteen-row screen where only a few rows can exist.
    """
    import re as _re

    for rows, ceiling in ((40, 30), (14, 8), (10, 6)):
        lines = _draw_adopt(rows, conversations=30)
        ranges = [
            _re.search(r"(\d+)-(\d+) of (\d+)", l)
            for l in lines
            if _re.search(r"\d+-\d+ of \d+", l)
        ]
        assert ranges, f"{rows} rows: no range line, so the window cannot be checked"
        first, last, total = (int(g) for g in ranges[0].groups())
        assert total == 30
        assert last - first + 1 <= ceiling, (
            f"{rows} rows: window reported {first}-{last} of {total}, "
            f"more than the {ceiling} rows that can exist here"
        )


def test_the_adopt_window_never_collapses_to_nothing():
    """One row is the floor: an empty list is not a kinder failure than a cut one."""
    assert modal_io.rows_available(200) >= 1
    lines = _draw_adopt(4, conversations=30)
    assert lines, "a very short screen drew nothing at all"


def _draw_type(rows: int, ty: int) -> list[str]:
    """The `type` stage at `rows` rows with driver index `ty` selected."""
    state = {
        "stage": "type",
        "name": "ALPHA",
        "ty": ty,
        "mode_sel": 0,
        "adopt_sel": 0,
        "filter": "",
        "adopt_items": [],
    }
    buffer = io.StringIO()
    os.environ["LINES"] = str(rows)
    try:
        with redirect_stdout(buffer):
            IdeNewModal._draw(state)
    finally:
        os.environ.pop("LINES", None)
    text = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "")
    return [l for l in text.split("\n") if l.strip()]


def test_the_selected_driver_is_on_screen_at_every_selection():
    """Found by a cross-model review of the first fit pass.

    The window was anchored on the LAST body line, so on a short screen the
    trailing explanation stayed and the selected driver could be scrolled out of
    view — while Enter still created that driver. A selection the reader cannot
    see makes the next keypress a guess.
    """
    missing = []
    for rows in (40, 14, 12, 10, 8):
        for ty, (_id, label, _note) in enumerate(IdeNewModal.TYPES):
            drawn = _draw_type(rows, ty)
            assert len(drawn) <= rows, f"{rows} rows: drew {len(drawn)}"
            if not any(label in line for line in drawn):
                missing.append(f"rows={rows} selected={label!r}")
    assert not missing, "the selected driver was off screen:\n  " + "\n  ".join(
        missing
    )
