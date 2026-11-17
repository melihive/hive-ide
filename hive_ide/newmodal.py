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

from . import modal_io
from datetime import datetime, timezone
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
    """One popup: name → driver+mode → optional conversation picker → create."""

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
    def _build_args(
        name: str, typ: str, *, adopt_reference: str | None = None
    ) -> list[str]:
        """Build the public CLI argv for one session."""
        args = ["create", f"--name={name}", f"--driver={typ}"]
        if adopt_reference:
            args.extend(["--adopt", f"--reference={adopt_reference}"])
        return args

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
    def _do_create(
        skill_dir: Path,
        name: str,
        typ: str,
        *,
        adopt_reference: str | None = None,
    ) -> tuple[bool, str]:
        """Create a session, build its window, and focus its main pane."""
        ok, out = IdeNewModal._cli(
            skill_dir,
            IdeNewModal._build_args(name, typ, adopt_reference=adopt_reference),
        )
        if not ok:
            return False, out or "create failed"
        try:
            session_id = json.loads(out)["id"]
        except (KeyError, TypeError, ValueError):
            return False, "create did not return a session id"
        repaired, repair_out = IdeNewModal._cli(
            skill_dir,
            [
                "repair",
                f"--session-id={session_id}",
                *(
                    [f"--tmux-socket={IdeNewModal._tmux_socket}"]
                    if IdeNewModal._tmux_socket
                    else []
                ),
            ],
        )
        if not repaired:
            return False, repair_out or "window build failed"
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
                ["tmux", "select-window", "-t", window_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["tmux", "select-pane", "-t", f"{window_id}.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return True, ""

    @staticmethod
    def _adoptable(skill_dir: Path, typ: str) -> tuple[bool, list[dict], str]:
        """Return unadopted local conversations for the selected driver."""
        ok, out = IdeNewModal._cli(
            skill_dir,
            ["adopt", f"--driver={typ}", "--dry-run"],
        )
        if not ok:
            return False, [], out or "adopt scan failed"
        try:
            data = json.loads(out)
        except ValueError:
            return False, [], "adopt scan returned unreadable data"
        items = data.get("created")
        if not isinstance(items, list):
            return False, [], "adopt scan returned no conversation list"
        return True, [item for item in items if isinstance(item, dict)], ""

    @staticmethod
    def _supports_adopt(typ: str) -> bool:
        return typ in {"claude", "codex"}

    @staticmethod
    def _mode_items(typ: str) -> list[tuple[str, str, str]]:
        items = [("new", "New session", "start a fresh agent or terminal")]
        if IdeNewModal._supports_adopt(typ):
            items.append(("adopt", "Adopt existing", "pick a local conversation"))
        return items

    @staticmethod
    def _filtered_conversations(st: dict) -> list[dict]:
        needle = " ".join(st.get("filter", "").lower().split())
        items = st.get("adopt_items") or []
        if not needle:
            return items
        return [
            item
            for item in items
            if needle
            in " ".join(
                str(item.get(key) or "")
                for key in ("name", "label", "title", "updated_at", "preview")
            ).lower()
        ]

    @staticmethod
    def _rel_time(iso: str | None) -> str:
        if not iso:
            return ""
        try:
            t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
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
            return {
                b"A": "up",
                b"B": "down",
                b"C": "right",
                b"D": "left",
            }.get(code, "other")
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
    def _field_tail(value: str, cells: int) -> str:
        """Delegates to `modal_io.field_tail` — the options modal needs it too.

        Kept as a name here because this stage's code reads better with it.
        """
        return modal_io.field_tail(value, cells)

    @staticmethod
    def _sel_summary(line: str) -> str:
        """What is CHOSEN on a highlighted row, for naming it in the footer.

        A row of checkboxes holds every option, so its plain text does not say
        which one is picked — printing all of them in the footer was as
        ambiguous as hiding the row. Take the `[x]` one, or the whole row when
        it is a plain highlighted item rather than a checkbox set.
        """
        text = modal_io.strip_ansi(line)
        if "[x]" in text:
            chosen = text.split("[x]", 1)[1]
            chosen = chosen.split("[")[0]
            return " ".join(chosen.split()).strip(" ▸·")
        return " ".join(text.split()).strip(" ▸·")

    @staticmethod
    def _footer(stage: str) -> str:
        if stage == "name":
            return "Enter → next · Esc → cancel"
        if stage == "adopt":
            return "type to filter · ↑/↓ or j/k · Enter → adopt · Backspace · Esc → cancel"
        if stage == "creating":
            return "working…"
        if stage == "error":
            return "Esc → close · any other key → back to retry"
        return "↑/↓ agent · ←/→ new/adopt · digits jump · Enter · Esc → cancel"

    @staticmethod
    def _draw(st: dict) -> None:
        C = IdeNewModal
        stage = st["stage"]
        o = [C.CLR, f"  {C.BOLD}New IDE session{C.RST}\n\n"]
        # Which chunks of `o` ARE the input fields, recorded as they are
        # written: (index into `o`, is this stage typing into it). Finding them
        # afterwards by searching the rendered text for "Name:"/"Filter:" read
        # the modal's own content as its structure — a conversation whose title
        # or preview contains "Filter:" was taken for the filter field, so the
        # list rows above it were pinned into the header and left the windowed
        # body. Two different selections then drew the same frame while Enter
        # adopted different conversations: 237 of the swept sizes with such a
        # title, 0 with ordinary ones. An index cannot be spoofed by content.
        fields: list[tuple[int, bool]] = []
        # Name — always on screen; caret only while editing it.
        caret = "▏" if stage == "name" else " "
        name_room = modal_io.cols_available() - len("  Name: ") - 1
        o.append(
            f"  Name: {C.NAME}{C._field_tail(st['name'], name_room)}"
            f"{caret}{C.RST}{C.EL}\n"
        )
        fields.append((len(o) - 1, stage == "name"))
        if stage == "name":
            hint = (f"{C.ERR}{st['err']}" if st["err"]
                    else f"{C.DIM}CAPS · spaces · ≤{StateIO.NAME_MAX}") + C.RST
            o.append(f"  {hint}{C.EL}\n")
        else:
            o.append(f"  {C.DIM}→ current workspace{C.RST}{C.EL}\n")
        o.append("\n")
        if stage == "type":
            C._render_list(o, "Driver:", [(lbl, note) for _, lbl, note in C.TYPES], st["ty"])
            typ = C.TYPES[st["ty"]][0]
            items = C._mode_items(typ)
            if st["mode_sel"] >= len(items):
                st["mode_sel"] = len(items) - 1
            o.append("\n")
            o.append(f"  Mode:{C.EL}\n")
            row = ["  "]
            for i, (_, label, _) in enumerate(items):
                mark = "x" if i == st["mode_sel"] else " "
                segment = f" [{mark}] {label} "
                if i == st["mode_sel"]:
                    row.append(f"{C.SEL}{segment}{C.RST}")
                else:
                    row.append(segment)
                if i != len(items) - 1:
                    row.append("  ")
            o.append("".join(row) + C.EL + "\n")
            o.append(f"  {C.DIM}{items[st['mode_sel']][2]}{C.RST}{C.EL}\n")
        elif stage == "adopt":
            typ = C.TYPES[st["ty"]][0]
            filtered = C._filtered_conversations(st)
            if filtered and st["adopt_sel"] >= len(filtered):
                st["adopt_sel"] = len(filtered) - 1
            o.append(f"  Adopt {typ} conversation{C.EL}\n")
            filter_room = modal_io.cols_available() - len("  Filter: ") - 1
            o.append(
                f"  Filter: {C.NAME}"
                f"{C._field_tail(str(st.get('filter', '')), filter_room)}"
                f"▏{C.RST}{C.EL}\n\n"
            )
            fields.append((len(o) - 1, True))
            if not filtered:
                o.append(f"  {C.DIM}No matching conversations.{C.RST}{C.EL}\n")
            else:
                # The window was a hardcoded 10 rows whatever the screen was — the
                # same mistake as sizing a popup by percentage, one layer down: on
                # a short screen ten adopt rows plus this stage's chrome overflows
                # and the title goes. Ask the terminal instead. CHROME is this
                # stage's own fixed lines (title, blank, name, hint, blank, the
                # "Adopt …" label, the filter line, blank, the range line, blank,
                # footer); one row is the floor, because a list of nothing is not
                # a kinder failure than a clipped one.
                CHROME = 11
                room = max(1, modal_io.rows_available() - CHROME)
                start = min(
                    max(0, st["adopt_sel"] - (room - 1)),
                    max(0, len(filtered) - room),
                )
                visible = filtered[start:start + room]
                rows = []
                for item in visible:
                    label = str(item.get("title") or item.get("label") or item.get("name") or "")
                    updated = item.get("updated_at")
                    when = C._rel_time(updated if isinstance(updated, str) else None)
                    preview = str(item.get("preview") or "").strip()
                    note_parts = [part for part in (when, preview) if part]
                    note = " · ".join(note_parts)
                    rows.append((label, note.strip(" ·")))
                C._render_list(o, "", rows, st["adopt_sel"] - start)
                if len(filtered) > len(visible):
                    o.append(
                        f"  {C.DIM}{start + 1}-{start + len(visible)} of {len(filtered)}{C.RST}{C.EL}\n"
                    )
        elif stage == "creating":
            verb = "Adopting" if st.get("mode") == "adopt" else "Creating"
            o.append(f"  {C.NAME}{verb} {st['name']}…{C.RST}{C.EL}\n")
        elif stage == "error":
            # Stay open on failure so the error is READABLE — the whole point of this
            # screen (a create that flashed an error and closed lost it before it could
            # even be seen). Show the tail of the captured output, where the cause is.
            o.append(f"  {C.ERR}✗ Couldn't create {st['name']}{C.RST}{C.EL}\n\n")
            for line in (st.get("error_msg") or "create failed").splitlines()[-8:]:
                o.append(f"  {C.DIM}{line[:76]}{C.RST}{C.EL}\n")
        # A backstop for every stage: the title says what this modal IS and the
        # footer how to leave it, so neither may be what scrolls away. The stages
        # build one buffer with embedded newlines, so split to display lines and
        # let `fit` window the middle.
        # o[0] is the clear-screen escape and o[1] IS the title line, which is
        # pinned as the header below — slicing from 1 printed it twice.
        body = "".join(o[2:]).split("\n")
        footer = [f"  {C.DIM}{C._footer(stage)}{C.RST}{C.EL}"]
        header = [f"  {C.BOLD}New IDE session{C.RST}", ""]
        dropped = 0
        while body and body[0].strip() == "":
            body.pop(0)
            dropped += 1
        # Any line the reader is TYPING INTO is pinned, not windowed. The window
        # broke both: at a 20x6 interior the name field scrolled away, and at
        # 80x8 the adopt filter did, so two different inputs drew identical
        # frames while the keystrokes still landed. An input you cannot see is
        # worse than a list you cannot see — the list has a marker saying so.
        #
        # Each field's body row is COUNTED, not recognised: the rows before a
        # registered chunk are the newlines written ahead of it, less the blank
        # leading rows just popped. Content plays no part, so no conversation
        # title can pose as a field.
        active_rows: set[int] = set()
        inputs: list[int] = []
        for chunk, live in fields:
            row = "".join(o[2:chunk]).count("\n") - dropped
            if 0 <= row < len(body):
                inputs.append(row)
                if live:
                    active_rows.add(row)
        inputs.sort()
        if inputs:
            # Everything up to and including the LAST input field moves to the
            # header: a prefix, so the order is unchanged. Pinning only a
            # leading run was not enough — the adopt filter sits after the
            # workspace hint, so it stayed in the body and kept scrolling off.
            cut = inputs[-1] + 1
            field_rows = set(inputs)
            # Carry each pinned line's ROLE with it; the fallbacks below used to
            # re-derive it by searching the line's text, which is the same
            # content-as-structure mistake one level down.
            marked = [
                (line, row in field_rows, row in active_rows)
                for row, line in enumerate(body[:cut])
                if line.strip()
            ]
            pinned = [line for line, _, _ in marked]
            del body[:cut]
            # On a screen too short even for that, keep the TITLE and the input
            # fields and let the rest go back to being windowed: the fields are
            # what the keystrokes land in. The popup floors at six rows, so this
            # really happens.
            # PHYSICAL rows, not line counts: fit measures in rows, and at a
            # 20x8 adopt interior the wrapped decoration ate the space before
            # the field, so this check passed and the filter was clipped anyway.
            cols = modal_io.cols_available()
            # MEASURE the footer. Assuming two rows was the same mistake one
            # level up: the adopt footer is three rows at a 30-cell interior,
            # so the guard did not fire and the field was clipped anyway.
            chrome_rows = modal_io.wrapped_rows(header + pinned + footer, cols)
            if chrome_rows > modal_io.rows_available():
                keep = [line for line, is_field, _ in marked if is_field]
                body[:0] = [
                    line for line, is_field, _ in marked if not is_field
                ]
                pinned = keep
                # Shed the decoration this modal owns before the field goes:
                # the blank spacer line, and the key hints down to the one that
                # says how to LEAVE. At a 20-cell interior the adopt footer
                # alone wraps to four rows and crowded the filter off a 6-row
                # screen. This selects from existing copy; it invents none.
                header[:] = [line for line in header if line.strip()]
                escape = C._footer(stage).rsplit("·", 1)[-1].strip()
                footer[:] = [f"  {C.DIM}{escape}{C.RST}{C.EL}"]
                # Smaller still: keep the field this stage is ACTIVELY typing
                # into. The adopt stage shows a name it is not editing; the
                # name stage has only the one. The invariant is that the field
                # taking the keystrokes is on screen — below this there is no
                # honest way to draw the modal at all.
                if (
                    modal_io.wrapped_rows(header + pinned + footer, cols)
                    > modal_io.rows_available()
                ):
                    live = [line for line, _, is_live in marked if is_live]
                    if live:
                        body[:0] = [
                            line
                            for line, is_field, is_live in marked
                            if is_field and not is_live
                        ]
                        pinned = live
            header.extend(pinned)
        while body and body[0].strip() == "":
            body.pop(0)
        while body and body[-1].strip() == "":
            body.pop()
        # Anchor the window on the row that is actually SELECTED, which is the
        # one carrying the highlight. Anchoring on the last line (as this first
        # did) keeps the trailing explanation on screen and can window away the
        # driver the reader is about to press Enter on — the selection must be
        # visible because the next keypress acts on it. Stages with no selection
        # (creating, error) have no highlight, and for them the end IS the news.
        selected_line = len(body) - 1
        also_selected = None
        # Whether Enter acts on a body row at all. The creating and error stages
        # anchor on their last line because that is the news, but nothing there
        # is selectable — so that row must NOT outrank the pinned name field
        # when the screen is too small for both.
        pin_selection = False
        if C.SEL:
            highlighted = [i for i, line in enumerate(body) if C.SEL in line]
            if highlighted:
                pin_selection = True
                # A stage can have TWO independent selections — the driver list
                # and the mode row — and Enter acts on both, so both have to be
                # visible. With only the first kept, "New session" and "Adopt
                # existing" drew identical screens at 10, 12 and 14 rows.
                selected_line = highlighted[0]
                if len(highlighted) > 1:
                    also_selected = highlighted[-1]
        # A pinned input field outranks the selection. The keystrokes are
        # landing in the field right now, and a selection the window cuts is
        # already named in the footer below — a cut field has no such notice.
        # Letting the selected row win dropped the adopt filter at 40x4, 74x4,
        # 40x5 and 40x8. The options modal has no conflict here: its rename
        # field lives in the FOOTER, which outranks both.
        if inputs:
            pin_selection = False
        lines = modal_io.fit(
            header, body, footer, selected=selected_line,
            also_selected=also_selected, pin_selection=pin_selection,
            # A stage footer is pure key hints at this point, so a screen too
            # small may spend it on the selected row instead. It stops being a
            # hint the moment it carries a cut summary — see the second fit.
            footer_hints=1,
        )
        # EVERY selection that was cut gets named in the footer, not just the
        # second one. The driver can be windowed out too — at narrow widths all
        # four drivers drew identical frames while Enter created the selected
        # one. Whatever decides what Enter does must be readable somewhere, and
        # the footer is the one place that cannot be clipped.
        cut_indexes = [
            index
            for index in (selected_line, also_selected)
            if index is not None and 0 <= index < len(body)
            and body[index] not in lines
        ]
        if cut_indexes:
            summaries = [
                summary
                for summary in (C._sel_summary(body[i]) for i in cut_indexes)
                if summary
            ]
            cut = " · ".join(dict.fromkeys(summaries))
            if cut:
                # Build on the footer AS IT STANDS. Re-reading `_footer(stage)`
                # put the full key hints back over a footer the shedding had
                # just compacted, which ate the body budget — at a 38x6
                # interior all four drivers then drew identical frames.
                standing = (
                    modal_io.strip_ansi(footer[0]).strip()
                    if footer
                    else C._footer(stage)
                )
                footer = [f"  {C.DIM}{cut} · {standing}{C.RST}{C.EL}"]
                # The summary is only useful if the footer carrying it SURVIVES.
                # At a 14-cell interior the title alone takes two rows, and the
                # full line needed three, so it was dropped and every driver
                # drew the same frame again. Trim to the rows actually left —
                # from the right, because the summary leads.
                cols_now = modal_io.cols_available()
                spare = modal_io.rows_available() - modal_io.wrapped_rows(
                    header, cols_now
                )
                if spare >= 1 and modal_io.wrapped_rows(footer, cols_now) > spare:
                    footer = [
                        f"  {C.DIM}"
                        f"{modal_io.truncate_to_rows(f'{cut} · {standing}', spare, cols_now - 2)}"
                        f"{C.RST}{C.EL}"
                    ]
                # No `footer_hints` here, deliberately. This footer now NAMES a
                # selection the window cut, so it is the only thing on screen
                # saying what Enter will do — shedding it would restore the very
                # defect it was added to fix.
                lines = modal_io.fit(
                    header, body, footer, selected=selected_line,
                    also_selected=also_selected, pin_selection=pin_selection,
                )

        sys.stdout.write(C.CLR + "\n".join(lines))
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
        st = {
            "name": "",
            "err": "",
            "stage": "name",
            "ty": 0,
            "mode": "new",
            "mode_sel": 0,
            "filter": "",
            "adopt_items": [],
            "adopt_sel": 0,
            "error_back": "type",
            "error_msg": "",
        }
        try:
            while True:
                C._draw(st)
                k = C._getkey(fd)
                if k == "esc":
                    return 0                  # cancel/close from any stage
                if st["stage"] == "error":
                    st["stage"] = st.get("error_back", "type")  # any non-Esc key → retry
                    continue
                if st["stage"] == "adopt":
                    filtered = C._filtered_conversations(st)
                    if k in ("up", "k") and filtered:
                        st["adopt_sel"] = (st["adopt_sel"] - 1) % len(filtered)
                        continue
                    if k in ("down", "j") and filtered:
                        st["adopt_sel"] = (st["adopt_sel"] + 1) % len(filtered)
                        continue
                    if k == "bs":
                        st["filter"] = st["filter"][:-1]
                        st["adopt_sel"] = 0
                        continue
                    if k == "enter":
                        if not filtered:
                            continue
                        chosen = filtered[st["adopt_sel"]]
                        reference = chosen.get("reference")
                        if not isinstance(reference, str) or not reference:
                            st["stage"], st["error_back"], st["error_msg"] = (
                                "error",
                                "adopt",
                                "selected conversation has no reference",
                            )
                            continue
                        st["stage"] = "creating"
                        C._draw(st)
                        ok, msg = C._do_create(
                            skill_dir,
                            st["name"],
                            C.TYPES[st["ty"]][0],
                            adopt_reference=reference,
                        )
                        if ok:
                            return 0
                        st["stage"], st["error_back"], st["error_msg"] = "error", "adopt", msg
                        continue
                    if len(k) == 1 and k.isprintable():
                        st["filter"] += k
                        st["adopt_sel"] = 0
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
                    if st["mode_sel"] >= len(C._mode_items(C.TYPES[st["ty"]][0])):
                        st["mode_sel"] = 0
                    continue
                if k in ("down", "j"):
                    st["ty"] = (st["ty"] + 1) % n
                    if st["mode_sel"] >= len(C._mode_items(C.TYPES[st["ty"]][0])):
                        st["mode_sel"] = 0
                    continue
                mode_items = C._mode_items(C.TYPES[st["ty"]][0])
                if k in ("left", "h") and mode_items:
                    st["mode_sel"] = (st["mode_sel"] - 1) % len(mode_items)
                    continue
                if k in ("right", "l") and mode_items:
                    st["mode_sel"] = (st["mode_sel"] + 1) % len(mode_items)
                    continue
                if k.isdigit() and 1 <= int(k) <= n:
                    st["ty"] = int(k) - 1
                    if st["mode_sel"] >= len(C._mode_items(C.TYPES[st["ty"]][0])):
                        st["mode_sel"] = 0
                elif k != "enter":
                    continue
                st["mode"] = C._mode_items(C.TYPES[st["ty"]][0])[st["mode_sel"]][0]
                if st["mode"] == "adopt":
                    st["stage"] = "creating"
                    C._draw(st)
                    ok, items, msg = C._adoptable(skill_dir, C.TYPES[st["ty"]][0])
                    if not ok:
                        st["stage"], st["error_back"], st["error_msg"] = "error", "type", msg
                        continue
                    if not items:
                        st["stage"], st["error_back"], st["error_msg"] = (
                            "error",
                            "type",
                            "No unadopted conversations were found for this workspace.",
                        )
                        continue
                    st["stage"] = "adopt"
                    st["adopt_items"] = items
                    st["filter"] = ""
                    st["adopt_sel"] = 0
                    continue
                st["stage"] = "creating"
                C._draw(st)
                ok, msg = C._do_create(skill_dir, st["name"], C.TYPES[st["ty"]][0])
                if ok:
                    return 0
                st["stage"], st["error_back"], st["error_msg"] = "error", "type", msg
        finally:
            sys.stdout.write("\x1b[?25h")     # restore the cursor
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write(C.CLR)
            sys.stdout.flush()
        return 0


if __name__ == "__main__":
    sys.exit(IdeNewModal.main(sys.argv))
