"""Small terminal input and LAYOUT helpers shared by the modal popups."""

from __future__ import annotations

import os
import re
import sys

#: What to assume when the terminal will not say how tall it is. Low on purpose:
#: guessing large is what loses content off the top, and guessing small only
#: costs a scroll indicator on a screen that had room.
DEFAULT_ROWS = 24

#: SGR/CSI escapes, which cost no cells on screen.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def printable_width(line: str) -> int:
    """Cells `line` occupies, ignoring the escapes that colour it."""
    return len(_ANSI.sub("", line))


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
    total = 0
    for line in lines:
        cells = printable_width(line)
        total += max(1, -(-cells // width))
    return total

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


def window(lines: list[str], selected: int, rows: int) -> tuple[list[str], bool, bool]:
    """`rows` of `lines` that still contain `selected`, and which ends were cut.

    Returns `(visible, clipped_above, clipped_below)`. The selection is kept near
    the middle so moving through a long list scrolls rather than jumping, and the
    window is clamped to the ends so the last page is full instead of trailing off.
    """
    if rows <= 0:
        return [], bool(lines), False
    if len(lines) <= rows:
        return list(lines), False, False
    selected = max(0, min(selected, len(lines) - 1))
    start = selected - (rows - 1) // 2
    start = max(0, min(start, len(lines) - rows))
    return list(lines[start:start + rows]), start > 0, start + rows < len(lines)


def fit(
    header: list[str],
    body: list[str],
    footer: list[str],
    *,
    selected: int = 0,
    rows: int | None = None,
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
    chrome = len(header) + len(footer)
    if rows <= chrome:
        return (header + body + footer)[:max(1, rows)]
    budget = rows - chrome
    if len(body) <= budget:
        return header + body + footer
    # A row is spent on a marker ONLY when that end is actually cut: reserving
    # both unconditionally wastes a row on a blank end, which on a 10-row screen
    # is a tenth of the modal. But the reserve CHANGES which ends are cut, so it
    # cannot be settled by iterating to a fixed point — it oscillates. Walk the
    # body height DOWN instead and take the first one whose markers also fit:
    # fewer body rows can only ever add markers, never remove the overflow, so
    # the first fit is the tallest honest layout and the walk cannot overrun.
    for height in range(budget, 0, -1):
        visible, above, below = window(body, selected, height)
        want = (1 if above else 0) + (1 if below else 0)
        if height + want <= budget:
            middle = (
                ([more_above] if above else [])
                + visible
                + ([more_below] if below else [])
            )
            return header + middle + footer
    # No body row fits beside its own markers. Spend the budget on the selection
    # and, if there is a row left, ONE marker saying the list is cut both ways —
    # drawing both would overrun, and overrunning is what loses the title.
    visible, _, _ = window(body, selected, max(1, budget - 1))
    middle = ([more_both] if budget > len(visible) else []) + visible
    return header + middle + footer
