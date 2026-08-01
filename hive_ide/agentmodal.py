#!/usr/bin/env python3
"""Guided 'change agent' modal — arrow-select, ESC-cancelable — in a tmux popup.

The visual twin of `ide_newmodal`'s driver step, but for an existing session: pick the
occupant the middle pane should run with ↑/↓ (or j/k, or 1-4), Enter to switch, and
ESC to cancel. The currently-running occupant is
marked and pre-selected, so Enter-on-it is a deliberate no-op rather than a surprise
switch.

It deliberately reuses `IdeNewModal`'s primitives — the SSH-timing-safe `_getkey`, the
colour constants, and the `TYPES` list — so the two modals look identical and the one
piece of subtle input logic (the arrow-vs-Escape peek) lives in exactly one place.

Stdlib only (like `ide_newmodal`); raw cbreak input, so it must not boot the foreground
runtime. The final action invokes the public package CLI.

usage: python -m hive_ide.agentmodal --state-home <state> --workspace-key <repo> --session-id <id>
"""
from __future__ import annotations

import subprocess
import argparse
import sys
from pathlib import Path

from .newmodal import IdeNewModal
from .state_compat import StateIO

try:                       # POSIX only (tmux is Unix); keep importable elsewhere
    import termios
    import tty
except ImportError:
    termios = None
    tty = None


class IdeAgentModal:
    """One popup: arrow-select the session's occupant → switch. ESC cancels."""

    DEFAULT_AGENT = "claude"   # mirrors _IdeCore.DEFAULT_AGENT; the modal must not import it

    # ---- switch (testable, no tty) ----

    @staticmethod
    def _switch(
        skill_dir: Path, session_id: str, kind: str, *, handoff: bool = False
    ) -> tuple[bool, str]:
        """Shell the switch through the launcher — the same path `pick_agent` uses, so the
        pane respawn/resume logic stays in one place (`switch_agent`)."""
        ok, out = IdeNewModal._cli(
            skill_dir,
            [
                "switch-driver",
                f"--session-id={session_id}",
                f"--driver={kind}",
                *(["--handoff"] if handoff else []),
                *(
                    [f"--tmux-socket={IdeNewModal._tmux_socket}"]
                    if IdeNewModal._tmux_socket
                    else []
                ),
            ],
        )
        return ok, out

    @staticmethod
    def _context(skill_dir: Path, argv: list[str]) -> tuple[str, str, str] | None:
        """`(repo, session_id, display_name)` for this popup, else None.

        Resolution order is deliberate — IDENTITY first, display name only as a fallback:

        1. Public session-id and workspace-key options stamped by the frame at build time.
           Immutable: `ide rename`, tmux `automatic-rename`, and a hand rename all leave
           them untouched, so this keeps working when the window is called something else.
        2. `#{session_name}` / `#{window_name}` — for a frame built before the tags existed
           (a server that has not been reopened since). Correct today, but goes stale on the
           first rename, which is exactly why it is second.
        """
        M = IdeNewModal
        sid = M._ask_tmux("#{@hive_ide_session_id}")
        repo = M._ask_tmux("#{@hive_ide_workspace_key}")
        if sid and repo:
            hit = StateIO.find_by_id(skill_dir, repo, sid)
            if hit:
                return hit[0], hit[2]["id"], hit[1]
            # A stamped id that resolves to nothing is a REAL error (deleted record, wrong
            # checkout's registry) — not a reason to quietly fall back to a name that might
            # accidentally match a different session.
            return None
        repo = M._resolve(argv[2] if len(argv) > 2 else None, "#{session_name}")
        window = M._resolve(argv[3] if len(argv) > 3 else None, "#{window_name}")
        if not repo or not window:
            return None
        hit = StateIO.find_by_identity(skill_dir, repo, window)
        return (hit[0], hit[2]["id"], hit[1]) if hit else None

    @staticmethod
    def _active(skill_dir: Path, repo: str, session_id: str) -> str | None:
        """The occupant the session runs now — None if there is no such record (the modal
        then has nothing to change and bails, rather than inventing a default)."""
        found = StateIO.find_by_id(skill_dir, repo, session_id)
        if found is None:
            return None
        rec = found[2]
        return (
            (rec.get("driver") or {}).get("id")
            or (rec.get("agents") or {}).get("active")
            or IdeAgentModal.DEFAULT_AGENT
        )

    # ---- interactive loop (tty) ----

    @staticmethod
    def _draw(window: str, repo: str, active: str, sel: int, handoff: bool) -> None:
        M = IdeNewModal
        o = [M.CLR, f"  {M.BOLD}Change agent{M.RST}\n"]
        # Subtitle: which session, and what it runs now — the context a bare list lacks.
        o.append(f"  {M.DIM}{window} ({repo}) — running {active}{M.RST}{M.EL}\n\n")
        for i, (kind, label, note) in enumerate(M.TYPES):
            picked = i == sel
            here = kind == active
            arrow = "▸" if picked else " "
            # The active occupant carries a trailing "· current" so it reads even when it is
            # NOT the highlighted row — the highlight says "will switch here", the tag says
            # "you are here", and the two must not be confused.
            tag = f"  {M.NAME}· current{M.RST}" if here else ""
            if picked:
                o.append(f"  {M.SEL} {arrow} {label}  {note} {M.RST}{tag}{M.EL}\n")
            else:
                o.append(f"   {arrow} {label}  {M.DIM}{note}{M.RST}{tag}{M.EL}\n")
        quick = f"{M.SEL} quick switch {M.RST}" if not handoff else f"{M.DIM}quick switch{M.RST}"
        package = f"{M.SEL} handoff package {M.RST}" if handoff else f"{M.DIM}handoff package{M.RST}"
        o.append(
            f"\n  mode: {quick}  {package}"
            f"\n  {M.DIM}↑/↓ or j/k · ←/→ handoff · 1-4 or Enter → switch · Esc → cancel{M.RST}{M.EL}"
        )
        sys.stdout.write("".join(o))
        sys.stdout.flush()

    @staticmethod
    def main(argv: list[str]) -> int:
        raw = argv[1:]
        if "--state-home" in raw:
            parser = argparse.ArgumentParser(prog="python -m hive_ide.agentmodal")
            parser.add_argument("--state-home", required=True)
            parser.add_argument("--workspace-key", required=True)
            parser.add_argument("--session-id", required=True)
            parser.add_argument("--tmux-socket")
            parsed = parser.parse_args(raw)
            skill_dir = Path(parsed.state_home)
            IdeNewModal._tmux_socket = parsed.tmux_socket or ""
            found = StateIO.find_by_id(skill_dir, parsed.workspace_key, parsed.session_id)
            ctx = (found[0], found[2]["id"], found[1]) if found else None
        else:
            if len(argv) < 2:
                sys.stderr.write("usage: ide_agentmodal.py <state_home> [workspace] [window]\n")
                return 2
            skill_dir = Path(argv[1])
            ctx = IdeAgentModal._context(skill_dir, argv)
        # `repo`/`window` are OPTIONAL and self-resolving. They used to be passed as tmux
        # formats from the key binding — but `display-popup` does not expand formats in its
        # command, so the modal received the literal `#{session_name}` / `#{window_name}`,
        # found no record, and closed instantly. That was the "flashes and closes" bug.
        if ctx is None:
            return IdeNewModal._bail(
                "Could not determine which ide session this window is.",
                "no @hive_ide_session_id on the window and no resolvable name — "
                "run `hive-ide repair --all` to heal the frame.")
        repo, session_id, window = ctx
        IdeNewModal._workspace_key = repo
        if not (termios and tty and sys.stdin.isatty()):
            return 0                          # interactive-only; no tty → nothing to do
        active = IdeAgentModal._active(skill_dir, repo, session_id)
        if active is None:
            return IdeNewModal._bail(
                f"No ide session record for id '{session_id}'.",
                f"looked in the protocol session registry for workspace {repo}")
        types = IdeNewModal.TYPES
        # Pre-select the active occupant, so opening the modal and pressing Enter keeps
        # things as they are — the safe default for a picker over an existing session.
        sel = next((i for i, (k, *_ ) in enumerate(types) if k == active), 0)
        handoff = False
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?25l")         # hide the real cursor
        sys.stdout.flush()
        result = None
        try:
            while True:
                IdeAgentModal._draw(window, repo, active, sel, handoff)
                k = IdeNewModal._getkey(fd)
                if k == "esc":
                    return 0
                if k in ("up", "k"):
                    sel = (sel - 1) % len(types)
                elif k in ("down", "j"):
                    sel = (sel + 1) % len(types)
                elif k in ("left", "right"):
                    handoff = not handoff
                elif k.isdigit() and 1 <= int(k) <= len(types):
                    result = types[int(k) - 1][0]
                    break
                elif k == "enter":
                    result = types[sel][0]
                    break
        finally:
            sys.stdout.write("\x1b[?25h")     # restore the cursor
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write(IdeNewModal.CLR)
            sys.stdout.flush()
        if result is None or result == active:
            return 0                          # cancelled, or picked what's already running
        ok, detail = IdeAgentModal._switch(
            skill_dir, session_id, result, handoff=handoff
        )
        if ok:
            return 0
        return IdeNewModal._bail(
            "Could not change the agent for this session.",
            detail or "switch-driver failed without output.",
        )


if __name__ == "__main__":
    sys.exit(IdeAgentModal.main(sys.argv))
