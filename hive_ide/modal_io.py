"""Small terminal input and LAYOUT helpers shared by the modal popups."""

from __future__ import annotations

import os
import re
import sys
import unicodedata

#: What to assume when the terminal will not say how tall it is. Low on purpose:
#: guessing large is what loses content off the top, and guessing small only
#: costs a scroll indicator on a screen that had room.
DEFAULT_ROWS = 24

#: CSI escapes, which cost no cells on screen. Same pattern the sidebar uses,
#: which covers more than SGR alone (`\x1b[?25l`, `\x1b[0 q`).
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def strip_ansi(line: str) -> str:
    """`line` without the escapes that colour it — its plain text."""
    return _ANSI.sub("", line)


#: Categories that take no cell at all: non-spacing and enclosing marks, and
#: format characters. A nonzero combining CLASS does not catch them — U+FE0F,
#: the variation selector that follows most emoji, has combining class 0 and
#: category Mn, and U+200B/U+200D are Cf.
_ZERO_WIDTH_CATEGORIES = frozenset({"Mn", "Me", "Cf"})


#: An explicit request for emoji presentation, which makes the character before
#: it double-width whatever its East_Asian_Width says.
_VARIATION_SELECTOR_16 = "\ufe0f"


def cell_costs(text: str) -> list[int]:
    """The cell cost of each character of `text`, escapes already stripped.

    Needs the whole string, not one character at a time: `U+1F441` EYE is
    East_Asian_Width Neutral, so it measures 1 while every terminal draws it
    across 2 cells — what makes it wide is the `U+FE0F` that FOLLOWS it. The
    sidebar learned this before the modals did; this is now the one copy.
    """
    costs = []
    for index, char in enumerate(text):
        if (
            unicodedata.combining(char)
            or unicodedata.category(char) in _ZERO_WIDTH_CATEGORIES
        ):
            costs.append(0)
        elif unicodedata.east_asian_width(char) in "WF":
            costs.append(2)
        elif text[index + 1 : index + 2] == _VARIATION_SELECTOR_16:
            # Anything before U+FE0F is two cells. I narrowed this to category
            # So for one round, to stop `x` + U+FE0F measuring two — and broke
            # real emoji: `ℹ️` is category Ll, the SAME category as `x`, and
            # `↔️` is Sm. The Emoji property is what distinguishes them and
            # `unicodedata` does not expose it, so category cannot do this job.
            # A selector after a non-emoji is a meaningless sequence no caller
            # here produces; every real one is wide. Breadth is correct.
            costs.append(2)
        else:
            costs.append(1)
    return costs


def printable_width(line: str) -> int:
    """CELLS `line` occupies — not characters, and ignoring colour escapes.

    A character is not a cell: CJK and most emoji are double-width, marks and
    format characters are zero-width. Counting characters under-reports the
    width, the line wraps where nothing expected it to, and the modal is a row
    taller than it asked for. These modals already carry emoji in every driver
    label, so this is not a hypothetical.
    """
    return sum(cell_costs(strip_ansi(line)))


def line_rows(line: str, width: int) -> int:
    """Rows `line` occupies at `width`, by where the characters actually land.

    Dividing total cells by the width is wrong at the wrap boundary: a
    double-width character that cannot fit in the last cell of a row is pushed
    whole onto the next one, and that unused cell is lost. `界界界` at width 3
    takes three rows, not the two the division claims — and the modal then
    draws past the bottom, which is the defect this module exists to stop.
    """
    if width <= 0:
        return 1
    rows = 1
    used = 0
    plain = strip_ansi(line)
    for cost in cell_costs(plain):
        if cost == 0:
            continue
        if used + cost > width:
            rows += 1
            used = cost
        else:
            used += cost
    return rows


def wrapped_rows(lines: list[str], width: int) -> int:
    """Terminal rows `lines` occupy once the terminal wraps them at `width`.

    A modal that counts LOGICAL lines under-reports its height the moment one
    line is wider than the popup it is drawn in: the terminal wraps it onto a
    second row, the content is one row taller than requested, and the row that
    leaves is the top one. The footer is the usual offender because it is the
    widest line in the modal.
    """
    if width <= 0:
        return len(lines)
    return sum(line_rows(line, width) for line in lines)

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - non-posix fallback
    termios = None
    tty = None


def wait_any_key(prompt: str = "(press any key to close)") -> None:
    """Wait for one keypress without requiring Enter."""
    if not sys.stdin.isatty():
        return
    print(f"\n  {prompt}", end="", flush=True)
    fd = sys.stdin.fileno()
    if not (termios and tty):
        try:
            sys.stdin.read(1)
        except OSError:
            pass
        return
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        os.read(fd, 1)
    except OSError:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def rows_available(reserve: int = 0, *, default: int = DEFAULT_ROWS) -> int:
    """How many rows this modal may actually draw into.

    Asked of the terminal, then `$LINES`, then a conservative default — because a
    modal that assumes a height it does not have writes past the bottom, the
    terminal scrolls, and the lines that disappear are the ones at the TOP: the
    title and the first item. That is the handheld report, reproduced at 80x20, where
    the capture began with the subtitle because the header had already gone.
    """
    rows = 0
    try:
        rows = os.get_terminal_size(sys.stdout.fileno()).lines
    except (OSError, ValueError):
        rows = 0
    if rows <= 0:
        try:
            rows = int(os.environ.get("LINES") or 0)
        except ValueError:
            rows = 0
    if rows <= 0:
        rows = default
    return max(1, rows - reserve)


#: Assumed when the terminal will not say how wide it is. Narrow on purpose,
#: for the same reason `DEFAULT_ROWS` is short: over-guessing loses content.
DEFAULT_COLS = 80


def cols_available(*, default: int = DEFAULT_COLS) -> int:
    """Cells this modal may draw across — the terminal, then `$COLUMNS`.

    Inside a tmux popup the terminal IS the popup's interior, so this is the
    width at which the modal's own lines will wrap.
    """
    cols = 0
    try:
        cols = os.get_terminal_size(sys.stdout.fileno()).columns
    except (OSError, ValueError):
        cols = 0
    if cols <= 0:
        try:
            cols = int(os.environ.get("COLUMNS") or 0)
        except ValueError:
            cols = 0
    if cols <= 0:
        cols = default
    return max(1, cols)


def truncate_to_rows(line: str, rows: int, width: int) -> str:
    """`line` cut so it occupies at most `rows` rows at `width`.

    Drops colour — it cuts the plain text. Only for the degenerate case where a
    single line is wider than the entire budget: keeping it whole would wrap
    past the bottom, and dropping it would leave the modal blank. Cutting plain
    text is the one option that shows something and stays inside the budget;
    cutting text with escapes in it risks severing an escape mid-flight. Cut by
    PLACEMENT, so a double-width character straddling the last cell goes too.
    """
    if rows <= 0 or width <= 0:
        return ""
    out: list[str] = []
    row, used = 1, 0
    plain = strip_ansi(line)
    for char, cost in zip(plain, cell_costs(plain)):
        if cost == 0:
            out.append(char)
            continue
        if used + cost > width:
            if row + 1 > rows:
                break
            row += 1
            used = cost
        else:
            used += cost
        out.append(char)
    return "".join(out)


def clip_to_rows(lines: list[str], rows: int, width: int) -> list[str]:
    """As many whole `lines` as fit in `rows` PHYSICAL rows at `width`.

    When not even the first line fits, it is TRUNCATED rather than kept whole:
    keeping a line wider than the whole budget is how the content ran past the
    bottom, which scrolls the top away — the defect this module exists for.
    """
    if rows <= 0:
        return []
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = line_rows(line, width) if width > 0 else 1
        if used + cost > rows:
            break
        kept.append(line)
        used += cost
    if not kept and lines:
        kept = [truncate_to_rows(lines[0], rows, max(1, width))]
    return kept


def window(
    lines: list[str], selected: int, rows: int, also: int | None = None
) -> tuple[list[str], bool, bool]:
    """`rows` of `lines` that still contain `selected`, and which ends were cut.

    Returns `(visible, clipped_above, clipped_below)`. The selection is kept near
    the middle so moving through a long list scrolls rather than jumping, and the
    window is clamped to the ends so the last page is full instead of trailing off.

    `also` is a SECOND row that must stay visible where it can — a stage with two
    independent selections (a driver AND a mode) acts on both, so showing only one
    makes the other a guess: two different modes drew identical screens while
    Enter did different things. When the span between them cannot fit, `selected`
    wins and the other is reported as clipped like anything else.
    """
    if rows <= 0:
        return [], bool(lines), False
    if len(lines) <= rows:
        return list(lines), False, False
    selected = max(0, min(selected, len(lines) - 1))
    if also is None:
        start = selected - (rows - 1) // 2
    else:
        also = max(0, min(also, len(lines) - 1))
        low, high = min(selected, also), max(selected, also)
        if high - low + 1 > rows:
            start = selected - (rows - 1) // 2
        else:
            start = (low + high) // 2 - (rows - 1) // 2
            start = max(high - rows + 1, min(start, low))
    start = max(0, min(start, len(lines) - rows))
    return list(lines[start:start + rows]), start > 0, start + rows < len(lines)


def _sacrifice(
    header: list[str], body: list[str], footer: list[str], rows: int, width: int
) -> list[str]:
    """What to keep when not even the pinned chrome fits, in order of need.

    Clipping `header + body + footer` front to back looked reasonable and was
    not: body rows displaced the FOOTER, and the footer is where the escape
    hint lives — and, while renaming, the field being typed into. At a 74x6
    options interior that produced identical frames for two different names
    with no rename field at all, while Enter still committed the edit.

    So: the title (what this is), then the footer (how to leave, and any field
    in it), then the rest of the header, and the body last — it is a list, and
    a list is the one part that can be scrolled to instead.
    """
    everything = header + body + footer
    if not everything or rows <= 0:
        return []
    head_count, body_count = len(header), len(body)

    def need(index: int) -> int:
        if index == 0 and head_count:
            return 0
        if index >= head_count + body_count:
            return 1
        if index < head_count:
            return 2
        return 3

    kept: list[int] = []
    used = 0
    for index in sorted(range(len(everything)), key=lambda i: (need(i), i)):
        cost = line_rows(everything[index], width)
        if used + cost > rows:
            continue
        kept.append(index)
        used += cost
    if not kept:
        return [truncate_to_rows(everything[0], rows, width)]
    return [everything[index] for index in sorted(kept)]


def fit(
    header: list[str],
    body: list[str],
    footer: list[str],
    *,
    selected: int = 0,
    rows: int | None = None,
    width: int | None = None,
    also_selected: int | None = None,
    more_above: str = "   ↑ more",
    more_below: str = "   ↓ more",
    more_both: str = "   ↕ more",
) -> list[str]:
    """Header and footer pinned, body windowed around `selected`, within `rows`.

    The header and footer are never the thing that gets dropped: they carry what
    the modal IS and how to leave it, so a clipped list is recoverable while a
    clipped title is just confusing. When the body does not fit, one row at each
    cut end becomes a `more` marker, so the list never silently appears complete.

    A screen too short even for the chrome keeps the header and as much else as
    fits — something readable beats a correct layout nobody can see.
    """
    rows = rows_available() if rows is None else rows
    width = cols_available() if width is None else width
    # The budget is PHYSICAL ROWS, not lines. Counting lines was wrong wherever
    # one of them is wider than the popup: the terminal wraps it onto a second
    # row, the modal is a row taller than the budget it was given, and the row
    # that leaves is the top one — the title. Same defect as the popup's own
    # sizing, one level down.
    def height(lines: list[str]) -> int:
        return wrapped_rows(lines, width)

    chrome = height(header) + height(footer)
    if rows <= chrome:
        return _sacrifice(header, body, footer, max(1, rows), width)
    budget = rows - chrome
    if height(body) <= budget:
        return header + body + footer
    # A row is spent on a marker ONLY when that end is actually cut: reserving
    # both unconditionally wastes a row on a blank end, which on a 10-row screen
    # is a tenth of the modal. But the reserve CHANGES which ends are cut, so it
    # cannot be settled by iterating to a fixed point — it oscillates. Walk the
    # body height DOWN instead and take the first one whose markers also fit:
    # fewer body rows can only ever add markers, never remove the overflow, so
    # the first fit is the tallest honest layout and the walk cannot overrun.
    for take in range(len(body), 0, -1):
        visible, above, below = window(body, selected, take, also_selected)
        middle = (
            ([more_above] if above else [])
            + visible
            + ([more_below] if below else [])
        )
        if height(middle) <= budget:
            return header + middle + footer
    # No body row fits beside its own markers. Spend the budget on the selection
    # and, if there is a row left, ONE marker saying the list is cut both ways —
    # drawing both would overrun, and overrunning is what loses the title.
    visible, _, _ = window(body, selected, 1, also_selected)
    middle = [more_both] + visible
    if height(middle) > budget:
        middle = clip_to_rows(visible, budget, width)
    return header + middle + footer
