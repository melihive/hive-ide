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


def test_the_chosen_mode_is_distinguishable_at_every_height():
    """Found by a second cross-model review.

    `New session` and `Adopt existing` are a separate selection from the driver,
    and Enter does different things for each — yet at 10, 12 and 14 rows the two
    drew IDENTICAL screens, because the window was anchored on the driver and the
    mode row was clipped away. Where both fit, both are shown; where they cannot,
    the pinned footer names the one that was cut.
    """
    same = []
    for rows in (6, 8, 10, 12, 14, 20, 40):
        new = _draw_type_mode(rows, mode_sel=0)
        adopt = _draw_type_mode(rows, mode_sel=1)
        assert len(new) <= rows and len(adopt) <= rows
        if new == adopt:
            same.append(rows)
    assert not same, (
        "two different modes drew the same screen at heights: "
        + ", ".join(str(r) for r in same)
    )


def _draw_type_mode(rows: int, mode_sel: int) -> list[str]:
    state = {
        "stage": "type",
        "name": "ALPHA",
        "ty": 0,
        "mode_sel": mode_sel,
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


def test_the_name_being_typed_is_never_windowed_away():
    """Found by a fourth cross-model review.

    The code that writes this line says "Name — always on screen", and the
    window broke it: at a short interior the field being typed into scrolled
    off, so two different names drew identical frames while the keystrokes
    still landed. The name is header now, not body, so it cannot be clipped.
    """
    from hive_ide import modal_io

    for rows in (4, 6, 8, 12, 20):
        alpha = _draw_name(rows, "ALPHA")
        beta = _draw_name(rows, "BETA")
        assert len(alpha) <= rows, f"{rows} rows: drew {len(alpha)}"
        assert any("ALPHA" in modal_io.strip_ansi(l) for l in alpha), (
            f"{rows} rows: the name being typed was not on screen: {alpha}"
        )
        assert alpha != beta, (
            f"{rows} rows: two different names drew the same frame"
        )


def _draw_name(rows: int, name: str) -> list[str]:
    state = {
        "stage": "name",
        "name": name,
        "err": "",
        "ty": 0,
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


def _draw_adopt_filter(rows: int, filter_text: str, count: int = 12) -> list[str]:
    state = {
        "stage": "adopt",
        "name": "ALPHA",
        "err": "",
        "ty": 0,
        "mode_sel": 0,
        "adopt_sel": 0,
        "filter": filter_text,
        "adopt_items": [
            {"title": f"al conversation {i}", "updated_at": None, "preview": "x" * 40}
            for i in range(count)
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


def test_the_adopt_filter_being_typed_is_never_windowed_away():
    """Found by a fifth cross-model review.

    Pinning only the name was not enough: the adopt filter sits after the
    workspace hint, so it stayed in the windowed body and scrolled off. At 80x8
    the filters `a` and `al` drew identical frames — with the same matching
    conversations — while the keystrokes still landed. Any field the reader is
    typing into is pinned; a clipped LIST at least carries a marker saying so,
    a clipped input carries nothing.
    """
    from hive_ide import modal_io

    for rows in (4, 5, 6, 8, 10, 16, 24):
        narrow = _draw_adopt_filter(rows, "a")
        wider = _draw_adopt_filter(rows, "al")
        assert len(narrow) <= rows, f"{rows} rows: drew {len(narrow)}"
        assert any("Filter:" in modal_io.strip_ansi(l) for l in narrow), (
            f"{rows} rows: the filter being typed was not on screen: {narrow}"
        )
        assert narrow != wider, (
            f"{rows} rows: two different filters drew the same frame"
        )


def _frame(rows: int, width: int, stage: str, **kw) -> list[str]:
    state = {
        "stage": stage,
        "name": kw.get("name", "ALPHA"),
        "err": "",
        "ty": 0,
        "mode_sel": 0,
        "adopt_sel": 0,
        "filter": kw.get("filter", ""),
        "adopt_items": [
            {"title": f"conversation {i}", "updated_at": None, "preview": "y" * 30}
            for i in range(8)
        ],
    }
    buffer = io.StringIO()
    os.environ["LINES"] = str(rows)
    os.environ["COLUMNS"] = str(width)
    try:
        with redirect_stdout(buffer):
            IdeNewModal._draw(state)
    finally:
        os.environ.pop("LINES", None)
        os.environ.pop("COLUMNS", None)
    text = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "")
    return [l for l in text.split("\n") if l.strip()]


def test_the_active_field_is_on_screen_at_every_size_the_popup_can_make():
    """The invariant: the field taking the keystrokes is always visible.

    Found by a sixth cross-model review in its wrapped form — the decoration
    check counted logical lines while fitting counts physical rows, so at a
    20x8 adopt interior the wrapped decoration ate the space and the filter was
    clipped anyway. Four rows is the smallest interior the popup can produce
    (it floors at six rows, less a two-row border), so that is the floor here.
    """
    from hive_ide import modal_io

    missing = []
    for rows in (4, 5, 6, 8, 10, 16, 24):
        for width in (20, 40, 74):
            for stage, field, edits in (
                ("adopt", "Filter:", ("conversatio", "conversation")),
                ("name", "Name:", ("ALPHA", "BETA")),
            ):
                key = "filter" if stage == "adopt" else "name"
                first = _frame(rows, width, stage, **{key: edits[0]})
                second = _frame(rows, width, stage, **{key: edits[1]})
                used = modal_io.wrapped_rows(first, width)
                if used > rows:
                    missing.append(f"{stage} {width}x{rows}: used {used} rows")
                if not any(field in modal_io.strip_ansi(l) for l in first):
                    missing.append(f"{stage} {width}x{rows}: {field} not drawn")
                if first == second:
                    missing.append(
                        f"{stage} {width}x{rows}: two edits drew one frame"
                    )
    assert not missing, "\n  ".join(["the active field was lost:"] + missing)
