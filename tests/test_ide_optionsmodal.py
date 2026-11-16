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


def _click(row: int, rows: dict[int, int]) -> int | None:
    """What a left-click on 1-based screen `row` selects."""
    return IdeOptionsModal._mouse_selection(
        f"\x1b[<0;12;{row}M".encode(), rows
    )


def test_options_modal_mouse_click_maps_action_rows(tmp_path):
    """The map comes from the draw, so it describes the screen as drawn."""
    store, record = _session(tmp_path)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        os.environ["LINES"] = "200"
        try:
            rows = IdeOptionsModal._draw(record, store.workspace_key, 0)
        finally:
            os.environ.pop("LINES", None)
    drawn = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "").split("\n")
    actions = IdeOptionsModal._actions(record)

    assert rows, "the draw reported no clickable rows"
    for screen_row, index in rows.items():
        label = actions[index][1]
        assert label in drawn[screen_row - 1], (
            f"row {screen_row} maps to {label!r} but draws "
            f"{drawn[screen_row - 1]!r}"
        )
    # A non-action row and a non-left button select nothing.
    assert _click(1, rows) is None, "the title row is not clickable"
    assert IdeOptionsModal._mouse_selection(b"\x1b[<2;12;5M", rows) is None


def test_a_click_after_clipping_runs_the_action_on_that_row(tmp_path):
    """The regression a cross-model review caught.

    A click goes straight to Enter, so a stale row map does not mis-highlight —
    it RUNS the wrong action, and `archive` is one of the actions a misdirected
    click could land on. Once the list is windowed, the rows the full action
    list would have occupied are no longer the rows on screen.
    """
    store, record = _session(tmp_path)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        os.environ["LINES"] = "12"
        try:
            rows = IdeOptionsModal._draw(record, store.workspace_key, 0)
        finally:
            os.environ.pop("LINES", None)
    drawn = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "").split("\n")
    actions = IdeOptionsModal._actions(record)

    assert len(drawn) <= 12, f"drew {len(drawn)} lines into 12 rows"
    assert rows, "a clipped modal reported no clickable rows at all"
    for screen_row, index in rows.items():
        assert 1 <= screen_row <= len(drawn)
        label = actions[index][1]
        assert label in drawn[screen_row - 1], (
            f"clipped: row {screen_row} would run {label!r} but draws "
            f"{drawn[screen_row - 1]!r}"
        )
    # Nothing off-screen remains clickable, and the footer is not an action.
    assert max(rows) <= len(drawn)
    footer_row = len(drawn)
    assert footer_row not in rows, "the footer row maps to an action"


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


# -- fitting the screen it is actually on -------------------------------------------
#
# Reported from a handheld as "hides first item", reproduced at 80x20: the pane began with
# the subtitle because the bold title had already scrolled off. Two causes — the
# modal drew 20 rows with no height budget, and the popup was sized by percentage
# rather than by content, so 92% of a short screen minus tmux's popup border was
# less than the modal needed and the overflow left from the top.

import argparse
import io
import os
import re
import uuid
from contextlib import redirect_stdout

import pytest

from hive_ide import modal_io
from hive_ide.popup import MIN_POPUP_ROWS, _content_rows
from hive_ide.store import StateStore


def _session(tmp_path, **extra) -> tuple[StateStore, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="ALPHA",
        working_dir=workspace,
        source={},
        driver={"id": "claude"},
        **extra,
    )
    return store, record


def _drawn_lines(record: dict, repo: str, rows: int) -> list[str]:
    """What `_draw` actually puts on a terminal of `rows` rows."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        os.environ["LINES"] = str(rows)
        try:
            IdeOptionsModal._draw(record, repo, 0)
        finally:
            os.environ.pop("LINES", None)
    text = buffer.getvalue()
    # Strip the clear-screen prefix, then count display lines.
    return text.replace("\x1b[2J", "").replace("\x1b[H", "").split("\n")


def test_the_row_count_popup_uses_matches_what_draw_actually_draws(tmp_path):
    """The two must not drift, or the clipping comes straight back.

    `popup._content_rows` tells tmux how tall to make the popup WITHOUT rendering,
    so it re-derives the chrome `_draw` adds. That is a duplicate of an arithmetic
    — the kind that agrees with itself while the real output moves — so it is
    pinned against the drawing, on a terminal tall enough that nothing is clipped.
    """
    store, record = _session(tmp_path)
    args = argparse.Namespace(
        state_home=str(store.home),
        workspace_key=store.workspace_key,
        session_id=record["id"],
    )

    claimed = _content_rows("options", args)
    drawn = len(_drawn_lines(record, store.workspace_key, rows=200))

    assert claimed == drawn, (
        f"popup would request {claimed} rows for a modal that draws {drawn}"
    )


def test_the_title_and_footer_survive_a_short_screen(tmp_path):
    """The reported symptom, as a test: the title must not be what gets dropped."""
    store, record = _session(tmp_path)

    for rows in (20, 14, 10, 7):
        lines = [l for l in _drawn_lines(record, store.workspace_key, rows=rows) if l]
        assert len(lines) <= rows, f"{rows} rows: drew {len(lines)} lines"
        assert "Session options" in lines[0], (
            f"{rows} rows: the title is not the first line — it scrolled off"
        )
        assert any("Esc" in l for l in lines), f"{rows} rows: the footer was dropped"


def test_the_selected_row_stays_on_screen_wherever_it_is(tmp_path):
    """A selection you cannot see is worse than a clipped list."""
    store, record = _session(tmp_path)
    actions = IdeOptionsModal._actions(record)

    for sel in range(len(actions)):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            os.environ["LINES"] = "10"
            try:
                IdeOptionsModal._draw(record, store.workspace_key, sel)
            finally:
                os.environ.pop("LINES", None)
        text = buffer.getvalue()
        label = actions[sel][1]
        assert "▸" in text, f"selection {sel}: no selection arrow on screen"
        assert label in text, f"selection {sel} ({label}) was scrolled out of view"


def test_a_clipped_list_says_so(tmp_path):
    """Silently showing part of a list reads as the whole list."""
    store, record = _session(tmp_path)

    short = _drawn_lines(record, store.workspace_key, rows=10)
    tall = _drawn_lines(record, store.workspace_key, rows=200)

    assert any("more" in l for l in short), "a clipped list showed no marker"
    assert not any("more" in l for l in tall), "an unclipped list claimed there was more"


def test_the_sizing_hint_abstains_rather_than_guesses(tmp_path):
    """None keeps the old percentage. A wrong number clips or over-fills."""
    store, record = _session(tmp_path)
    good = argparse.Namespace(
        state_home=str(store.home),
        workspace_key=store.workspace_key,
        session_id=record["id"],
    )

    assert _content_rows("options", good) > MIN_POPUP_ROWS
    assert _content_rows("card", good) is None, "a kind that cannot say must abstain"
    assert _content_rows(
        "options",
        argparse.Namespace(
            state_home=str(store.home),
            workspace_key=store.workspace_key,
            session_id=f"missing-{uuid.uuid4().hex}",
        ),
    ) is None, "a session it cannot read must abstain, not guess"


def test_click_rows_are_physical_rows_even_when_lines_wrap(tmp_path):
    """Found by a second cross-model review.

    The map was keyed by LINE NUMBER. A line wider than the popup wraps onto a
    second row and shifts everything below it down, so at a narrow width a
    click ran a different action than the row it landed on — measured at an
    interior of 38 cells, eleven rows were wrong, and a click acts immediately.
    """
    from hive_ide import modal_io

    store, record = _session(tmp_path)
    actions = IdeOptionsModal._actions(record)

    for interior in (30, 38, 55, 72):
        lines, mouse_rows = IdeOptionsModal._lines(
            record, store.workspace_key, 0, rows=10_000, width=interior
        )
        # The physical row each line starts on, and the rows it occupies.
        occupies: dict[int, str] = {}
        row = 1
        for line in lines:
            cost = max(1, -(-modal_io.printable_width(line) // interior))
            for offset in range(cost):
                occupies[row + offset] = line
            row += cost

        assert mouse_rows, f"interior {interior}: no clickable rows"
        for screen_row, index in mouse_rows.items():
            label = actions[index][1]
            assert label in occupies.get(screen_row, ""), (
                f"interior {interior}: clicking row {screen_row} runs "
                f"{label!r} but that row draws {occupies.get(screen_row)!r}"
            )


def test_a_truncated_action_is_still_clickable(tmp_path):
    """Found by a third cross-model review.

    A screen too short for its own chrome gets ONE truncated line. That line is
    a new string, so the identity-keyed map lost it — and it can be a visible,
    selected action, which then looked clickable while clicking did nothing.
    """
    from hive_ide import modal_io

    store, record = _session(tmp_path)
    actions = IdeOptionsModal._actions(record)

    lines, mouse_rows = IdeOptionsModal._lines(
        record, store.workspace_key, 0, rows=6, width=38
    )
    shown = [modal_io.strip_ansi(line) for line in lines]
    action_rows = [
        i for i, text in enumerate(shown, start=1) if "\u25b8" in text
    ]
    assert action_rows, f"no action was drawn at all: {shown}"

    for row in action_rows:
        assert row in mouse_rows, (
            f"row {row} draws an action ({shown[row - 1]!r}) but is not clickable"
        )
        label = actions[mouse_rows[row]][1]
        # The drawn text is a prefix of the full action line, so the label may
        # itself be cut — compare on the part that survived.
        assert shown[row - 1].strip().lstrip("\u25b8 ").startswith(label[:4]), (
            f"row {row} maps to {label!r} but draws {shown[row - 1]!r}"
        )


def test_an_ambiguous_truncation_is_left_unmapped():
    """A prefix that two actions could own must not guess between them."""
    body = ["  x same prefix A", "  x same prefix B"]
    action_of_line = {id(body[0]): 0, id(body[1]): 1}
    assert IdeOptionsModal._action_for_truncated(
        "  x same prefix", body, action_of_line
    ) is None
    assert IdeOptionsModal._action_for_truncated(
        "  x same prefix A", body, action_of_line
    ) == 0
    assert IdeOptionsModal._action_for_truncated("   ", body, action_of_line) is None


def test_an_emoji_in_the_repo_name_does_not_misdirect_a_click(tmp_path):
    """Found by a fourth cross-model review.

    `U+1F441 U+FE0F` is East_Asian_Width Neutral — what makes it two cells is
    the variation selector AFTER it. Measuring it as one cell made the subtitle
    wrap a row earlier than predicted, which shifted every action below it, and
    a click then ran a different action. The sidebar already measured this
    correctly; the modals had grown a second, worse copy.
    """
    from hive_ide import modal_io

    # U+2139 is category Ll, the same as a plain letter, so a category-scoped
    # rule measures it as one cell and the subtitle wraps a row early.
    workspace = tmp_path / "rrrrrrrrr\u2139\ufe0f"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="ALPHA", working_dir=workspace, source={}, driver={"id": "claude"}
    )
    actions = IdeOptionsModal._actions(record)

    for interior in (28, 30, 38):
        lines, mouse_rows = IdeOptionsModal._lines(
            record, store.workspace_key, 0, rows=12, width=interior
        )
        occupies: dict[int, str] = {}
        row = 1
        for line in lines:
            cost = modal_io.line_rows(line, interior)
            for offset in range(cost):
                occupies[row + offset] = line
            row += cost

        for screen_row, index in mouse_rows.items():
            label = actions[index][1]
            assert label in modal_io.strip_ansi(occupies.get(screen_row, "")), (
                f"interior {interior}: clicking row {screen_row} runs {label!r} "
                f"but that row draws {occupies.get(screen_row)!r}"
            )


def test_the_rename_field_survives_a_screen_too_small_for_the_chrome(tmp_path):
    """Found by a sixth cross-model review.

    When not even the pinned chrome fits, `fit` used to clip
    `header + body + footer` front to back — so body rows displaced the FOOTER,
    and the rename field lives in the footer. At a 74x6 interior two different
    names drew identical frames with no rename field at all, while Enter still
    committed the edit.
    """
    from hive_ide import modal_io

    store, record = _session(tmp_path)

    def frame(value: str, rows: int) -> list[str]:
        lines, _ = IdeOptionsModal._lines(
            record, store.workspace_key, 0,
            rename_value=value, driver_too=False, rows=rows, width=74,
        )
        return lines

    for rows in (4, 5, 6, 8, 12, 20):
        alpha, beta = frame("ALPHA", rows), frame("BETA", rows)
        assert modal_io.wrapped_rows(alpha, 74) <= rows, f"{rows}: overran"
        assert any("new name" in modal_io.strip_ansi(l) for l in alpha), (
            f"{rows} rows: the rename field was not drawn"
        )
        assert alpha != beta, f"{rows} rows: two names drew the same frame"


def test_the_rename_field_survives_every_width_and_value(tmp_path):
    """Swept, because the first rename test used one width (74) and missed this.

    The rename value is unbounded by display width: a valid 24-character name
    wrapped to three rows at a 16-cell interior and was dropped whole, so two
    different edits drew the same frame while Enter still returned the edited
    value. It trims to its row like every other field now.
    """
    from hive_ide import modal_io

    store, record = _session(tmp_path)
    bad = []
    for width in range(14, 84, 2):
        for rows in range(4, 25):
            for value in ("X" * 4, "X" * 24, "N" * 24 + "X", "\u65e5" * 12):
                drawn, _ = IdeOptionsModal._lines(
                    record, store.workspace_key, 0, rename_value=value,
                    driver_too=False, rows=rows, width=width,
                )
                other, _ = IdeOptionsModal._lines(
                    record, store.workspace_key, 0,
                    rename_value=value[:-1] + "Y",
                    driver_too=False, rows=rows, width=width,
                )
                if modal_io.wrapped_rows(drawn, width) > rows:
                    bad.append(f"{width}x{rows} len={len(value)}: overran")
                if not any("new name" in modal_io.strip_ansi(l) for l in drawn):
                    bad.append(f"{width}x{rows} len={len(value)}: no field")
                if drawn == other:
                    bad.append(f"{width}x{rows} len={len(value)}: identical")
    assert not bad, "\n  ".join(["the rename field was lost:"] + bad[:12])


def _options_frame(record: dict, repo: str, rows: int, width: int, sel: int):
    buffer = io.StringIO()
    os.environ["LINES"] = str(rows)
    os.environ["COLUMNS"] = str(width)
    try:
        with redirect_stdout(buffer):
            IdeOptionsModal._draw(record, repo, sel)
    finally:
        os.environ.pop("LINES", None)
        os.environ.pop("COLUMNS", None)
    text = buffer.getvalue().replace("\x1b[2J", "").replace("\x1b[H", "")
    return tuple(l for l in text.split("\n") if l.strip())


def test_which_action_enter_will_run_survives_a_screen_too_small(tmp_path):
    """Enter must never fire an action the screen is not showing.

    When the pinned chrome alone overflowed, the whole body was given up — so
    at a 20x8 interior all twelve actions, `archive` among them, drew ONE
    identical frame with no selection anywhere, while Enter still ran the one
    the invisible cursor was on. That was 96 of the 735 swept sizes, up to
    interior height eleven — well above the sub-height-eight limitation this
    file already documents.

    The selected row now outranks everything but the title and the footer.

    GUARANTEED, no exceptions: every interior at least 6 rows tall AND at least
    34 cells wide draws all twelve actions distinguishably — swept, 0 of 735
    violations.

    LIMIT, stated not hidden: 43 sizes below that still draw some selections
    alike — interiors 4-5 rows tall at any width, and interiors 24 cells or
    narrower at 6-11 rows. There the footer, wrapped at that width, fills the
    screen before the selection gets a row. Letting the selection outrank the
    footer is a trade this phase did not take: the footer carries the escape
    hint and, while renaming, the field being typed into. It is a task on the
    plan, not a silent gap. (An interior of 4 rows means a client terminal of
    6 — the popup floor.)
    """
    store, record = _session(tmp_path)
    repo = store.workspace_key
    actions = IdeOptionsModal._actions(record)
    assert len(actions) > 1, "a one-action modal cannot show this"

    blind = []
    for rows in range(4, 25):
        for width in range(14, 84, 2):
            frames = {
                _options_frame(record, repo, rows, width, sel)
                for sel in range(len(actions))
            }
            if len(frames) != len(actions) and rows >= 6 and width >= 34:
                blind.append(f"{width}x{rows}: {len(frames)} of {len(actions)}")
    assert not blind, "\n  ".join(
        ["selections were indistinguishable inside the guarantee:"] + blind[:12]
    )

    # The size Codex reported is fully distinguishable, archive included.
    frames = {
        _options_frame(record, repo, 8, 20, sel) for sel in range(len(actions))
    }
    assert len(frames) == len(actions), f"20x8: {len(frames)} of {len(actions)}"
