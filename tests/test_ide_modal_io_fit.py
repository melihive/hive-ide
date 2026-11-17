"""`fit` never draws more rows than it was given, for ANY shape.

Drawing one row too many is not a cosmetic slip: the terminal scrolls, and what
leaves is the TOP — the title and the first item. That is the whole defect this
helper exists to prevent, so the budget is swept exhaustively rather than
sampled at the sizes a human happened to try.

Found by a cross-model review of the first implementation, which reserved marker
rows by iterating to a "fixed point" that does not exist: the reserve changes
which ends are cut, which changes the reserve. `fit([], list("abcd"), [],
selected=2, rows=2)` oscillated and returned three lines.
"""
from __future__ import annotations

import pytest

from hive_ide import modal_io

SHAPES = [
    (h, f, rows, n)
    for h in (0, 1, 2)
    for f in (0, 1, 2)
    for rows in range(1, 16)
    for n in range(0, 12)
]


@pytest.mark.parametrize("width,wide", [(80, False), (20, True), (12, True)])
def test_fit_never_exceeds_its_row_budget(width, wide):
    """Every chrome/height/length/selection combination, not a sample.

    Measured in PHYSICAL rows, and run once with lines that wrap at the given
    width — a budget honoured only for lines that happen to fit is not a budget.
    """
    over = []
    for header_rows, footer_rows, rows, length in SHAPES:
        header = ["H" * (width + 5) if wide else "H"] * header_rows
        footer = ["F" * (width + 3) if wide else "F"] * footer_rows
        body = [
            ("b%d " % i) * (width if wide else 1) for i in range(length)
        ]
        for selected in range(max(1, length)):
            drawn = modal_io.fit(
                header, body, footer, selected=selected, rows=rows, width=width
            )
            physical = modal_io.wrapped_rows(drawn, width)
            if physical > rows:
                over.append(
                    f"header={header_rows} footer={footer_rows} rows={rows} "
                    f"body={length} selected={selected} -> {physical} rows"
                )
    assert not over, "fit drew past the bottom:\n  " + "\n  ".join(over[:12])


def test_the_selected_row_is_always_on_screen():
    """A window that drops the selection is why the clipping was invisible.

    The reader moves the selection and the modal acts on it, so a selection the
    reader cannot see makes the next keypress a guess.
    """
    missing = []
    for header_rows, footer_rows, rows, length in SHAPES:
        if length == 0:
            continue
        header = ["H"] * header_rows
        footer = ["F"] * footer_rows
        body = [f"b{i}" for i in range(length)]
        for selected in range(length):
            drawn = modal_io.fit(
                header, body, footer, selected=selected, rows=rows, width=80
            )
            # Only meaningful where there is room for at least one body row
            # beside the chrome; below that the chrome branch owns the output.
            if rows <= header_rows + footer_rows + 1:
                continue
            if body[selected] not in drawn:
                missing.append(
                    f"header={header_rows} footer={footer_rows} rows={rows} "
                    f"body={length} selected={selected}"
                )
    assert not missing, "the selected row was off screen:\n  " + "\n  ".join(
        missing[:12]
    )


@pytest.mark.parametrize(
    "rows,length,selected",
    [(2, 4, 2), (2, 3, 1), (2, 8, 6)],
)
def test_a_budget_too_small_for_both_markers_says_cut_both_ways(
    rows, length, selected
):
    """One honest marker beats two that overrun.

    With a two-row budget and a list cut at both ends, `↑ more` AND `↓ more`
    plus the selection need three rows. Three rows IS enough for both markers
    and is left to draw them — this is only about the budget that is not.
    """
    body = [f"b{i}" for i in range(length)]
    drawn = modal_io.fit([], body, [], selected=selected, rows=rows)

    assert len(drawn) <= rows
    assert body[selected] in drawn, "the selection was dropped for a marker"
    markers = [line for line in drawn if "more" in line]
    assert len(markers) == 1, f"expected one combined marker, got {markers}"
    assert "\u2195" in markers[0], (
        f"a list cut both ways reported only one direction: {markers[0]!r}"
    )


def test_three_rows_is_enough_for_both_markers_and_still_draws_them():
    """The floor above must not become an excuse to under-report clipping."""
    body = [f"b{i}" for i in range(5)]
    drawn = modal_io.fit([], body, [], selected=2, rows=3)

    assert len(drawn) == 3
    assert drawn[0].strip().startswith("\u2191")
    assert drawn[-1].strip().startswith("\u2193")
    assert "b2" in drawn[1]


def test_width_is_counted_in_cells_not_characters():
    """A character is not a cell, and these modals are full of emoji.

    CJK and most emoji take two cells; combining marks take none. Counting
    characters under-reports the width, the line wraps where nothing expected
    it to, and the modal ends up a row taller than it asked for.
    """
    assert modal_io.printable_width("abc") == 3
    assert modal_io.printable_width("\x1b[1mabc\x1b[0m") == 3, "escapes cost cells"
    assert modal_io.printable_width("\u65e5\u672c\u8a9e") == 6, "CJK is double-width"
    assert modal_io.printable_width("e\u0301") == 1, "a combining mark costs nothing"
    assert modal_io.printable_width("   \u25b8 \U0001f506 claude") == 14
    # Zero width with combining CLASS 0, which a `combining()` test misses.
    # U+FE0F follows most emoji, so this is the common case, not the exotic one.
    for zero, name in (("\u200b", "ZWSP"), ("\u200d", "ZWJ")):
        assert modal_io.printable_width("x" + zero) == 1, f"{name} cost a cell"
        assert modal_io.cell_costs(zero) == [0], name
    # U+FE0F takes no cell of its own, but it makes the character BEFORE it two
    # cells wide, so it is not neutral in a string the way the other two are.
    assert modal_io.cell_costs("\ufe0f") == [0]
    assert modal_io.printable_width("x\ufe0f") == 2
    # Emoji presentation: what makes U+1F441 wide is the U+FE0F after it, so a
    # per-character measurement cannot see it. The sidebar already knew.
    # The bases here span THREE categories on purpose — So, Sm and Ll — because
    # scoping this to So (one round of mine did) measures the last two as one
    # cell, and U+2139's category is the same as plain `x`.
    for emoji in (
        "\U0001f441\ufe0f",  # So  eye
        "\u2764\ufe0f",       # So  heart
        "\u2194\ufe0f",       # Sm  left-right arrow
        "\u2139\ufe0f",       # Ll  information
    ):
        assert modal_io.printable_width(emoji) == 2, repr(emoji)
    assert modal_io.printable_width("\U0001f9e0") == 2, "wide without a selector"
    assert modal_io.printable_width("\u25b6") == 1, "narrow without a selector"


def test_fit_budgets_physical_rows_not_logical_lines():
    """A line wider than the screen wraps, and the budget must know.

    Found by a second cross-model review: the popup had been taught to count
    wrapped rows while `fit`, one level down, still counted lines — so a narrow
    popup overflowed by exactly the number of wrapped lines, and the row that
    leaves is the title.
    """
    width = 20
    body = ["x" * 45] + [f"short {i}" for i in range(6)]
    for rows in range(3, 14):
        drawn = modal_io.fit(
            ["H"], body, ["F"], selected=0, rows=rows, width=width
        )
        physical = modal_io.wrapped_rows(drawn, width)
        assert physical <= rows, (
            f"{rows} rows: drew {len(drawn)} lines occupying {physical} rows"
        )


def test_a_second_selection_is_kept_on_screen_when_it_fits():
    """Two independent selections both decide what Enter does."""
    body = [f"b{i}" for i in range(12)]
    drawn = modal_io.fit([], body, [], selected=2, rows=8, width=80, also_selected=7)
    assert "b2" in drawn and "b7" in drawn, drawn

    # When the span cannot fit, the primary selection wins rather than neither.
    tight = modal_io.fit([], body, [], selected=1, rows=3, width=80, also_selected=11)
    assert "b1" in tight
    assert modal_io.wrapped_rows(tight, 80) <= 3


def test_rows_are_counted_by_where_characters_LAND_not_by_division():
    """A double-width character cannot straddle the right edge.

    Dividing total cells by the width assumes every cell is usable. It is not:
    a wide character that will not fit in the last cell of a row is pushed whole
    onto the next row and that cell is wasted. `\u754c\u754c\u754c` is six cells, but at
    width 3 it needs three rows, not two — and a modal that believes the
    division draws past the bottom.
    """
    assert modal_io.line_rows("\u754c\u754c\u754c", 3) == 3
    assert modal_io.line_rows("\u754c\u754c\u754c", 6) == 1
    assert modal_io.line_rows("abc", 3) == 1
    assert modal_io.line_rows("abcd", 3) == 2
    assert modal_io.wrapped_rows(["\u754c\u754c\u754c"], 3) == 3


@pytest.mark.parametrize("width", [3, 4, 5, 7, 12])
def test_fit_respects_its_budget_with_double_width_content(width):
    """The budget must hold for content that wraps awkwardly, not just neatly."""
    body = ["\u754c" * n for n in range(1, 7)] + ["mixed \u754c\u754c text"]
    for rows in range(2, 14):
        for selected in range(len(body)):
            drawn = modal_io.fit(
                ["H\u754c"], body, ["F"], selected=selected, rows=rows, width=width
            )
            physical = modal_io.wrapped_rows(drawn, width)
            assert physical <= rows, (
                f"width={width} rows={rows} selected={selected}: "
                f"drew {physical} rows"
            )


def test_truncation_cuts_a_straddling_wide_character_too():
    """Half a double-width character is not half a cell."""
    cut = modal_io.truncate_to_rows("\u754c\u754c\u754c", 1, 3)
    assert modal_io.line_rows(cut, 3) == 1
    assert cut == "\u754c", f"expected one wide char, got {cut!r}"


def test_a_hint_footer_is_shed_only_when_it_costs_the_selected_row():
    """`footer_hints` buys the selection with the hint — never on principle.

    The trade the user settled on 2026-11-17: a screen too small for both does
    not get key hints. So the hint goes ONLY where keeping it would push the
    selected row off, and a roomy screen still draws both.
    """
    header = ["Session options"]
    body = [f"  action {n}" for n in range(12)]
    hint = ["  " + "up/down or j/k . Enter . Esc -> cancel" * 2]

    roomy = modal_io.fit(
        header, body, hint, selected=4, rows=12, width=60,
        pin_selection=True, footer_hints=1,
    )
    assert hint[0] in roomy, "the hint was shed although both fit"
    assert body[4] in roomy, "the selection is missing on a roomy screen"

    # 5 rows at 20 cells: the title takes one and the wrapped hint takes four,
    # so the selected row has nowhere to go until the hint gives way. (At 6 rows
    # all three fit exactly, which is why that size keeps its hint.)
    assert modal_io.wrapped_rows(hint, 20) == 4, "the fixture stopped over-filling"
    tight = modal_io.fit(
        header, body, hint, selected=4, rows=5, width=20,
        pin_selection=True, footer_hints=1,
    )
    assert body[4] in tight, "the selection was lost to a hint that could be shed"
    assert hint[0] not in tight, "the hint survived at the cost of the selection"


def test_a_footer_that_is_not_a_hint_is_never_shed():
    """The options modal's rename field lives in the footer, above the hint.

    Typing blind into a field Enter will commit is the defect the pinning order
    exists to stop, so only the trailing `footer_hints` lines may go — the field
    above them stays even when that costs the selected row.
    """
    header = ["Session options"]
    body = [f"  action {n}" for n in range(12)]
    field = "  new name: ALPHA"
    hint = "  " + "up/down or j/k . Enter . Esc -> cancel" * 2

    drawn = modal_io.fit(
        header, body, [field, hint], selected=4, rows=6, width=20,
        pin_selection=True, footer_hints=1,
    )
    assert field in drawn, "the rename field was shed as if it were a hint"
    assert hint not in drawn, "the hint should have gone first"


def test_without_footer_hints_the_footer_is_pinned_exactly_as_before():
    """Default `footer_hints=0` keeps the old contract for every other caller.

    The new-session modal relies on this once its footer names a selection the
    window cut: that line is the only thing saying what Enter will do.
    """
    header = ["New session"]
    body = [f"  driver {n}" for n in range(8)]
    footer = ["  " + "claude . codex . adopt existing . Esc -> cancel" * 2]

    drawn = modal_io.fit(
        header, body, footer, selected=3, rows=6, width=20, pin_selection=True,
    )
    assert footer[0] in drawn, "a non-hint footer was dropped"
