"""Responsive terminal-cell grid for the sidebar."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from .layout import IdeLayout


@dataclass(frozen=True)
class SidebarGrid:
    """Lay out sidebar rows in terminal cells, not Python characters."""

    width: int
    entry_rows: int
    leading_cells: int = 2
    status_cells: int = 1
    slot_cells: tuple[int, ...] = (2, 2)

    GAP_CELLS = 1

    @staticmethod
    def cell_width(text: str) -> int:
        width = 0
        for char in text:
            if unicodedata.combining(char) or unicodedata.category(char) in {
                "Cf",
                "Mn",
                "Me",
            }:
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return width

    @classmethod
    def for_view(
        cls,
        width: int,
        height: int,
        session_count: int,
        archive_mode: bool = False,
        *,
        leading_cells: int = 2,
        status_cells: int = 1,
        slot_cells: tuple[int, ...] = (2, 2),
    ) -> "SidebarGrid":
        return cls(
            width=max(1, width),
            entry_rows=IdeLayout.entry_rows(session_count, height, archive_mode),
            leading_cells=leading_cells,
            status_cells=status_cells,
            slot_cells=slot_cells,
        )

    @classmethod
    def fit(cls, text: str, width: int) -> str:
        if width <= 0:
            return ""
        if cls.cell_width(text) <= width:
            return text
        if width == 1:
            return "…"
        budget = width - 1
        out: list[str] = []
        used = 0
        for char in text:
            cells = cls.cell_width(char)
            if used + cells > budget:
                break
            out.append(char)
            used += cells
        return "".join(out) + "…"

    @classmethod
    def pad(cls, text: str, width: int) -> str:
        fitted = cls.fit(text, width)
        return fitted + " " * max(0, width - cls.cell_width(fitted))

    @property
    def name_width(self) -> int:
        reserved = (
            self.leading_cells
            + self.GAP_CELLS
            + self.GAP_CELLS
            + self.status_cells
        )
        return max(1, self.width - reserved)

    def metadata_row(
        self,
        *,
        state: str,
        slots: list[str],
        age: str,
        age_style: str = "",
        reset: str = "",
        right_status: str = "",
    ) -> str:
        """Render the most informative metadata tracks that fit this width."""
        indent = self.pad(state, self.leading_cells) + " "
        available = max(0, self.width - self.cell_width(indent))
        show_right = bool(right_status) and available >= self.status_cells + 3
        right_width = self.status_cells if show_right else 0
        right_gap = self.GAP_CELLS if show_right else 0
        content_available = max(0, available - right_width - right_gap)

        def styled_age(limit: int) -> str:
            fitted = self.fit(age, limit)
            return f"{age_style}{fitted}{reset}" if fitted else ""

        tracks = list(zip(slots, self.slot_cells))
        # The rightmost configured slots have collapse priority. Drop whole tracks until
        # the remaining grid leaves at least two cells for age; columns never shift based
        # on whether a particular session happens to have a value.
        while tracks:
            tracks_width = sum(cells for _, cells in tracks) + len(tracks)
            if tracks_width + 2 <= content_available:
                break
            tracks.pop(0)
        prefix = "".join(f"{self.pad(icon, cells)} " for icon, cells in tracks)
        age_width = max(0, content_available - self.cell_width(prefix))
        content = f"{prefix}{styled_age(age_width)}"
        if not show_right:
            return f"{indent}{content}"
        spacer = " " * max(
            self.GAP_CELLS,
            content_available - self.cell_width(content) + right_gap,
        )
        return f"{indent}{content}{spacer}{self.pad(right_status, self.status_cells)}"
