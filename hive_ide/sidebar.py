#!/usr/bin/env python3
"""Live, interactive sidebar for the `ide` tmux frame — one instance per window.

Run as a bare process in each window's left pane (plan D4/D20):

    python3 -m hive_ide.sidebar --state-home <state> --workspace-key <repo>

Stdlib only — it must NOT boot the foreground CLI runtime (a dozen panes doing so
would waste memory). It re-reads the per-workspace registry via `StateIO`,
repaints a Zed-style list, and — when its pane is focused — lets you move a
selection with **↑/↓**, **Enter to switch** to that session's window, and **→ to
go back to the chat pane** leaving the selection untouched:

    <agent-icon>  <name>        ← name ORANGE for the current window;
                  <rel-time>       filled bar for the browse selection

Rendering: every line is hard-truncated with an ellipsis, cleared to end-of-line,
and auto-wrap is off — so a narrow pane never reflows and a shorter redraw never
leaves stale characters. Input uses cbreak mode (signals still work) + `select`,
so it also refreshes on a timer while idle.
"""
from __future__ import annotations

import os
import argparse
import re
import select
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import PROTOCOL_VERSION, __version__

try:                       # POSIX only (tmux is Unix); keep importable elsewhere
    import termios
    import tty
except ImportError:
    termios = None
    tty = None

from .config import _sidebar_config
from .layout import IdeLayout
from .python_cmd import PythonCommand
from .sidebar_grid import SidebarGrid
from .sidebar_plugins import SidebarProviderRegistry
from .state_compat import StateIO

_DEFAULT_PROVIDERS = SidebarProviderRegistry()
_DEFAULT_SIDEBAR = _sidebar_config({}, _DEFAULT_PROVIDERS)


@dataclass
class SidebarCursorState:
    cursor: int = -1
    session_id: str = ""
    focused: bool = False

    def reconcile(
        self,
        session_ids: list[str],
        *,
        free: bool,
        archive_mode: bool,
        current_session_id: str,
    ) -> "SidebarCursorState":
        if not session_ids:
            return SidebarCursorState(0, "", self.focused)
        cursor = self.cursor
        if self.session_id in session_ids and (self.focused or archive_mode or free):
            cursor = session_ids.index(self.session_id)
        elif not self.focused or (not free and (cursor < 0 or cursor >= len(session_ids))):
            cursor = (
                session_ids.index(current_session_id)
                if current_session_id in session_ids
                else 0
            )
        else:
            cursor = max(0, min(cursor, len(session_ids) - 1))
        return SidebarCursorState(cursor, session_ids[cursor], self.focused)

    def activate_chat(self, session_ids: list[str]) -> "SidebarCursorState":
        session_id = (
            session_ids[self.cursor]
            if 0 <= self.cursor < len(session_ids)
            else ""
        )
        return SidebarCursorState(self.cursor, session_id, False)


@dataclass
class SidebarCommandRunner:
    state_home: Path
    workspace_key: str
    python: str = sys.executable

    def cli(self, args: list[str]) -> bool:
        """Run one public CLI mutation in response to a deliberate user action."""
        try:
            r = subprocess.run(
                PythonCommand.cli_argv(
                    [
                        "--state-home",
                        str(self.state_home),
                        "--workspace-key",
                        self.workspace_key,
                        *args,
                    ],
                    python=self.python,
                ),
                cwd=self.workspace_key,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return r.returncode == 0

    def window_id(self, session_id: str) -> str | None:
        result = subprocess.run(
            [
                "tmux",
                "list-windows",
                "-a",
                "-F",
                "#{window_id}\t#{@hive_ide_session_id}",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        return next(
            (
                line.split("\t", 1)[0]
                for line in result.stdout.splitlines()
                if line.endswith(f"\t{session_id}")
            ),
            None,
        )

    def active_session_id(self, fallback: str) -> str:
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{@hive_ide_session_id}"],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            return fallback
        active = result.stdout.strip() if result.returncode == 0 else ""
        return active or fallback

    def switch(self, session_id: str) -> bool:
        """Select a session window and focus its agent/chat pane."""
        target = self.window_id(session_id)
        if target is None:
            args = ["repair", f"--session-id={session_id}"]
            if socket := os.environ.get("HIVE_IDE_TMUX_SOCKET"):
                args.append(f"--tmux-socket={socket}")
            if not self.cli(args):
                return False
            target = self.window_id(session_id)
        if target is None:
            return False
        window = subprocess.run(
            ["tmux", "select-window", "-t", target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        pane = subprocess.run(
            ["tmux", "select-pane", "-t", f"{target}.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return window.returncode == 0 and pane.returncode == 0

    def focus_agent(self, session_id: str) -> None:
        target = self.window_id(session_id)
        if target is None:
            return
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{window_zoomed_flag}"],
            capture_output=True,
            text=True,
        )
        if r.stdout.strip() == "1":
            subprocess.run(
                ["tmux", "resize-pane", "-Z", "-t", target],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        subprocess.run(
            ["tmux", "select-pane", "-t", f"{target}.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def resume(self, session_id: str) -> bool:
        """Resume an archived session, repair its window, and focus chat."""
        if not session_id:
            return False
        args = [
            "resume",
            f"--session-id={session_id}",
            *(
                [f"--tmux-socket={socket}"]
                if (socket := os.environ.get("HIVE_IDE_TMUX_SOCKET"))
                else []
            ),
        ]
        if not self.cli(args):
            return False
        self.focus_agent(session_id)
        return True


class IdeSidebar:
    """Interactive render loop for one window's sidebar pane."""

    TICK_SECONDS = 1.5
    NO_WRAP, HOME, EL, CLEAR_BELOW, RESET = "\x1b[?7l", "\x1b[H", "\x1b[K", "\x1b[J", "\x1b[0m"
    # Selection uses a REAL background colour, not reverse-video (SGR 7): erase-to-EOL
    # fills with the background colour, and reverse is only an attribute — so a
    # reverse-video bar stops at the text instead of reaching the pane edge.
    # Browse selection keeps the legacy IDE's high-contrast palette: green for the
    # current window, teal for any other selected row.
    SEL_CUR = "\x1b[48;5;46m\x1b[38;5;16m"
    SEL_ALT = "\x1b[48;5;51m\x1b[38;5;16m"
    # The window you are actually IN, when it is NOT the browse selection: a subtle dark
    # fill so "you are here" is visible even while you type in the agent pane — deliberately
    # far quieter than the cyan selection bar. Tune the 238 (→ darker/lighter) to taste.
    CUR_BG = "\x1b[48;5;238m"
    # The current window's NAME sits on CUR_BG — in ORANGE (the 214 the inline name prompt
    # used before the modal superseded it). History: it was bright cyan (competing with the
    # selection teal), then a quiet light-grey — but grey left the fill doing ALL the work,
    # so "you are here" read as a background tint and nothing more. Orange is unclaimed by
    # any status here, so the current session's TEXT now says it too.
    # Selection still wins on focus: a selected row paints SEL_CUR/SEL_ALT over this.
    CUR_FG = "\x1b[1;38;5;214m"
    # Reset intensity + fg to default but KEEP the background — used inside a CUR_BG row so
    # an inline colour change doesn't punch a hole in the fill (a full RESET would clear the
    # bg, and the erase-to-EOL only re-fills to the RIGHT of the cursor, not behind it).
    KEEP_BG = "\x1b[22;39m"
    FOCUS_ON, FOCUS_OFF = "\x1b[?1004h", "\x1b[?1004l"   # ask tmux to report focus in/out
    MOUSE_ON, MOUSE_OFF = "\x1b[?1000h\x1b[?1006h", "\x1b[?1006l\x1b[?1000l"   # SGR mouse
    MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
    ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    # A read can end mid-report, so the unterminated head of one (ESC through the
    # coordinates, no M/m yet) is carried to the next read instead of discarded.
    MOUSE_PARTIAL_RE = re.compile(rb"\x1b(\[(<\d*(;\d*(;\d*)?)?)?)?\Z")
    MOUSE_PARTIAL_MAX = 32        # longer than any real report → malformed, drop it
    # `ESC [ <` can only ever begin an SGR mouse report — no key produces it. So a tail
    # starting with it is never flushed as keystrokes, unlike a bare `ESC`/`ESC [`, which
    # might be a real Escape and must still fire.
    MOUSE_HEAD = b"\x1b[<"
    NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_ ")   # valid filter input
    # ESC-tail wait for a carried arrow/escape prefix (`\x1b`, `\x1b[`, `\x1bO`): if the
    # `[A` tail of an arrow arrives a beat after the ESC, too short a wait flushes the ESC
    # as a lone key and the arrow is lost. Matches the new-modal's ESC_PEEK_SECONDS.
    # NOTE: this is slow-link robustness only — it was NOT the cause of "arrows dead over
    # SSH". That was tmux: a wheel scroll under `mouse on` dropped the pane into copy-mode,
    # whose own Up/Down bindings ate the keys before the sidebar ever saw them. Claiming
    # the wheel (see _wheel_delta) is what actually keeps the pane out of copy-mode.
    PARTIAL_SECONDS = 0.4         # was 0.05 — too short for network latency
    # Row layout, must match render_lines: header + blank, then ENTRY_ROWS per entry
    # (name, relative-time, blank). ENTRY_ROWS is the FULL layout and a MAXIMUM, not a
    # constant: `_entry_rows` drops the spacer (→2) and then the sub-line (→1) when the
    # list would outgrow the pane. Whatever it returns MUST be used by both `render_lines`
    # and `_click_index` — if they disagree, clicks land on the wrong session.
    # Aliases of the single owner — NOT independent definitions. See ide_layout.py.
    HEADER_ROWS, ENTRY_ROWS = IdeLayout.HEADER_ROWS, IdeLayout.ENTRY_ROWS
    FOOTER_ROWS, FOOTER_ROWS_ARCHIVE = IdeLayout.FOOTER_ROWS, IdeLayout.FOOTER_ROWS_ARCHIVE
    WHEEL_UP, WHEEL_DOWN = 64, 65             # SGR mouse buttons for the scroll wheel
    # Sentinel for "the click landed on the header `+`". A negative int can never collide
    # with a real session index, so `_click_index` keeps one return type.
    PLUS_HIT = -1
    ARCHIVE_HIT = -2
    OPTIONS_HIT_BASE = -10000
    HEADER = "\x1b[1;38;5;250m"   # bold light grey
    ACTIVE = "\x1b[1;38;5;51m"    # bold bright cyan — the current window
    INACTIVE = "\x1b[38;5;252m"
    DIM = "\x1b[38;5;244m"        # relative time
    # Status dots (written by the agent hooks). Idle/unknown shows nothing, so a
    # dot always MEANS something — Zed-style.
    # The two dots share one column, so the comparison that matters is waiting-vs-working,
    # not either against the text: cyan/yellow reads as "needs you / in progress" at a
    # glance. Working was amber 214 until the current session's NAME took that orange
    # (`CUR_FG`) — a status dot and an identity colour must not be the same value.
    STATUS_DOT = {"waiting": ("●", "\x1b[1;38;5;51m"),    # bright cyan — needs you
                  "working": ("●", "\x1b[38;5;220m"),
                  "error": ("!", "\x1b[1;38;5;203m")}
    STALE_WORKING_SECONDS = 900   # a crashed agent must not sit on "working" forever
    PLUS = "+"                    # header button — click to create an ide session
    # Green, deliberately NOT the cyan of ACTIVE/SEL_CUR: cyan means "the current
    # window" everywhere else in this list, so a cyan `+` read as a status rather than
    # a button. Green is unused here and says "create".
    PLUS_COLOR = "\x1b[1;38;5;46m"          # bright green — an affordance, not a status
    ARCHIVE_HEADER = "\x1b[1;38;5;245m"     # grey banner — you are browsing the archive

    @staticmethod
    def _rel_time(iso: str | None) -> str:
        if not iso:
            return ""
        try:
            t = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return ""
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        secs = max(0, int((datetime.now(timezone.utc) - t).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m"
        hrs = mins // 60
        if hrs < 24:
            return f"{hrs}h"
        days = hrs // 24
        if days < 7:
            return f"{days}d"
        return f"{days // 7}w"

    @staticmethod
    def _age_seconds(iso: str | None) -> float:
        if not iso:
            return float("inf")
        try:
            t = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            return float("inf")
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - t).total_seconds())

    @staticmethod
    def _provider_mark(
        state_home: Path,
        session: dict,
        provider_id: str | None,
        sidebar: dict,
        providers: SidebarProviderRegistry,
    ) -> str:
        if not provider_id:
            return ""
        try:
            value = providers.get(provider_id).value(state_home, session)
        except (OSError, RuntimeError, ValueError):
            return ""
        icons = ((sidebar.get("icons") or {}).get("providers") or {}).get(
            provider_id, {}
        )
        if isinstance(value, str) and value.startswith("count:"):
            count = value.partition(":")[2]
            return count[:2] if count.isdigit() else ""
        return icons.get(value, icons.get("default", "")) if value else ""

    @staticmethod
    def _plan_mark(s: dict) -> str:
        return IdeSidebar._provider_mark(
            Path("."), s, "plan", _DEFAULT_SIDEBAR, _DEFAULT_PROVIDERS
        )

    @staticmethod
    def _checkout_mark(s: dict) -> str:
        return IdeSidebar._provider_mark(
            Path("."), s, "checkout", _DEFAULT_SIDEBAR, _DEFAULT_PROVIDERS
        )

    @staticmethod
    def _status_dot(
        skill_dir: Path,
        s: dict,
        current: bool = False,
        sidebar: dict | None = None,
    ) -> tuple[str, str]:
        """(glyph, color) for this session's agent status — ('','') when idle/unknown.

        `current` = this row IS the window you're in. Current rows still render the
        same status glyph as inactive rows; the highlight only changes the background.
        """
        settings = sidebar or _DEFAULT_SIDEBAR
        status_icons = (settings.get("icons") or {}).get("status") or {}
        if StateIO.read_error(skill_dir, s):
            return status_icons.get("error", "!"), IdeSidebar.STATUS_DOT["error"][1]
        st = StateIO.read_session_status(skill_dir, s)
        if not st:
            return "", ""
        state = st.get("state")
        if state == "working" and IdeSidebar._age_seconds(st.get("ts")) > IdeSidebar.STALE_WORKING_SECONDS:
            return "", ""   # self-heal: stale "working" (crashed agent) → neutral
        _, color = IdeSidebar.STATUS_DOT.get(state, ("", ""))
        return status_icons.get(state, ""), color

    @staticmethod
    def _strip_ansi(text: str) -> str:
        return IdeSidebar.ANSI_RE.sub("", text)

    @staticmethod
    def _activity_mark(skill_dir: Path, s: dict) -> str:
        return IdeSidebar._provider_mark(
            skill_dir, s, "activity", _DEFAULT_SIDEBAR, _DEFAULT_PROVIDERS
        )

    @staticmethod
    def _tmux_alerts() -> dict[str, str]:
        """Live tmux bell/activity markers keyed by IDE session id."""
        try:
            result = subprocess.run(
                [
                    "tmux",
                    "list-windows",
                    "-a",
                    "-F",
                    "#{@hive_ide_session_id}\t#{window_bell_flag}\t#{window_activity_flag}\t#{window_flags}",
                ],
                capture_output=True,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        if result.returncode != 0:
            return {}
        alerts: dict[str, str] = {}
        for line in result.stdout.splitlines():
            session_id, bell, activity, flags = (line.split("\t") + ["", "", "", ""])[:4]
            if not session_id:
                continue
            if bell == "1" or "!" in flags:
                alerts[session_id] = "🔔"
            elif activity == "1" or "#" in flags:
                alerts[session_id] = "•"
        return alerts

    @staticmethod
    def _width() -> int:
        try:
            return max(8, os.get_terminal_size(sys.stdout.fileno()).columns)
        except OSError:
            return 24

    @staticmethod
    def _height() -> int:
        try:
            return max(4, os.get_terminal_size(sys.stdout.fileno()).lines)
        except OSError:
            return 24

    @staticmethod
    def _entry_rows(n_sessions: int, height: int, archive_mode: bool = False) -> int:
        """How many rows each session gets so the list FITS the pane.

        Full layout is 3 (name · sub-line · blank spacer). When the sessions would outgrow
        the pane the spacer goes first (→2), then the sub-line (→1) — losing decoration
        before losing sessions. Returns the densest-needed layout, never below 1.

        This is the fix for two bugs at once: the wasted blank rows on a short pane, AND
        clicks selecting the WRONG session — the click math divides by exactly this number,
        so a render that silently used a different row count mis-mapped every click.
        """
        return IdeLayout.entry_rows(n_sessions, height, archive_mode)

    @staticmethod
    def _wheel_delta(m: "re.Match[bytes]") -> int:
        """Cursor movement for one SGR mouse report: +1 wheel-up, -1 wheel-down, else 0.

        Direction is NATURAL/content-style, matching how the pane actually feels to use:
        the wheel moves the LIST under a fixed cursor, so pushing the wheel up walks the
        selection DOWN the list. (The opposite — wheel-up moving the selection up — read
        backwards in practice.)

        Claiming the wheel is not just a feature — it is what keeps tmux from treating the
        scroll as a scrollback gesture and dropping the pane into copy-mode, whose Up/Down
        bindings then swallow the arrow keys (the "arrows dead over SSH" report).
        """
        button, kind = int(m.group(1)), m.group(4)
        if kind != b"M":
            return 0
        if button == IdeSidebar.WHEEL_UP:
            return 1
        if button == IdeSidebar.WHEEL_DOWN:
            return -1
        return 0

    @staticmethod
    def _fit(text: str, width: int) -> str:
        return SidebarGrid.fit(text, width)

    @staticmethod
    def _row(content: str, highlight: bool = False, base: str = "") -> str:
        """One rendered line: erase the row FIRST, then draw over it.

        `\\x1b[K` erases from the cursor to the end of the line using the ACTIVE
        background colour (BCE), so `{base}{EL}` paints the whole row in the selection
        colour and the text then lands on top of it. The bar reaches the pane edge with
        no width arithmetic, exact regardless of emoji cell width — `base` must set a
        real bg (SEL_*); reverse-video would not fill.

        The erase used to TRAIL the content, which silently ate any glyph in the LAST
        column: with auto-wrap off (NO_WRAP) the cursor does not advance past the final
        cell, so the erase began ON it rather than after it. Everything further left
        rendered, which is what made it look like a data problem — the header `+` and a
        right-flush presence icon or status dot just never appeared.
        """
        return f"{base}{IdeSidebar.EL}{content}{IdeSidebar.RESET}"

    @staticmethod
    def _initial_focus() -> bool:
        """tmux only reports focus on CHANGE, so seed the state for this pane."""
        return IdeSidebar._pane_active()

    @staticmethod
    def _pane_active() -> bool:
        """Authoritative tmux focus state for this sidebar pane.

        Terminal focus events are lossy around window switches: the newly visible
        window's sidebar can keep an old local browse cursor even though tmux has
        already focused the chat pane. Polling the pane state before each draw keeps
        the visual selection tied to the real active pane, not stale process memory.
        """
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            return False
        try:
            r = subprocess.run(["tmux", "display-message", "-p", "-t", pane, "#{pane_active}"],
                               capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            return False
        return r.stdout.strip() == "1"

    @staticmethod
    def _key(data: bytes) -> str | None:
        """Control keys only — anything else is filter text (so no j/k binding)."""
        if data == b"\x1b[I":
            return "focus_in"
        if data == b"\x1b[O":
            return "focus_out"
        if data in (b"\x1b[A", b"\x1bOA"):
            return "up"
        if data in (b"\x1b[B", b"\x1bOB"):
            return "down"
        if data in (b"\x1b[C", b"\x1bOC"):
            return "right"
        if data in (b"\r", b"\n"):
            return "enter"
        if data in (b"\x7f", b"\b"):
            return "backspace"
        if data == b"\x15":          # Ctrl-U — kill the line, as in any shell
            return "clear"
        if data == b"\x1b":
            return "escape"
        return None

    @staticmethod
    def _cli(skill_dir: Path, args: list[str]) -> bool:
        return SidebarCommandRunner(skill_dir, IdeSidebar._repo_hint).cli(args)

    @staticmethod
    def _window_id(session_id: str) -> str | None:
        return SidebarCommandRunner(Path("."), IdeSidebar._repo_hint).window_id(session_id)

    @staticmethod
    def _switch(session_id: str, skill_dir: Path | None = None) -> bool:
        """Switch to an IDE session's window and focus its agent/chat pane.
        Running inside `tmux -L ide`, $TMUX routes these to the ide server.

        The sidebar lists RECORDS, but this can only select a WINDOW — and a record
        whose window doesn't exist used to make the click a silent no-op, because the
        exit code was discarded. So a miss is now escalated to `repair`, which builds
        the window and selects it. Costs nothing in the common case (the window exists,
        `select-window` succeeds, no runtime boot).
        """
        return SidebarCommandRunner(skill_dir or Path("."), IdeSidebar._repo_hint).switch(
            session_id
        )

    @staticmethod
    def _active_session_id(fallback: str) -> str:
        return SidebarCommandRunner(Path("."), IdeSidebar._repo_hint).active_session_id(
            fallback
        )

    @staticmethod
    def _focus_agent(session_id: str) -> None:
        """Right arrow — hand focus back to THIS window's agent/chat pane.

        Deliberately NOT a switch: browsing the list with the arrows moves a cursor, and
        Right leaves that cursor exactly where it is without selecting it. So Right is the
        "never mind, back to work" exit — the counterpart to Enter, which commits the
        browse. The unfocus at the loop top then restores the canonical view, so the
        abandoned cursor never persists.

        Targets the agent pane by INDEX (`.1`) rather than geometrically (`select-pane -R`,
        what Alt+Right does): from the sidebar both land on the chat pane, but the index is
        stable no matter how the columns are laid out or which pane the user last touched.

        Unzooms first, mirroring the `c` binding: on a narrow window `l` zooms the sidebar
        full-screen, and selecting another pane while zoomed leaves the zoom on a pane you
        can no longer see.
        """
        SidebarCommandRunner(Path("."), IdeSidebar._repo_hint).focus_agent(session_id)

    @staticmethod
    def _reconcile_cursor(
        session_ids: list[str],
        cursor: int,
        *,
        focused: bool,
        free: bool,
        archive_mode: bool,
        current_session_id: str,
        cursor_session_id: str,
    ) -> tuple[int, str]:
        state = SidebarCursorState(cursor, cursor_session_id, focused).reconcile(
            session_ids,
            free=free,
            archive_mode=archive_mode,
            current_session_id=current_session_id,
        )
        return state.cursor, state.session_id

    @staticmethod
    def _after_activation(session_ids: list[str], cursor: int) -> tuple[bool, str]:
        """A successful activation moves focus to chat; sidebar browse selection ends."""
        state = SidebarCursorState(cursor, "", True).activate_chat(session_ids)
        return state.focused, state.session_id

    @staticmethod
    def _click_index(
        m: re.Match[bytes],
        entry_rows: int = ENTRY_ROWS,
        *,
        archive_row: int | None = None,
    ) -> int | None:
        """What one SGR mouse report hit: a session index, PLUS_HIT, or None.

        `entry_rows` MUST be the value the current draw used (`_entry_rows`), or clicks
        map to the wrong session on a pane too short for the full 3-row layout.

        None unless it's a left-click or right-click. Row 0 is the header, which carries the `+`
        button — a click anywhere on that row opens the new-session prompt (the whole
        row is the target, not just the glyph cell: a 1-cell hitbox is unusable).
        A right-click on a session row returns an encoded options hit for that index.
        """
        button, kind = int(m.group(1)), m.group(4)
        if button not in {0, 2} or kind != b"M":   # press only (ignore wheel/drag/release)
            return None
        row = int(m.group(3)) - 1                              # 1-based row → 0-based
        if button == 2:
            index = IdeLayout.session_at_row(row, entry_rows)
            return (
                IdeSidebar.OPTIONS_HIT_BASE - index
                if index is not None
                else None
            )
        if row == 0:
            return IdeSidebar.PLUS_HIT
        if archive_row is not None and row == archive_row:
            return IdeSidebar.ARCHIVE_HIT
        # Delegated to the geometry owner, which also computes the row the renderer
        # DRAWS each session on. Same arithmetic, both directions, one place — so the
        # wrong-session bug cannot come back by the two drifting apart.
        return IdeLayout.session_at_row(row, entry_rows)

    @staticmethod
    def _drain(
        buf: bytes,
        entry_rows: int = ENTRY_ROWS,
        *,
        archive_row: int | None = None,
    ) -> tuple[int | None, int, bytes, bytes]:
        """Split an input buffer into (last left-click, net wheel delta, key bytes, tail).

        One read can coalesce several reports (a wheel or drag landing just before a
        click), so EVERY complete one is consumed — a lone `search` would stop at the
        first and drop the click behind it. Mouse bytes are stripped out rather than
        swallowing the buffer, so keys sharing the read still reach `_key`.

        Wheel reports are ACCUMULATED (a fast flick arrives as several in one read) rather
        than discarded, so the sidebar scrolls instead of tmux hijacking it into copy-mode.
        """
        click, wheel, keys, end = None, 0, bytearray(), 0
        for m in IdeSidebar.MOUSE_RE.finditer(buf):
            keys += buf[end:m.start()]
            end = m.end()
            idx = IdeSidebar._click_index(m, entry_rows, archive_row=archive_row)
            if idx is not None:
                click = idx
            else:
                wheel += IdeSidebar._wheel_delta(m)
        rest = buf[end:]
        partial = IdeSidebar.MOUSE_PARTIAL_RE.search(rest)
        if not partial or len(rest) - partial.start() > IdeSidebar.MOUSE_PARTIAL_MAX:
            return click, wheel, bytes(keys) + rest, b""
        keys += rest[: partial.start()]
        return click, wheel, bytes(keys), rest[partial.start():]

    @staticmethod
    def _printable(data: bytes) -> str:
        """Filter/name text only. Accepts just the characters a session name can hold,
        which also keeps stray report bytes (`;`, `M`) out of the query if one ever
        escapes the mouse parser."""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        return text if text and all(ch in IdeSidebar.NAME_CHARS for ch in text.lower()) else ""

    @staticmethod
    def _launch_new_modal(skill_dir: Path, repo: str) -> None:
        """Open the guided new-session modal (name → type → create + switch) in a centered
        popup — the `+` action. A create always walks the modal now, instead of the old
        inline name+Tab prompt. Non-blocking: the popup runs in the tmux server, so this
        returns at once and the sidebar keeps repainting underneath until the modal creates
        the session and switches focus to it."""
        command = PythonCommand.module_command(
            "newmodal",
            [
                "--state-home",
                str(skill_dir),
                "--workspace-key",
                repo,
                "--tmux-socket",
                os.environ.get("HIVE_IDE_TMUX_SOCKET", "hive-ide"),
            ],
            python=sys.executable,
        )
        subprocess.run(
            ["tmux", "display-popup", "-E", "-w", "52%", "-h", "42%",
             command],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    @staticmethod
    def _options_index(value: int) -> int | None:
        if value <= IdeSidebar.OPTIONS_HIT_BASE:
            return IdeSidebar.OPTIONS_HIT_BASE - value
        return None

    @staticmethod
    def _launch_options_modal(skill_dir: Path, repo: str, session_id: str) -> None:
        command = PythonCommand.module_command(
            "optionsmodal",
            [
                "--state-home",
                str(skill_dir),
                "--workspace-key",
                repo,
                "--session-id",
                session_id,
                "--tmux-socket",
                os.environ.get("HIVE_IDE_TMUX_SOCKET", "hive-ide"),
            ],
            python=sys.executable,
        )
        subprocess.run(
            [
                "tmux",
                "display-popup",
                "-E",
                "-w",
                "62%",
                "-h",
                "58%",
                command,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @staticmethod
    def _resume(skill_dir: Path, session_id: str) -> bool:
        """Resume an archived session (the archive view's Enter). Shells out to `ide
        resume`, which restores it to active, re-homes to the repo root, and rebuilds its
        window; then focus its agent pane so you can type. Returns True on success."""
        return SidebarCommandRunner(skill_dir, IdeSidebar._repo_hint).resume(session_id)

    _repo_hint: str = ""

    @staticmethod
    def _repo_of(skill_dir: Path) -> str:
        """The repo this sidebar renders — captured from argv at startup (the sidebar is
        launched per repo), so no git call on the input path."""
        return IdeSidebar._repo_hint

    @staticmethod
    def _filter(sessions: list[dict], query: str) -> list[dict]:
        if not query:
            return sessions
        q = query.lower()
        return [s for s in sessions if q in (s.get("name") or "").lower()]

    @staticmethod
    def render_lines(skill_dir: Path, sessions: list[dict], repo: str, this_session_id: str,
                     cursor: int, width: int, query: str = "", focused: bool = True,
                     on_plus: bool = False,
                     on_filter: bool = False, on_archive: bool = False,
                     archive_mode: bool = False,
                     entry_rows: int = ENTRY_ROWS,
                     sidebar: dict | None = None,
                     providers: SidebarProviderRegistry | None = None,
                     tmux_alerts: dict[str, str] | None = None) -> list[str]:
        row = IdeSidebar._row
        # Where the terminal cursor should PARK after this draw: the filter/name input row,
        # so the block sits where you'd type instead of at the end of the last line drawn.
        # `main()` reads this and emits a cursor-position escape. Reset every call.
        IdeSidebar._cursor_rc = None
        settings = sidebar or _DEFAULT_SIDEBAR
        provider_registry = providers or _DEFAULT_PROVIDERS
        icons = settings.get("icons") or {}
        driver_icons = icons.get("drivers") or {}
        status_icons = icons.get("status") or {}
        provider_icons = icons.get("providers") or {}
        controls = icons.get("controls") or {}
        state_provider = settings.get("state")
        slot_providers = settings.get("slots") or []

        def track_width(values: dict, fallback: int = 1) -> int:
            return max(
                [fallback, *(SidebarGrid.cell_width(value) for value in values.values())]
            )

        state_width = track_width(provider_icons.get(state_provider, {}), 1)
        driver_width = track_width(driver_icons, 1)
        leading_width = max(state_width, driver_width)
        slot_widths = tuple(
            track_width(provider_icons.get(provider_id, {}), 1)
            for provider_id in slot_providers
        )
        status_width = max(track_width(status_icons, 1), 2)
        grid = SidebarGrid(
            width=max(1, width),
            entry_rows=entry_rows,
            leading_cells=leading_width,
            status_cells=status_width,
            slot_cells=slot_widths,
        )
        workspace_label = Path(repo).name or repo
        if archive_mode:
            # Archive VIEW header: no `+`, an unmistakable "you're browsing the archive"
            # banner. ESC leaves it (footer hint).
            banner = grid.fit(f"{workspace_label} · archived", width)
            lines = [row(f"{IdeSidebar.ARCHIVE_HEADER}{banner}{IdeSidebar.RESET}"), row("")]
        else:
            # Header: repo name left, `+` right — a TUI button (see _click_index). The repo
            # is fitted to leave the button its cell, so a long repo name truncates rather
            # than pushing `+` off the edge.
            create_icon = controls.get("create", IdeSidebar.PLUS)
            plus_w = grid.cell_width(create_icon)
            repo_text = grid.fit(
                workspace_label,
                max(1, width - plus_w - 1),
            )
            gap = " " * max(
                1,
                width - grid.cell_width(repo_text) - plus_w,
            )
            # The `+` inverts to a filled bar when keyboard-focused, so its focus is as
            # visible as a selected row.
            plus_active = on_plus and focused
            plus = (f"{IdeSidebar.SEL_CUR}{create_icon}{IdeSidebar.RESET}" if plus_active
                    else f"{IdeSidebar.PLUS_COLOR}{create_icon}{IdeSidebar.RESET}")
            lines = [row(f"{IdeSidebar.HEADER}{repo_text}{IdeSidebar.RESET}{gap}{plus}"), row("")]
        if not sessions:
            msg = ("(no match)" if query else
                   "(no archived sessions)" if archive_mode else "(no ide sessions)")
            lines.append(row(f"{IdeSidebar.DIM}{IdeSidebar._fit(msg, width)}{IdeSidebar.RESET}"))
        for i, s in enumerate(sessions):
            raw = s.get("name") or "?"
            # In the archive view nothing is "current" (these are dormant), and the cursor
            # is always free to select; in the active view the header/footer spots suppress
            # the row highlight so two things aren't lit at once.
            current = s.get("id") == this_session_id and not archive_mode
            selected = focused and i == cursor and (
                archive_mode or not (on_plus or on_filter or on_archive))
            agent = (
                (s.get("driver") or {}).get("id")
                or (s.get("agents") or {}).get("active")
                or "claude"
            )
            icon = driver_icons.get(agent, driver_icons.get("default", "•"))
            namew = grid.name_width
            base = IdeSidebar.ACTIVE if current else IdeSidebar.INACTIVE
            text = grid.fit(raw, namew)
            pad = " " * max(0, namew - grid.cell_width(text))
            # `current`, NOT `selected`: the dot clears when you ACTIVATE the session
            # (switch to its window), not when the browse cursor merely passes over it.
            glyph, gcolor = IdeSidebar._status_dot(
                skill_dir, s, current, settings
            )
            rel = IdeSidebar._rel_time(s.get("last_active")) or ""
            state = IdeSidebar._provider_mark(
                skill_dir, s, state_provider, settings, provider_registry
            )
            slot_marks = [
                IdeSidebar._provider_mark(
                    skill_dir, s, provider_id, settings, provider_registry
                )
                for provider_id in slot_providers
            ]
            subagent_mark = IdeSidebar._provider_mark(
                skill_dir, s, "subagents", settings, provider_registry
            )
            alert_mark = (tmux_alerts or {}).get(s.get("id") or "", "")
            right_mark = subagent_mark or alert_mark
            # At one-row density the metadata row is gone. Preserve transient state by
            # borrowing the leading driver track until the activity clears.
            leading_mark = state if entry_rows == 1 and state else icon
            driver_mark = grid.pad(leading_mark, grid.leading_cells)
            # `entry_rows` (from `_entry_rows`) decides how much of each entry is drawn:
            # 3 = name + sub-line + spacer, 2 = drop the spacer, 1 = name only. The click
            # math divides by the SAME number, so the two can never disagree.
            show_sub = entry_rows >= 2
            show_gap = entry_rows >= IdeSidebar.ENTRY_ROWS
            inline_right = right_mark if not show_sub else ""
            inline_right_width = grid.cell_width(inline_right)

            def append_inline_subagent(content: str) -> str:
                if not inline_right:
                    return content
                spacer = " " * max(
                    1,
                    width - grid.cell_width(IdeSidebar._strip_ansi(content)) - inline_right_width,
                )
                return f"{content}{spacer}{inline_right}"

            if selected:
                # Full 2-line box: icon + name + time. The real bg colour means the
                # erase-to-EOL paints BOTH rows out to the pane edge (see _row).
                sel = IdeSidebar.SEL_CUR if current else IdeSidebar.SEL_ALT
                dot_text = grid.pad(glyph, grid.status_cells)
                lines.append(
                    row(
                        append_inline_subagent(
                            f"{driver_mark} {text}{pad} {dot_text}"
                        ),
                        True,
                        sel,
                    )
                )
                if show_sub:
                    lines.append(
                        row(
                            grid.metadata_row(
                                state=state,
                                slots=slot_marks,
                                age=rel,
                                right_status=right_mark,
                            ),
                            True,
                            sel,
                        )
                    )
            elif current:
                # The window you're IN but not browsing: fill both rows with the subtle
                # CUR_BG. Inside the fill, KEEP_BG (not RESET) ends each colour so the
                # background survives to the pane edge.
                keep = IdeSidebar.KEEP_BG
                dot_text = grid.pad(glyph, grid.status_cells)
                dot = f"{gcolor}{dot_text}{keep}" if glyph else dot_text
                lines.append(
                    row(
                        append_inline_subagent(
                            f"{driver_mark} {IdeSidebar.CUR_FG}{text}{keep}{pad} {dot}"
                        ),
                        True,
                        IdeSidebar.CUR_BG,
                    )
                )
                if show_sub:
                    lines.append(
                        row(
                            grid.metadata_row(
                                state=state,
                                slots=slot_marks,
                                age=rel,
                                age_style=IdeSidebar.DIM,
                                reset=keep,
                                right_status=right_mark,
                            ),
                            True,
                            IdeSidebar.CUR_BG,
                        )
                    )
            else:
                dot_text = grid.pad(glyph, grid.status_cells)
                dot = f"{gcolor}{dot_text}{IdeSidebar.RESET}" if glyph else dot_text
                lines.append(
                    row(
                        append_inline_subagent(
                            f"{driver_mark}{IdeSidebar.RESET} {base}{text}{IdeSidebar.RESET}{pad} {dot}"
                        )
                    )
                )
                if show_sub:
                    lines.append(
                        row(
                            grid.metadata_row(
                                state=state,
                                slots=slot_marks,
                                age=rel,
                                age_style=IdeSidebar.DIM,
                                reset=IdeSidebar.RESET,
                                right_status=right_mark,
                            )
                        )
                    )
            if show_gap:
                lines.append(row(""))
        # The per-entry trailing blank is the ONE gap. Below it goes the cursor's own
        # line: the query if you've typed (cursor lands right after your text), else
        # an empty line for the cursor to park on — otherwise it sits *on* the gap and
        # reads as no gap at all. A bare prompt char is never drawn (reads as a glitch).
        if not sessions:
            lines.append(row(""))   # no per-entry blank to lean on
        if archive_mode:
            # Archive view footer: the filter input, then an ESC-to-return hint.
            fg = IdeSidebar.SEL_ALT if (on_filter and focused) else IdeSidebar.DIM
            filter_row = len(lines)
            lines.append(row(f"{fg}{IdeSidebar._fit('filter: ' + query if query else 'filter…', width)}"
                             f"{IdeSidebar.RESET}"))
            lines.append(row(f"{IdeSidebar.DIM}{IdeSidebar._fit('esc ← back · enter = resume', width)}"
                             f"{IdeSidebar.RESET}"))
            IdeSidebar._cursor_rc = (filter_row + 1, (len('filter: ') + len(query) + 1) if query else 1)
        else:
            # Active view footer: the filter line (a real focus stop), a BLANK gap, then the
            # hidden `show archive` affordance — both invert when arrow-focused. The gap
            # keeps `show archive` from reading as part of the filter block.
            fsel = (on_filter and focused)
            ftext = (query or "filter…")
            filter_row = len(lines)
            if fsel:
                lines.append(row(f"{IdeSidebar._fit(ftext, width)}", True, IdeSidebar.SEL_ALT))
            else:
                lines.append(row(f"{IdeSidebar.ACTIVE if query else IdeSidebar.DIM}"
                                 f"{IdeSidebar._fit(ftext, width)}{IdeSidebar.RESET}"))
            lines.append(row(""))   # one blank line ABOVE `show archive` (Phase 15 bug)
            asel = (on_archive and focused)
            archive_icon = controls.get("archive", "▾")
            alabel = IdeSidebar._fit(f"{archive_icon} show archive", width)
            if asel:
                lines.append(row(alabel, True, IdeSidebar.SEL_ALT))
            else:
                lines.append(row(f"{IdeSidebar.DIM}{alabel}{IdeSidebar.RESET}"))
            # Park the cursor in the filter field (after any typed query), NOT at the end of
            # the `show archive` line — so the block sits where you'd type (Phase 15 bug).
            IdeSidebar._cursor_rc = (filter_row + 1, (len(query) + 1) if query else 1)
        return lines

    @staticmethod
    def _reload_watch() -> dict:
        """Compatibility shim for the old render loop, now keyed by package identity."""
        module_dir = Path(__file__).resolve().parent
        watched = (
            module_dir / name
            for name in (
                "sidebar.py",
                "sidebar_grid.py",
                "sidebar_plugins.py",
                "state_compat.py",
                "git_status.py",
                "config.py",
            )
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "package_version": __version__,
            "expected_version": os.environ.get("HIVE_IDE_VERSION"),
            "source": os.environ.get("HIVE_IDE_SOURCE"),
            "modules": {
                str(path): path.stat().st_mtime_ns
                for path in watched
                if path.exists()
            },
        }

    @staticmethod
    def _version_error() -> str | None:
        protocol = os.environ.get("HIVE_IDE_PROTOCOL_VERSION")
        if protocol and protocol != str(PROTOCOL_VERSION):
            return f"protocol {protocol} is incompatible with {PROTOCOL_VERSION}"
        return None

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        args = argv if argv is not None else sys.argv[1:]
        if "--state-home" in args:
            parser = argparse.ArgumentParser(prog="python -m hive_ide.sidebar")
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--session-id", required=True)
            parser.add_argument("--tmux-socket", required=True)
            parsed = parser.parse_args(args)
            skill_dir, repo = Path(parsed.state_home), parsed.workspace_key
            found = StateIO.find_by_id(skill_dir, repo, parsed.session_id)
            if found is None:
                sys.stderr.write(f"hive-ide sidebar: no session {parsed.session_id}\n")
                return 1
            this_session_id = parsed.session_id
            os.environ["HIVE_IDE_TMUX_SOCKET"] = parsed.tmux_socket
        else:
            if len(args) < 3:
                sys.stderr.write("usage: ide_sidebar.py <state_home> <workspace> <window>\n")
                return 2
            skill_dir, repo, display_name = Path(args[0]), args[1], args[2]
            found = StateIO.find_by_identity(skill_dir, repo, display_name)
            if found is None or not found[2].get("id"):
                sys.stderr.write(f"hive-ide sidebar: no session {display_name}\n")
                return 1
            this_session_id = found[2]["id"]
        if mismatch := IdeSidebar._version_error():
            sys.stderr.write(f"hive-ide sidebar: {mismatch}\n")
            return 1
        IdeSidebar._repo_hint = repo
        snapshot = StateIO.read_config_snapshot(skill_dir, repo) or {}
        sidebar_settings = snapshot.get("sidebar") or _DEFAULT_SIDEBAR
        provider_registry = SidebarProviderRegistry.from_snapshot(
            sidebar_settings.get("providers") or _DEFAULT_SIDEBAR["providers"]
        )
        reload_baseline = IdeSidebar._reload_watch()   # exit-and-reload when source changes
        fd = sys.stdin.fileno()
        interactive = bool(termios) and sys.stdin.isatty()
        saved = termios.tcgetattr(fd) if interactive else None
        focused = IdeSidebar._initial_focus()
        if interactive:
            tty.setcbreak(fd)     # unbuffered, no echo; Ctrl-C still raises KeyboardInterrupt
            sys.stdout.write(IdeSidebar.FOCUS_ON + IdeSidebar.MOUSE_ON)
        cursor, cursor_session_id, query, buf, on_plus = -1, "", "", b"", False
        # Downward focus chain below the list: the filter line, then a hidden "show
        # archive" affordance. `archive_mode` swaps the whole list for the repo's archive.
        on_filter = on_archive = archive_mode = False
        try:
            while True:
                # Running stale code? A ship/merge rewrote this module or `ide_state` since
                # we started — exit so the keep-alive wrapper restarts us on fresh code,
                # instead of forever rendering the old registry (the Phase-35 ghost). Cheap
                # (two stats) and checked before drawing, so no stale frame is painted.
                if IdeSidebar._reload_watch() != reload_baseline:
                    return 0
                if focused and not IdeSidebar._pane_active():
                    focused = False
                # Every window runs its OWN sidebar, so cursor/query are per-process. Left
                # alone they drift apart and arriving at a window shows a cursor parked
                # where you last left it — reading as an out-of-sync bug. So a sidebar that
                # isn't being browsed always shows the canonical view: no filter, cursor on
                # the session you're actually in. Only one window is current at a time, so
                # every sidebar then agrees without any shared state.
                if not focused:
                    # Leaving the pane abandons a half-typed filter, header focus, and any
                    # archive browsing — an unfocused sidebar always shows the canonical
                    # active view centred on the window you're in.
                    query, on_plus = "", False
                    on_filter = on_archive = archive_mode = False
                if archive_mode:
                    sessions = IdeSidebar._filter(StateIO.list_archived(skill_dir, repo), query)
                else:
                    sessions = IdeSidebar._filter(StateIO.list_sessions(skill_dir, repo), query)
                names = [s.get("name") or "?" for s in sessions]
                session_ids = [s.get("id") or "" for s in sessions]
                active_session_id = IdeSidebar._active_session_id(this_session_id)
                # In archive mode / on a below-list spot, the cursor is the user's; don't
                # snap it back to this_window (which isn't in the archive list anyway).
                free = on_filter or on_archive or archive_mode
                cursor, cursor_session_id = IdeSidebar._reconcile_cursor(
                    session_ids,
                    cursor,
                    focused=focused,
                    free=free,
                    archive_mode=archive_mode,
                    current_session_id=active_session_id,
                    cursor_session_id=cursor_session_id,
                )
                width = IdeSidebar._width()
                # Density for THIS draw — and the divisor the click math must reuse, so a
                # short pane can't desync rendering from hit-testing.
                entry_rows = IdeSidebar._entry_rows(len(names), IdeSidebar._height(),
                                                    archive_mode)
                lines = IdeSidebar.render_lines(skill_dir, sessions, repo, active_session_id,
                                                cursor, width, query, focused, on_plus,
                                                on_filter, on_archive, archive_mode,
                                                entry_rows, sidebar_settings,
                                                provider_registry,
                                                IdeSidebar._tmux_alerts())
                # Each line carries its own erase-to-EOL (see _row).
                sys.stdout.write(IdeSidebar.NO_WRAP + IdeSidebar.HOME
                                 + "\n".join(lines) + IdeSidebar.CLEAR_BELOW)
                # Park the terminal cursor on the input row (filter/name) when focused, so
                # the block sits where you'd type instead of at the end of the last line
                # drawn (Phase 15). `render_lines` set `_cursor_rc` for this draw.
                rc = IdeSidebar._cursor_rc
                if interactive and focused and rc is not None:
                    sys.stdout.write(f"\x1b[{rc[0]};{rc[1]}H")
                sys.stdout.flush()
                if not interactive:
                    time.sleep(IdeSidebar.TICK_SECONDS)
                    continue
                # A carried tail waits only an ESC-timeout: the rest of a split report
                # lands within microseconds, so a tail that outlives it was never a
                # mouse report — it's a real key (a lone ESC) and must still fire.
                mouse_tail = buf.startswith(IdeSidebar.MOUSE_HEAD)
                wait = (IdeSidebar.TICK_SECONDS if not buf or mouse_tail
                        else IdeSidebar.PARTIAL_SECONDS)
                ready, _, _ = select.select([fd], [], [], wait)
                if ready:
                    archive_row = None if archive_mode else len(lines) - 1
                    click, wheel, data, buf = IdeSidebar._drain(
                        buf + os.read(fd, 64),
                        entry_rows,
                        archive_row=archive_row,
                    )
                    if wheel:
                        # The sidebar OWNS the wheel: move the browse cursor. Handling it
                        # here (instead of letting it fall through to tmux) is what stops
                        # the pane dropping into copy-mode and eating the arrow keys.
                        focused = True
                        if names:
                            cursor = max(0, min(cursor + wheel, len(names) - 1))
                            cursor_session_id = session_ids[cursor]
                        continue
                elif buf and mouse_tail:
                    # An unfinished report is NOT keys — drop it. Flushing it would end the
                    # carry, so the rest of that report ("4;14;27M") would arrive headless,
                    # look printable, and land in the filter.
                    click, data, buf = None, b"", b""
                elif buf:
                    click, data, buf = None, buf, b""   # ambiguous ESC → a real key
                else:
                    continue
                if click is not None:
                    # A click IS proof tmux focused this pane — and the focus-in event
                    # that says so often rides in the same read, then gets dropped by the
                    # `continue` below. Without setting it here, the next loop top hits
                    # `if not focused:` and wipes the header focus a click just set.
                    focused = True
                    options_index = IdeSidebar._options_index(click)
                    if options_index is not None:
                        if not archive_mode and 0 <= options_index < len(session_ids):
                            IdeSidebar._launch_options_modal(
                                skill_dir,
                                IdeSidebar._repo_of(skill_dir),
                                session_ids[options_index],
                            )
                        continue
                    if archive_mode:
                        if 0 <= click < len(session_ids) and IdeSidebar._resume(
                            skill_dir, session_ids[click]
                        ):
                            archive_mode, query, cursor = False, "", 0   # resumed → back to active
                            cursor_session_id = ""
                    elif click == IdeSidebar.PLUS_HIT:
                        # Same action as Enter/Space on the focused `+` and as <prefix>+:
                        # the guided modal. This branch used to open the OLD inline name
                        # prompt — the modal commit rewired the keyboard paths and the key
                        # binding but missed the click, so clicking `+` silently landed you
                        # in a superseded UI while every other route showed the modal.
                        on_plus, on_filter, on_archive, query, cursor = False, False, False, "", 0
                        IdeSidebar._launch_new_modal(skill_dir, IdeSidebar._repo_of(skill_dir))
                    elif click == IdeSidebar.ARCHIVE_HIT:
                        archive_mode, on_archive, on_filter, query, cursor = True, False, False, "", 0
                        cursor_session_id = ""
                    elif 0 <= click < len(names):
                        cursor, on_plus, on_filter, on_archive, query = click, False, False, False, ""
                        cursor_session_id = session_ids[cursor]
                        if IdeSidebar._switch(session_ids[cursor], skill_dir):
                            focused, cursor_session_id = IdeSidebar._after_activation(
                                session_ids, cursor
                            )
                    continue
                key = IdeSidebar._key(data)
                # Any key input means THIS pane is focused — tmux only routes input to the
                # ACTIVE pane. This is the fallback for terminals that never send focus-events
                # (mobile clients like Termius): without it `focused` stays False, the cursor
                # snaps back to the current window every render, and the ARROW KEYS look dead.
                if key != "focus_out":
                    focused = True
                if key == "focus_in":
                    focused = True
                elif key == "focus_out":
                    focused = False
                elif key == "up":
                    # Vertical chain (active mode): + · sessions · [filter] · [show archive].
                    # In archive mode it's just the archived list.
                    if archive_mode:
                        cursor = max(0, cursor - 1)
                    elif on_archive:
                        on_archive, on_filter = False, True
                    elif on_filter:
                        on_filter = False
                        cursor = len(names) - 1 if names else 0
                        on_plus = not names
                    else:
                        on_plus = cursor <= 0
                        cursor = max(0, cursor - 1)
                elif key == "down":
                    if archive_mode:
                        cursor = min(len(names) - 1, cursor + 1) if names else 0
                    elif on_plus:
                        on_plus, cursor = False, 0
                    elif on_filter:
                        on_filter, on_archive = False, True
                    elif on_archive:
                        pass
                    elif not names or cursor >= len(names) - 1:
                        on_filter, cursor = True, cursor   # off the last session → the filter line
                    else:
                        cursor += 1
                elif key == "right":
                    # Leave the browse cursor alone — Right is "back to chat", not a pick.
                    IdeSidebar._focus_agent(active_session_id)
                elif key == "backspace":
                    query, cursor = query[:-1], 0
                elif key in ("clear", "escape"):
                    # Esc is the universal back/cancel: archive view → active list; else
                    # clear the filter and any header/footer focus.
                    if archive_mode:
                        archive_mode, query, cursor = False, "", 0
                    else:
                        query, cursor, on_plus, on_filter, on_archive = "", 0, False, False, False
                elif key is None:
                    # Space ON the focused `+` presses it (a button's natural key); Space on
                    # `show archive` opens it. Any other printable is filter text — typing
                    # always drops you into the list (or the archive list) and filters it.
                    if on_plus and data == b" ":
                        IdeSidebar._launch_new_modal(skill_dir, IdeSidebar._repo_of(skill_dir))
                        on_plus, query = False, ""
                    elif on_archive and data == b" ":
                        archive_mode, on_archive, on_filter, query, cursor = True, False, False, "", 0
                    else:
                        text = IdeSidebar._printable(data)
                        if text:
                            query, cursor = query + text, 0
                            if not archive_mode:
                                on_plus = on_filter = on_archive = False
                elif key == "enter":
                    if archive_mode:
                        if session_ids and IdeSidebar._resume(
                            skill_dir, session_ids[cursor]
                        ):
                            archive_mode, query, cursor = False, "", 0
                    elif on_plus:
                        # press the `+` → the guided new-session modal
                        IdeSidebar._launch_new_modal(skill_dir, IdeSidebar._repo_of(skill_dir))
                        on_plus, query = False, ""
                    elif on_archive:
                        archive_mode, on_archive, query, cursor = True, False, "", 0   # open the archive
                    elif on_filter:
                        pass   # the filter line: typing filters; Enter does nothing here
                    elif session_ids:
                        if IdeSidebar._switch(session_ids[cursor], skill_dir):
                            focused, cursor_session_id = IdeSidebar._after_activation(
                                session_ids, cursor
                            )
                if 0 <= cursor < len(session_ids):
                    cursor_session_id = session_ids[cursor]
                else:
                    cursor_session_id = ""
        except KeyboardInterrupt:
            return 0
        finally:
            if interactive:
                sys.stdout.write(IdeSidebar.MOUSE_OFF + IdeSidebar.FOCUS_OFF)
                sys.stdout.flush()
                if saved is not None:
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)


if __name__ == "__main__":
    raise SystemExit(IdeSidebar.main())
