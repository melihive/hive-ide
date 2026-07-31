#!/usr/bin/env python3
"""Session operations popup for the tmux IDE."""

from __future__ import annotations

import argparse
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

    ACTIONS = [
        ("chat", "current chat", "focus the agent pane"),
        ("plan", "current plan", "open or focus the plan pane"),
        ("agent", "change agent", "open the agent picker"),
        ("rename", "rename", "change the display label"),
        ("repair", "repair", "heal session state and redraw"),
        ("card", "session info", "show the info modal"),
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
        if action == "repair":
            return IdeNewModal._cli(
                skill_dir,
                ["--quiet", "repair", f"--session-id={session_id}", *socket_args],
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
        return False, f"Unsupported action: {action}"

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
        o = [M.CLR, f"  {M.BOLD}Session options{M.RST}\n"]
        o.append(
            f"  {M.NAME}{record.get('name') or '?'}{M.RST}"
            f"  {M.DIM}{Path(repo).name or repo} · {driver}{M.RST}{M.EL}\n\n"
        )
        for i, (_action, label, note) in enumerate(IdeOptionsModal.ACTIONS):
            arrow = "▸" if i == sel else " "
            if i == sel:
                o.append(f"  {M.SEL} {arrow} {label:<14} {note} {M.RST}{M.EL}\n")
            else:
                o.append(f"   {arrow} {label:<14} {M.DIM}{note}{M.RST}{M.EL}\n")
        if rename_value:
            o.append(f"\n  {M.DIM}new name:{M.RST} {M.NAME}{rename_value}{M.RST}{M.EL}")
        o.append(
            f"\n  {M.DIM}↑/↓ or j/k · Enter · Esc → cancel"
            f" · rename types after selecting rename{M.RST}{M.EL}"
        )
        sys.stdout.write("".join(o))
        sys.stdout.flush()

    @staticmethod
    def _rename_prompt(fd: int, record: dict, repo: str, sel: int) -> str | None:
        value = record.get("name") or ""
        while True:
            IdeOptionsModal._draw(record, repo, sel, value)
            key = IdeNewModal._getkey(fd)
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
                key = IdeNewModal._getkey(fd)
                if key == "esc":
                    return 0
                if key in ("up", "k"):
                    sel = (sel - 1) % len(IdeOptionsModal.ACTIONS)
                    continue
                if key in ("down", "j"):
                    sel = (sel + 1) % len(IdeOptionsModal.ACTIONS)
                    continue
                if key.isdigit() and 1 <= int(key) <= len(IdeOptionsModal.ACTIONS):
                    sel = int(key) - 1
                    key = "enter"
                if key != "enter":
                    continue
                action = IdeOptionsModal.ACTIONS[sel][0]
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
