#!/usr/bin/env python3
"""THE single owner of ide frame + sidebar geometry.

Every layout number and every derivation lives here. Nothing else defines a column
width, a threshold, or a row count — they import these.

Why this module exists: the ide produced three bugs of the SAME shape, each one two
places computing layout independently and drifting apart.

    1. `render_lines` dropped rows on a short pane while `_click_index` still divided by
       3 → clicks selected the wrong session.
    2. The responsive ladder computed a plan width while `rebuild` wrote the raw
       `PLAN_W` → every rebuild undid the responsive layout.
    3. A hook was wired to the event its own `resize-pane` raises → infinite re-entry,
       ~1000 processes, a wedged tmux server.

Tests now catch each of those individually. This module removes the *category*: with one
owner there is no second copy to drift from. Consumers are `ide_skill.py` (constants +
`_apply_columns` + hook thresholds), `ide_relayout.py` (the resize/adopt hook), and
`ide_sidebar.py` (row density + hit-testing).

Stdlib only and import-light on purpose — `ide_relayout.py` and `ide_sidebar.py` run as
bare `python3` scripts inside tmux with no runtime boot, so this must never pull in the
skill runtime.
"""
from __future__ import annotations

__all__ = ["IdeLayout"]


class IdeLayout:
    """Frame columns and sidebar rows: the numbers, and every derivation from them.

    Class attributes are the ONLY definitions of these values in the codebase; the
    `no_geometry_outside_layout` test enforces that. Everything else is derived, so a
    change here propagates everywhere at once instead of leaving a stale copy behind.
    """

    # ---- frame columns ----
    SIDEBAR_W = 20      # 2-cell icon + name (~15ch) + status dot
    PLAN_W = 86         # 80 text + 4 line-number gutter + 2 padding
    AGENT_MIN = 60      # the agent column never goes below this
    PLAN_MIN = 40       # narrower than this and the plan is not worth showing
    # The agent is served THIS width before the plan gets any slack. The plan is
    # reference material; the agent is where the work happens. Without it the plan
    # absorbed every spare column and the agent sat pinned at AGENT_MIN on any
    # sub-full-width screen ("the plan pane is too big on smaller screens").
    AGENT_PREF = 90

    # ---- sidebar rows ----
    HEADER_ROWS = 2                     # repo/`+` header, then a blank
    ENTRY_ROWS = 3                      # FULL entry: name · sub-line · blank spacer
    FOOTER_ROWS = 3                     # filter + blank + `show archive`
    FOOTER_ROWS_ARCHIVE = 2             # filter + esc hint
    SPACER_OFFSET = 2                   # index of the blank row within a full entry

    @classmethod
    def mobile_threshold(cls) -> int:
        """Below this the three columns cannot fit, so the FOCUSED column is zoomed.

        DERIVED, never hand-set: the relayout ladder and the `pane-focus-in` zoom hook
        both key off it, and if they ever disagreed the ladder would unzoom what the hook
        just zoomed and the layout would flap.
        """
        return cls.SIDEBAR_W + cls.AGENT_MIN + cls.PLAN_MIN

    @classmethod
    def is_mobile(cls, window_width: int) -> bool:
        return window_width < cls.mobile_threshold()

    @classmethod
    def plan_width(cls, window_width: int, side: int | None = None) -> int:
        """The plan column for a window — the AGENT has priority for the slack.

            plan = clamp(width - side - AGENT_PREF, PLAN_MIN, PLAN_W)

        then the plan yields further if that would push the agent under `AGENT_MIN`.
        Reverses the original ladder, which pinned the agent at its minimum and handed
        every remaining column to the plan.
        """
        side = cls.SIDEBAR_W if side is None else side
        plan = max(cls.PLAN_MIN, min(cls.PLAN_W, window_width - side - cls.AGENT_PREF))
        if window_width - side - plan < cls.AGENT_MIN:
            plan = max(cls.PLAN_MIN, window_width - side - cls.AGENT_MIN)
        return plan

    @classmethod
    def columns(cls, window_width: int) -> tuple[int, int, int] | None:
        """`(sidebar, agent, plan)` for a window, or None when it is mobile (zoom instead).

        The one call a consumer should need: it answers "how wide is everything?" without
        the caller re-deriving the mobile check or the agent remainder.
        """
        if cls.is_mobile(window_width):
            return None
        plan = cls.plan_width(window_width)
        return cls.SIDEBAR_W, window_width - cls.SIDEBAR_W - plan, plan

    @classmethod
    def entry_rows(cls, n_sessions: int, height: int, archive_mode: bool = False) -> int:
        """Rows per session so the list FITS the pane: 3 → drop the spacer (2) → name only (1).

        Lose decoration before losing sessions off the bottom. Whatever this returns MUST
        be used by both the renderer and `session_at_row`, or clicks land on the wrong
        session — that is bug (1) above.
        """
        footer = cls.FOOTER_ROWS_ARCHIVE if archive_mode else cls.FOOTER_ROWS
        budget = height - cls.HEADER_ROWS - footer
        for rows in (cls.ENTRY_ROWS, 2, 1):
            if n_sessions * rows <= budget:
                return rows
        return 1

    @classmethod
    def session_capacity(
        cls, height: int, entry_rows: int, archive_mode: bool = False
    ) -> int:
        """Maximum visible session rows for a pane at the chosen density."""
        footer = cls.FOOTER_ROWS_ARCHIVE if archive_mode else cls.FOOTER_ROWS
        budget = height - cls.HEADER_ROWS - footer
        return max(0, budget // max(1, entry_rows))

    @classmethod
    def session_row(cls, index: int, entry_rows: int) -> int:
        """The 0-based screen row where session `index` draws its NAME line."""
        return cls.HEADER_ROWS + index * entry_rows

    @classmethod
    def session_at_row(cls, row0: int, entry_rows: int) -> int | None:
        """Inverse of `session_row`: which session a 0-based screen row belongs to.

        None for the header, above the list, or the blank spacer inside a full entry.
        Keeping both directions here is what makes render and hit-testing provably
        agree — they are literally the same arithmetic.
        """
        rel = row0 - cls.HEADER_ROWS
        if rel < 0:
            return None
        if entry_rows >= cls.ENTRY_ROWS and rel % entry_rows == cls.SPACER_OFFSET:
            return None
        return rel // entry_rows
