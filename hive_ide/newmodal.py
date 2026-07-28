#!/usr/bin/env python3
"""Guided 'new IDE session' modal — arrow-select, ESC-cancelable — in a tmux popup.

Two steps in one screen (↑/↓ or j/k to pick, Enter to advance, digits jump):
  1. Name — type it (Backspace edits, live-capped at 14, upper-cased); Enter validates
     (CAPS/spaces/≤14, not already taken). A bad name shows an inline error and cannot
     be confirmed — never silently accepted.
  2. Driver — Claude, Codex, Antigravity, or a plain terminal.

ESC cancels the whole modal at any point. The driver has a sensible default.

Stdlib only (like `ide_sidebar`/`ide_nav`); raw cbreak input, so it must not boot the
foreground runtime during input. The final action invokes the public package CLI.

usage: python -m hive_ide.newmodal --state-home <state> --workspace-key <repo>
"""
from __future__ import annotations

import os
import argparse
import json
import select
import subprocess
import sys
from pathlib import Path

from .python_cmd import PythonCommand
from .state_compat import StateIO

try:                       # POSIX only (tmux is Unix); keep importable elsewhere
    import termios
    import tty
except ImportError:
    termios = None
    tty = None


class IdeNewModal:
    """One popup: name → driver → create. ESC cancels."""

    # (kind, label, hint) — kind maps to `ide new` flags: claude=default,
    # codex=--agent=codex, terminal=--terminal.
    TYPES = [("claude", "🔆 claude", "agent"),
             ("codex", "🌀 codex", "agent"),
             ("antigravity", "🔷 antigravity", "agent"),
             ("term", "💻 terminal", "plain shell")]

    CLR = "\x1b[2J\x1b[H"                       # clear screen + home
    EL = "\x1b[K"                              # erase to end of line
    SEL = "\x1b[48;5;51m\x1b[38;5;16m"         # teal bg + dark fg — the picked row
    DIM = "\x1b[38;5;244m"
    BOLD = "\x1b[1m"
    # The name you are typing — bold orange, the SAME 214 the sidebar gives the current
    # session (`IdeSidebar.CUR_FG`). One colour for "this session's name" across both
    # surfaces: what you type here is what lights up in the list a moment later.
    NAME = "\x1b[1;38;5;214m"
    ERR = "\x1b[38;5;203m"                     # soft red
    RST = "\x1b[0m"
    _workspace_key = ""
    _tmux_socket = ""

    # ---- create (testable, no tty) ----

    @staticmethod
    def _build_args(name: str, typ: str) -> list[str]:
        """Build the public CLI argv for one session."""
        return ["create", f"--name={name}", f"--driver={typ}"]

    @staticmethod
    def _ask_tmux(fmt: str) -> str:
        """Resolve ONE tmux format by asking the server, from inside the popup.

        `display-popup` does NOT format-expand its shell-command (verified on tmux 3.7b:
        a command given `'#{window_name}'` receives the literal seven-character string
        `#{window_name}`). Only option arguments like `-d` are expanded. So a modal can
        never be TOLD its window — it must ASK. The popup inherits `$TMUX`, so a bare
        `tmux display-message -p` targets the right server and the client's active
        window, which is the window the key was pressed in.
        """
        try:
            out = subprocess.run(["tmux", "display-message", "-p", fmt],
                                 capture_output=True, text=True, timeout=5)
            return out.stdout.strip() if out.returncode == 0 else ""
        except Exception:
            return ""

    @staticmethod
    def _resolve(value: str | None, fmt: str) -> str:
        """A caller-supplied value, unless it is an UNEXPANDED tmux format — then ask tmux.

        Keeps the args positional-compatible (tests and the CLI can still pass real
        values) while making a stale binding that still passes `'#{window_name}'`
        self-healing rather than fatal.
        """
        if value and not (value.startswith("#{") and value.endswith("}")):
            return value
        return IdeNewModal._ask_tmux(fmt)

    @staticmethod
    def _bail(message: str, detail: str = "") -> int:
        """Show WHY the modal cannot continue, then wait for a key.

        A modal must never vanish without saying anything: `display-popup -E` closes the
        instant the command exits, so a bare `return 0` renders as an unexplained flash.
        That is precisely what hid the unexpanded-format bug — the modal knew it had no
        record and said nothing. Any early exit goes through here.
        """
        M = IdeNewModal
        sys.stdout.write(f"{M.CLR}  {M.BOLD}Cannot open{M.RST}\n\n  {message}{M.EL}\n")
        if detail:
            sys.stdout.write(f"  {M.DIM}{detail}{M.RST}{M.EL}\n")
        sys.stdout.write(f"\n  {M.DIM}(press any key to close){M.RST}{M.EL}")
        sys.stdout.flush()
        try:
            if termios and tty and sys.stdin.isatty():
                fd = sys.stdin.fileno()
                saved = termios.tcgetattr(fd)
                tty.setcbreak(fd)
                try:
                    os.read(fd, 1)
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:
            pass
        return 1

    @staticmethod
    def _cli(skill_dir: Path, args: list[str]) -> tuple[bool, str]:
        """Run a public CLI mutation and capture output for the modal."""
        try:
            command = PythonCommand.cli_argv(
                [
                    "--state-home",
                    str(skill_dir),
                    "--workspace-key",
                    IdeNewModal._workspace_key,
                    *args,
                ],
                python=sys.executable,
            )
            p = subprocess.run(
                command,
                cwd=IdeNewModal._workspace_key,
                capture_output=True,
                text=True,
            )
            return p.returncode == 0, ((p.stdout or "") + (p.stderr or "")).strip()
        except OSError as exc:
            return False, str(exc)

    @staticmethod
    def _do_create(skill_dir: Path, name: str, typ: str) -> tuple[bool, str]:
        """Create a session, build its window, and focus its main pane."""
        ok, out = IdeNewModal._cli(skill_dir, IdeNewModal._build_args(name, typ))
        if not ok:
            return False, out or "create failed"
        try:
            session_id = json.loads(out)["id"]
        except (KeyError, TypeError, ValueError):
            return False, "create did not return a session id"
        ensured, ensure_out = IdeNewModal._cli(
            skill_dir,
            [
                "ensure",
                f"--session-id={session_id}",
                *(
                    [f"--tmux-socket={IdeNewModal._tmux_socket}"]
                    if IdeNewModal._tmux_socket
                    else []
                ),
            ],
        )
        if not ensured:
            return False, ensure_out or "window build failed"
        windows = subprocess.run(
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
        window_id = next(
            (
                line.split("\t", 1)[0]
                for line in windows.stdout.splitlines()
                if line.endswith(f"\t{session_id}")
            ),
            None,
        )
        if window_id:
            subprocess.run(
                ["tmux", "select-pane", "-t", f"{window_id}.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True, ""

    @staticmethod
    def _validate(skill_dir: Path, repo: str, raw: str) -> tuple[str | None, str]:
        """`(name, error)` — name is None when invalid; error is the inline message."""
        cand = " ".join(raw.upper().split())
        if not cand:
            return None, "name can't be empty"
        if not StateIO.valid_name(cand):
            return None, f"{cand!r} invalid — letters/digits/spaces, ≤{StateIO.NAME_MAX}"
        if StateIO.read(skill_dir, repo, cand) is not None:
            return None, f"{cand!r} already exists"
        return cand, ""

    # ---- interactive loop (tty) ----

    # A bare ESC is told apart from an arrow escape sequence (ESC [ A/B) by peeking for
    # the continuation. It must be generous: over SSH/Termius the `[A` tail arrives a
    # beat after the ESC, and too short a peek reads the arrow as a cancel — which is the
    # bug that closed the modal mid-selection. `j`/`k` and the digit keys below need no
    # escape at all, so navigation never depends on this timing.
    ESC_PEEK_SECONDS = 0.4

    @staticmethod
    def _getkey(fd: int) -> str:
        """One logical key: up/down/enter/esc/bs or a single char.

        Reads the RAW fd with `os.read`, NOT buffered `sys.stdin`: `sys.stdin.read(1)`
        pulls the whole `ESC [ B` arrow sequence into Python's own buffer, so `select()`
        on the fd sees nothing waiting and the ESC reads as a bare cancel — which is what
        killed the modal on every arrow press. `os.read` is unbuffered, so `select` sees
        the continuation and the arrow resolves."""
        data = os.read(fd, 1)
        if not data or data == b"\x03":       # EOF / Ctrl-C → cancel
            return "esc"
        if data in (b"\r", b"\n"):
            return "enter"
        if data in (b"\x7f", b"\x08"):
            return "bs"
        if data == b"\x1b":
            r, _, _ = select.select([fd], [], [], IdeNewModal.ESC_PEEK_SECONDS)
            if not r:
                return "esc"                  # nothing followed → a real Escape → cancel
            # Arrows arrive as CSI (`ESC [ A`) or SS3 (`ESC O A`); read the tail raw. Handle
            # the tail arriving split (intro now, final byte a beat behind over the network).
            rest = os.read(fd, 8)
            if not rest or rest[:1] not in (b"[", b"O"):
                return "esc"
            code = rest[1:2]
            if not code:
                r, _, _ = select.select([fd], [], [], IdeNewModal.ESC_PEEK_SECONDS)
                code = os.read(fd, 1) if r else b""
            return {b"A": "up", b"B": "down"}.get(code, "other")
        try:
            return data.decode("utf-8", "ignore") or "other"
        except ValueError:
            return "other"

    @staticmethod
    def _render_list(o: list[str], title: str, items: list[tuple[str, str]], sel: int) -> None:
        """Append an arrow-select list (label + dim note) to the output buffer."""
        C = IdeNewModal
        if title:
            o.append(f"  {title}{C.EL}\n")
        for i, (label, note) in enumerate(items):
            arrow = "▸" if i == sel else " "
            if i == sel:
                tail = f"  {note}" if note else ""
                o.append(f"  {C.SEL} {arrow} {label}{tail} {C.RST}{C.EL}\n")
            else:
                tail = f"  {C.DIM}{note}{C.RST}" if note else ""
                o.append(f"   {arrow} {label}{tail}{C.EL}\n")

    @staticmethod
    def _footer(stage: str) -> str:
        if stage == "name":
            return "Enter → next · Esc → cancel"
        if stage == "creating":
            return "working…"
        if stage == "error":
            return "Esc → close · any other key → back to retry"
        return "↑/↓ or j/k · digits jump · Enter → create · Esc → cancel"

    @staticmethod
    def _draw(st: dict) -> None:
        C = IdeNewModal
        stage = st["stage"]
        o = [C.CLR, f"  {C.BOLD}New IDE session{C.RST}\n\n"]
        # Name — always on screen; caret only while editing it.
        caret = "▏" if stage == "name" else " "
        o.append(f"  Name: {C.NAME}{st['name']}{caret}{C.RST}{C.EL}\n")
        if stage == "name":
            hint = (f"{C.ERR}{st['err']}" if st["err"]
                    else f"{C.DIM}CAPS · spaces · ≤{StateIO.NAME_MAX}") + C.RST
            o.append(f"  {hint}{C.EL}\n")
        else:
            o.append(f"  {C.DIM}→ current workspace{C.RST}{C.EL}\n")
        o.append("\n")
        if stage == "type":
            C._render_list(o, "Driver:", [(lbl, note) for _, lbl, note in C.TYPES], st["ty"])
        elif stage == "creating":
            o.append(f"  {C.NAME}Creating {st['name']}…{C.RST}{C.EL}\n")
        elif stage == "error":
            # Stay open on failure so the error is READABLE — the whole point of this
            # screen (a create that flashed an error and closed lost it before it could
            # even be seen). Show the tail of the captured output, where the cause is.
            o.append(f"  {C.ERR}✗ Couldn't create {st['name']}{C.RST}{C.EL}\n\n")
            for line in (st.get("error_msg") or "create failed").splitlines()[-8:]:
                o.append(f"  {C.DIM}{line[:76]}{C.RST}{C.EL}\n")
        o.append(f"\n  {C.DIM}{C._footer(stage)}{C.RST}{C.EL}")
        sys.stdout.write("".join(o))
        sys.stdout.flush()

    @staticmethod
    def main(argv: list[str]) -> int:
        raw = argv[1:]
        if "--state-home" in raw:
            parser = argparse.ArgumentParser(prog="python -m hive_ide.newmodal")
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--tmux-socket", required=True)
            parsed = parser.parse_args(raw)
            skill_dir = Path(parsed.state_home)
            repo = parsed.workspace_key
            IdeNewModal._tmux_socket = parsed.tmux_socket
        else:
            if len(argv) < 2:
                sys.stderr.write("usage: ide_newmodal.py <state_home> [workspace]\n")
                return 2
            skill_dir = Path(argv[1])
            repo = IdeNewModal._resolve(argv[2] if len(argv) > 2 else None, "#{session_name}")
        # `repo` is OPTIONAL and self-resolving: the binding no longer passes a tmux
        # format, because display-popup would hand it over unexpanded (see `_resolve`).
        if not repo:
            return IdeNewModal._bail("Could not determine which repo this window belongs to.",
                                     "tmux did not answer #{session_name} — is this running inside tmux?")
        IdeNewModal._workspace_key = repo
        if not (termios and tty and sys.stdin.isatty()):
            return 0                          # interactive-only; no tty → nothing to do
        C = IdeNewModal
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?25l")         # hide the real cursor — we draw our own `▏`
        sys.stdout.flush()
        st = {"name": "", "err": "", "stage": "name", "ty": 0, "error_msg": ""}
        try:
            while True:
                C._draw(st)
                k = C._getkey(fd)
                if k == "esc":
                    return 0                  # cancel/close from any stage
                if st["stage"] == "error":
                    st["stage"] = "type"  # any non-Esc key → back to retry (name kept)
                    continue
                if st["stage"] == "name":
                    if k == "enter":
                        valid, st["err"] = C._validate(skill_dir, repo, st["name"])
                        if valid is not None:
                            st["name"], st["stage"], st["err"] = valid, "type", ""
                    elif k == "bs":
                        st["name"], st["err"] = st["name"][:-1], ""
                    elif len(k) == 1 and (k.isalnum() or k == " ") and len(st["name"]) < StateIO.NAME_MAX:
                        st["name"], st["err"] = st["name"] + k.upper(), ""
                    continue
                n = len(C.TYPES)
                if k in ("up", "k"):
                    st["ty"] = (st["ty"] - 1) % n
                    continue
                if k in ("down", "j"):
                    st["ty"] = (st["ty"] + 1) % n
                    continue
                if k.isdigit() and 1 <= int(k) <= n:
                    st["ty"] = int(k) - 1
                elif k != "enter":
                    continue
                # Flow complete → create INSIDE the modal (output captured), so a failure
                # is shown here and stays put; only success or Esc closes the popup.
                st["stage"] = "creating"
                C._draw(st)
                ok, msg = C._do_create(skill_dir, st["name"], C.TYPES[st["ty"]][0])
                if ok:
                    return 0                   # success → close (finally restores the tty)
                st["stage"], st["error_msg"] = "error", msg
        finally:
            sys.stdout.write("\x1b[?25h")     # restore the cursor
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write(C.CLR)
            sys.stdout.flush()
        return 0


if __name__ == "__main__":
    sys.exit(IdeNewModal.main(sys.argv))
