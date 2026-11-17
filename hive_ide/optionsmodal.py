#!/usr/bin/env python3
"""Session operations popup for the tmux IDE."""

from __future__ import annotations

import argparse
import os
import re
import select
import subprocess
import sys
from pathlib import Path

from . import modal_io

from .agentmodal import IdeAgentModal
from .newmodal import IdeNewModal
from .python_cmd import PythonCommand
from .state_compat import StateIO

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None


class IdeOptionsModal:
    """One popup for common session maintenance actions."""

    MOUSE_RE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([mM])")

    GROUPS = [
        (
            "Open",
            [
                ("chat", "chat", "focus the agent pane"),
                ("plan", "plan pane", "open or focus the plan pane"),
                ("plan-modal", "plan modal", "open the plan in a popup"),
                ("tasks-modal", "tasks modal", "open tasks at first unfinished"),
                ("scratchpad", "scratchpad", "open notes in a plan popup"),
            ],
        ),
        (
            "Session",
            [
                ("card", "session info", "show the info modal"),
                ("agent", "change agent", "open the agent picker"),
                ("rename", "rename", "change the display label"),
            ],
        ),
        (
            "Maintenance",
            [
                ("repair", "repair", "heal session state and redraw"),
                ("archive", "archive", "close and move to archive"),
            ],
        ),
    ]
    DRIVER_RENAME_ACTIONS = [
        ("driver-rename", "rename driver", "send /rename when agent is idle"),
    ]
    SLEEP_ACTION = ("sleep", "sleep agent", "stop agent, keep session listed")
    CLEAR_PLAN_ACTION = ("plan-clear", "clear plan", "unlink the session plan")
    # Claude absorbs a typed /rename mid-turn; other drivers can be interrupted by it.
    DRIVER_RENAME_DEFAULT = {"claude": True, "codex": False}

    @staticmethod
    def _actions(record: dict) -> list[tuple[str, str, str]]:
        return [
            action
            for _group, group_actions in IdeOptionsModal._grouped_actions(record)
            for action in group_actions
        ]

    @staticmethod
    def _command(
        skill_dir: Path,
        session_id: str,
        action: str,
        *,
        name: str | None = None,
    ) -> tuple[bool, str]:
        socket_args = (
            [f"--tmux-socket={IdeNewModal._tmux_socket}"]
            if IdeNewModal._tmux_socket
            else []
        )
        if action == "chat":
            return IdeNewModal._cli(
                skill_dir,
                ["--quiet", "chat", f"--session-id={session_id}", *socket_args],
            )
        if action == "plan":
            return IdeNewModal._cli(
                skill_dir,
                [
                    "--quiet",
                    "plan",
                    f"--session-id={session_id}",
                    *socket_args,
                    "--focus",
                ],
            )
        if action == "plan-modal":
            return IdeOptionsModal._background_cli(
                skill_dir,
                [
                    "--quiet",
                    "plan-popup",
                    "--mode=plan",
                    f"--session-id={session_id}",
                    *socket_args,
                ],
            )
        if action == "tasks-modal":
            return IdeOptionsModal._background_cli(
                skill_dir,
                [
                    "--quiet",
                    "plan-popup",
                    "--mode=tasks",
                    f"--session-id={session_id}",
                    *socket_args,
                ],
            )
        if action == "scratchpad":
            return IdeOptionsModal._background_cli(
                skill_dir,
                ["--quiet", "scratchpad", f"--session-id={session_id}", *socket_args],
            )
        if action == "repair":
            return IdeNewModal._cli(
                skill_dir,
                ["--quiet", "repair", f"--session-id={session_id}", *socket_args],
            )
        if action == "sleep":
            return IdeNewModal._cli(
                skill_dir,
                ["--quiet", "sleep", f"--session-id={session_id}", *socket_args],
            )
        if action == "archive":
            return IdeNewModal._cli(
                skill_dir,
                ["--quiet", "archive", f"--session-id={session_id}", *socket_args],
            )
        if action == "rename":
            if not name:
                return False, "A new name is required."
            return IdeNewModal._cli(
                skill_dir,
                [
                    "--quiet",
                    "rename",
                    f"--session-id={session_id}",
                    f"--name={name}",
                    *socket_args,
                ],
            )
        if action == "driver-rename":
            return IdeNewModal._cli(
                skill_dir,
                [
                    "--quiet",
                    "driver-rename",
                    f"--session-id={session_id}",
                    *([f"--name={name}"] if name else []),
                    *socket_args,
                ],
            )
        if action == "plan-clear":
            return IdeNewModal._cli(
                skill_dir,
                [
                    "--quiet",
                    "plan-set",
                    f"--session-id={session_id}",
                    "--clear",
                    *socket_args,
                ],
            )
        return False, f"Unsupported action: {action}"

    @staticmethod
    def _background_cli(skill_dir: Path, args: list[str]) -> tuple[bool, str]:
        """Run a popup-opening CLI after this options popup has returned."""
        try:
            command = PythonCommand.module_command(
                "cli",
                [
                    "--state-home",
                    str(skill_dir),
                    "--workspace-key",
                    IdeNewModal._workspace_key,
                    *args,
                ],
                python=sys.executable,
            )
            tmux = ["tmux"]
            if IdeNewModal._tmux_socket:
                tmux.extend(["-L", IdeNewModal._tmux_socket])
            p = subprocess.run(
                [*tmux, "run-shell", "-b", f"sleep 0.05; {command}"],
                capture_output=True,
                text=True,
            )
            return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
        except OSError as exc:
            return False, str(exc)

    @staticmethod
    def _popup(skill_dir: Path, repo: str, session_id: str, kind: str) -> int:
        command = PythonCommand.module_command(
            "popup",
            [
                "--kind",
                kind,
                "--state-home",
                str(skill_dir),
                "--workspace-key",
                repo,
                "--session-id",
                session_id,
                *(
                    ["--tmux-socket", IdeNewModal._tmux_socket]
                    if IdeNewModal._tmux_socket
                    else []
                ),
            ],
            python=sys.executable,
        )
        try:
            tmux = ["tmux"]
            if IdeNewModal._tmux_socket:
                tmux.extend(["-L", IdeNewModal._tmux_socket])
            return subprocess.run(
                [*tmux, "run-shell", "-b", command],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
        except OSError:
            return 1

    @staticmethod
    def _context(skill_dir: Path, argv: list[str]) -> tuple[str, str, dict] | None:
        sid = IdeNewModal._ask_tmux("#{@hive_ide_session_id}")
        repo = IdeNewModal._ask_tmux("#{@hive_ide_workspace_key}")
        if sid and repo:
            found = StateIO.find_by_id(skill_dir, repo, sid)
            return (found[0], found[2]["id"], found[2]) if found else None
        repo = IdeNewModal._resolve(argv[2] if len(argv) > 2 else None, "#{session_name}")
        window = IdeNewModal._resolve(argv[3] if len(argv) > 3 else None, "#{window_name}")
        if not repo or not window:
            return None
        found = StateIO.find_by_identity(skill_dir, repo, window)
        return (found[0], found[2]["id"], found[2]) if found else None

    @staticmethod
    def _lines(
        record: dict,
        repo: str,
        sel: int,
        rename_value: str = "",
        driver_too: bool | None = None,
        rows: int | None = None,
        width: int | None = None,
    ) -> tuple[list[str], dict[int, int]]:
        """The modal's lines and {screen row: action index}, without printing.

        Separated from `_draw` so the popup can MEASURE what will be drawn
        instead of re-deriving a row count beside it. That duplicate arithmetic
        agreed with itself while missing that the footer is wider than the popup
        and wraps onto a second row.
        """
        M = IdeNewModal
        if width is None:
            width = modal_io.cols_available()
        driver = (record.get("driver") or {}).get("id") or "term"
        actions = IdeOptionsModal._actions(record)
        groups = IdeOptionsModal._grouped_actions(record)
        # Built as LINES, then fitted to the rows this terminal actually has.
        # Writing straight out is what lost the title on a short screen: the
        # content is 21-22 rows and the popup gets a percentage of the client, so
        # on a handheld the overflow scrolled off the top. Header and footer are
        # pinned; the group/item list is what scrolls.
        header = [
            f"  {M.BOLD}Session options{M.RST}",
            f"  {M.NAME}{record.get('name') or '?'}{M.RST}"
            f"  {M.DIM}{Path(repo).name or repo} · {driver}{M.RST}{M.EL}",
            "",
        ]
        def build(*, group_headings: bool, notes: bool):
            """One body variant. Returns `(lines, selected_line, action_of_line)`.

            Which action each body line belongs to is keyed by the identity of the
            line object. `fit` returns the very strings handed to it, so after
            clipping the surviving lines map back without re-deriving anything.
            """
            lines: list[str] = []
            selected_at = 0
            seen = 0
            of_line: dict[int, int] = {}
            for group, group_actions in groups:
                if group_headings:
                    lines.append(f"  {M.DIM}{group}{M.RST}{M.EL}")
                for action, label, note in group_actions:
                    i = actions.index((action, label, note), seen)
                    arrow = "▸" if i == sel else " "
                    shown = note if notes else ""
                    if i == sel:
                        selected_at = len(lines)
                        line = f"  {M.SEL} {arrow} {label:<14} {shown} {M.RST}{M.EL}"
                    else:
                        line = f"   {arrow} {label:<14} {M.DIM}{shown}{M.RST}{M.EL}"
                    lines.append(line)
                    of_line[id(line)] = i
                seen += len(group_actions)
            return lines, selected_at, of_line

        # DECORATION GOES BEFORE CONTENT. A phone with its keyboard up reports 23
        # rows, which is a 19-row popup interior; the full body is 13 rows of list
        # plus 3 of header and a footer that wraps to two, and `repair` and
        # `archive` fell off the bottom behind a `more` marker. An action the user
        # cannot see is an action they cannot reach -- there is no scrolling on
        # that screen -- while a group heading and a one-line note are things they
        # can do without. So: drop the three group headings, then the notes, and
        # only then let `fit` window the list as it always has.
        footer: list[str] = []
        if rename_value:
            # Trimmed to its row like the new-session fields: a valid 24-char
            # name wraps to three rows at a 16-cell interior and fitting then
            # drops it whole, so two different edits draw the same frame while
            # Enter still returns the edited value.
            room = width - len("  new name: ")
            footer.append(
                f"  {M.DIM}new name:{M.RST} "
                f"{M.NAME}{modal_io.field_tail(rename_value, room)}{M.RST}{M.EL}"
            )
            if driver_too is not None:
                box = "[x]" if driver_too else "[ ]"
                footer.append(
                    f"  {box} also rename {driver} {M.DIM}(Tab toggles){M.RST}{M.EL}"
                )
        footer.append(
            f"  {M.DIM}↑/↓ or j/k · Enter · Esc → cancel"
            f" · rename types after selecting rename{M.RST}{M.EL}"
        )
        body, selected_line, action_of_line = build(group_headings=True, notes=True)
        if rows is not None:
            chrome = modal_io.wrapped_rows(header, width) + modal_io.wrapped_rows(
                footer, width
            )
            for variant in ({"group_headings": False, "notes": True},
                            {"group_headings": False, "notes": False}):
                if modal_io.wrapped_rows(body, width) + chrome <= rows:
                    break
                body, selected_line, action_of_line = build(**variant)
        lines = modal_io.fit(
            header, body, footer, selected=selected_line, rows=rows, width=width,
            pin_selection=True,
        )
        # The click map MUST come from what was drawn. Deriving it from the full
        # action list independently was a real defect: once the list is windowed,
        # a screen row holds a different action than the arithmetic believed, so
        # a click ran the wrong one — and a mouse click goes straight to Enter,
        # with `archive` among the actions it could land on.
        # PHYSICAL screen rows, not line numbers: a line wider than the popup
        # wraps onto a second row and shifts everything below it down, so at a
        # narrow width the map pointed a click at a different action — measured
        # at an interior of 38 cells, eleven of them were wrong, and a click
        # runs its action immediately. A wrapped action owns both of its rows.
        mouse_rows: dict[int, int] = {}
        screen_row = 1
        for line in lines:
            cost = modal_io.line_rows(line, width)
            index = action_of_line.get(id(line))
            if index is None:
                # A screen too short for its own chrome gets ONE truncated line,
                # which is a new string and so has no identity to match. It can
                # still be a visible, selected action — leaving it unmapped made
                # it look clickable while clicking did nothing. Match it back by
                # prefix, and only when exactly one action can own it.
                index = IdeOptionsModal._action_for_truncated(
                    line, body, action_of_line
                )
            if index is not None:
                for offset in range(cost):
                    mouse_rows[screen_row + offset] = index
            screen_row += cost
        return lines, mouse_rows

    @staticmethod
    def _action_for_truncated(
        drawn: str, body: list[str], action_of_line: dict[int, int]
    ) -> int | None:
        """The action a TRUNCATED line came from, or None if it is ambiguous."""
        plain = modal_io.strip_ansi(drawn)
        if not plain.strip():
            return None
        matches = {
            action_of_line[id(candidate)]
            for candidate in body
            if id(candidate) in action_of_line
            and modal_io.strip_ansi(candidate).startswith(plain)
        }
        return matches.pop() if len(matches) == 1 else None

    @staticmethod
    def _draw(
        record: dict,
        repo: str,
        sel: int,
        rename_value: str = "",
        driver_too: bool | None = None,
    ) -> dict[int, int]:
        """Draw the modal; return {screen row: action index} for the mouse."""
        lines, mouse_rows = IdeOptionsModal._lines(
            record, repo, sel, rename_value, driver_too
        )
        sys.stdout.write(IdeNewModal.CLR + "\n".join(lines))
        sys.stdout.flush()
        return mouse_rows

    @staticmethod
    def _grouped_actions(record: dict) -> list[tuple[str, list[tuple[str, str, str]]]]:
        groups = [(name, list(actions)) for name, actions in IdeOptionsModal.GROUPS]
        driver = (record.get("driver") or {}).get("id")
        if driver in {"claude", "codex"}:
            session_actions = groups[1][1]
            rename_index = next(
                (
                    index
                    for index, (action, _label, _note) in enumerate(session_actions)
                    if action == "rename"
                ),
                len(session_actions),
            )
            session_actions[rename_index + 1:rename_index + 1] = IdeOptionsModal.DRIVER_RENAME_ACTIONS
        if IdeOptionsModal._linked_plan(record):
            groups[1][1].append(IdeOptionsModal.CLEAR_PLAN_ACTION)
        if driver != "term":
            maintenance_actions = groups[2][1]
            archive_index = next(
                (
                    index
                    for index, (action, _label, _note) in enumerate(maintenance_actions)
                    if action == "archive"
                ),
                len(maintenance_actions),
            )
            maintenance_actions[archive_index:archive_index] = [
                IdeOptionsModal.SLEEP_ACTION
            ]
        return groups

    @staticmethod
    def _linked_plan(record: dict) -> str | None:
        """The linked plan path, from a store record (dict) or a StateIO one (str)."""
        plan = record.get("plan")
        path = plan.get("path") if isinstance(plan, dict) else plan
        return path if isinstance(path, str) and path else None

    @staticmethod
    def _mouse_selection(data: bytes, rows: dict[int, int]) -> int | None:
        match = IdeOptionsModal.MOUSE_RE.fullmatch(data)
        if not match or match.group(4) != b"M":
            return None
        button = int(match.group(1))
        if button != 0:
            return None
        row = int(match.group(3))
        return rows.get(row)

    @staticmethod
    def _getkey(fd: int, *, mouse_rows: dict[int, int] | None = None) -> str:
        data = os.read(fd, 1)
        if not data or data == b"\x03":
            return "esc"
        if data != b"\x1b":
            if data in (b"\r", b"\n"):
                return "enter"
            if data in (b"\x7f", b"\x08"):
                return "bs"
            return data.decode("utf-8", "ignore") or "other"
        r, _, _ = select.select([fd], [], [], IdeNewModal.ESC_PEEK_SECONDS)
        if not r:
            return "esc"
        rest = os.read(fd, 1)
        if rest == b"[":
            r, _, _ = select.select([fd], [], [], IdeNewModal.ESC_PEEK_SECONDS)
            code = os.read(fd, 1) if r else b""
            if code == b"<":
                report = bytearray(b"\x1b[<")
                while len(report) < 32:
                    r, _, _ = select.select([fd], [], [], IdeNewModal.ESC_PEEK_SECONDS)
                    if not r:
                        break
                    chunk = os.read(fd, 1)
                    report += chunk
                    if chunk in (b"M", b"m"):
                        break
                picked = IdeOptionsModal._mouse_selection(bytes(report), mouse_rows or {})
                return f"mouse:{picked}" if picked is not None else "other"
            return {
                b"A": "up",
                b"B": "down",
                b"C": "right",
                b"D": "left",
            }.get(code, "other")
        if rest == b"O":
            r, _, _ = select.select([fd], [], [], IdeNewModal.ESC_PEEK_SECONDS)
            code = os.read(fd, 1) if r else b""
            return {b"A": "up", b"B": "down", b"C": "right", b"D": "left"}.get(
                code, "other"
            )
        return "esc"

    @staticmethod
    def _rename_prompt(
        fd: int, record: dict, repo: str, sel: int
    ) -> tuple[str, bool] | None:
        """Return the new name and whether to also send `/rename` to the driver."""
        value = record.get("name") or ""
        driver = (record.get("driver") or {}).get("id")
        driver_too = IdeOptionsModal.DRIVER_RENAME_DEFAULT.get(driver)
        while True:
            IdeOptionsModal._draw(record, repo, sel, value, driver_too)
            key = IdeOptionsModal._getkey(fd)
            if key == "esc":
                return None
            if key == "enter":
                cleaned = " ".join(value.split())
                return (cleaned, bool(driver_too)) if cleaned else None
            if key == "\t":
                if driver_too is not None:
                    driver_too = not driver_too
            elif key in ("bs", "delete", "backspace"):
                value = value[:-1]
            elif key == "\x15":  # Ctrl-U
                value = ""
            elif key == "\x17":  # Ctrl-W
                value = value.rstrip().rsplit(" ", 1)[0]
            elif len(key) == 1 and key.lower() in "abcdefghijklmnopqrstuvwxyz0123456789-_ ":
                value = (value + key)[:24]

    @staticmethod
    def main(argv: list[str] | None = None) -> int:
        raw = sys.argv if argv is None else argv
        args = raw[1:]
        if "--state-home" in args:
            parser = argparse.ArgumentParser(prog="python -m hive_ide.optionsmodal")
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--session-id", required=True)
            parser.add_argument("--tmux-socket")
            parsed = parser.parse_args(args)
            skill_dir = Path(parsed.state_home)
            IdeNewModal._tmux_socket = parsed.tmux_socket or ""
            found = StateIO.find_by_id(skill_dir, parsed.workspace_key, parsed.session_id)
            ctx = (found[0], found[2]["id"], found[2]) if found else None
        else:
            if len(raw) < 2:
                sys.stderr.write("usage: ide_optionsmodal.py <state_home> [workspace] [window]\n")
                return 2
            skill_dir = Path(raw[1])
            ctx = IdeOptionsModal._context(skill_dir, raw)
        if ctx is None:
            return IdeNewModal._bail(
                "Could not determine which ide session this window is.",
                "run `hive-ide open` to rebuild the frame and session tags.",
            )
        repo, session_id, record = ctx
        IdeNewModal._workspace_key = repo
        if not (termios and tty and sys.stdin.isatty()):
            return 0
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()
        sel = 0
        try:
            while True:
                mouse_rows = IdeOptionsModal._draw(record, repo, sel)
                actions = IdeOptionsModal._actions(record)
                if sel >= len(actions):
                    sel = len(actions) - 1
                key = IdeOptionsModal._getkey(fd, mouse_rows=mouse_rows)
                if key == "esc":
                    return 0
                if key in ("up", "k"):
                    sel = (sel - 1) % len(actions)
                    continue
                if key in ("down", "j"):
                    sel = (sel + 1) % len(actions)
                    continue
                if key.startswith("mouse:"):
                    sel = int(key.split(":", 1)[1])
                    key = "enter"
                if key.isdigit() and 1 <= int(key) <= len(actions):
                    sel = int(key) - 1
                    key = "enter"
                if key != "enter":
                    continue
                action = actions[sel][0]
                if action == "agent":
                    return IdeAgentModal.main(
                        [
                            "ide_agentmodal.py",
                            "--state-home",
                            str(skill_dir),
                            "--workspace-key",
                            repo,
                            "--session-id",
                            session_id,
                            "--tmux-socket",
                            IdeNewModal._tmux_socket,
                        ]
                    )
                if action == "card":
                    return IdeOptionsModal._popup(skill_dir, repo, session_id, "card")
                name = None
                driver_too = False
                if action == "rename":
                    answer = IdeOptionsModal._rename_prompt(fd, record, repo, sel)
                    if answer is None:
                        continue
                    name, driver_too = answer
                ok, detail = IdeOptionsModal._command(
                    skill_dir, session_id, action, name=name
                )
                if ok and driver_too:
                    action = "driver-rename"
                    ok, detail = IdeOptionsModal._command(
                        skill_dir, session_id, action, name=name
                    )
                if ok:
                    return 0
                return IdeNewModal._bail(
                    f"Could not run session action {action!r}.",
                    detail or "command failed without output.",
                )
        finally:
            sys.stdout.write("\x1b[?25h")
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write(IdeNewModal.CLR)
            sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(IdeOptionsModal.main(sys.argv))
