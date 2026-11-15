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


def test_fit_never_exceeds_its_row_budget():
    """Every chrome/height/length/selection combination, not a sample."""
    over = []
    for header_rows, footer_rows, rows, length in SHAPES:
        header = ["H"] * header_rows
        footer = ["F"] * footer_rows
        body = [f"b{i}" for i in range(length)]
        for selected in range(max(1, length)):
            drawn = modal_io.fit(
                header, body, footer, selected=selected, rows=rows
            )
            if len(drawn) > rows:
                over.append(
                    f"header={header_rows} footer={footer_rows} rows={rows} "
                    f"body={length} selected={selected} -> {len(drawn)} lines"
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
                header, body, footer, selected=selected, rows=rows
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
