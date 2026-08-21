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
    ACTION_ROW = 4

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
    def _draw(record: dict, repo: str, sel: int, rename_value: str = "") -> None:
        M = IdeNewModal
        driver = (record.get("driver") or {}).get("id") or "term"
        actions = IdeOptionsModal._actions(record)
        groups = IdeOptionsModal._grouped_actions(record)
        o = [M.CLR, f"  {M.BOLD}Session options{M.RST}\n"]
        o.append(
            f"  {M.NAME}{record.get('name') or '?'}{M.RST}"
            f"  {M.DIM}{Path(repo).name or repo} · {driver}{M.RST}{M.EL}\n\n"
        )
        offset = 0
        for group, group_actions in groups:
            o.append(f"  {M.DIM}{group}{M.RST}{M.EL}\n")
            for action, label, note in group_actions:
                i = actions.index((action, label, note), offset)
                arrow = "▸" if i == sel else " "
                if i == sel:
                    o.append(f"  {M.SEL} {arrow} {label:<14} {note} {M.RST}{M.EL}\n")
                else:
                    o.append(f"   {arrow} {label:<14} {M.DIM}{note}{M.RST}{M.EL}\n")
            offset += len(group_actions)
        if rename_value:
            o.append(f"\n  {M.DIM}new name:{M.RST} {M.NAME}{rename_value}{M.RST}{M.EL}")
        o.append(
            f"\n  {M.DIM}↑/↓ or j/k · Enter · Esc → cancel"
            f" · rename types after selecting rename{M.RST}{M.EL}"
        )
        sys.stdout.write("".join(o))
        sys.stdout.flush()

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
    def _action_rows(record: dict) -> dict[int, int]:
        row = IdeOptionsModal.ACTION_ROW
        index = 0
        rows: dict[int, int] = {}
        for _group, actions in IdeOptionsModal._grouped_actions(record):
            row += 1
            for _action in actions:
                rows[row] = index
                row += 1
                index += 1
        return rows

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
    def _rename_prompt(fd: int, record: dict, repo: str, sel: int) -> str | None:
        value = record.get("name") or ""
        while True:
            IdeOptionsModal._draw(record, repo, sel, value)
            key = IdeOptionsModal._getkey(fd)
            if key == "esc":
                return None
            if key == "enter":
                cleaned = " ".join(value.split())
                return cleaned if cleaned else None
            if key in ("bs", "delete", "backspace"):
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
                IdeOptionsModal._draw(record, repo, sel)
                actions = IdeOptionsModal._actions(record)
                if sel >= len(actions):
                    sel = len(actions) - 1
                key = IdeOptionsModal._getkey(
                    fd, mouse_rows=IdeOptionsModal._action_rows(record)
                )
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
                if action == "rename":
                    name = IdeOptionsModal._rename_prompt(fd, record, repo, sel)
                    if name is None:
                        continue
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
