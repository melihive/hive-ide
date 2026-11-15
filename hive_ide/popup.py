"""Open a context-bearing popup from a tmux run-shell binding."""

from __future__ import annotations

import argparse
import subprocess
import sys

from .python_cmd import PythonCommand

#: Below this a popup has no room for a title, one row and a footer, so there is
#: nothing to be gained by shrinking further.
MIN_POPUP_ROWS = 6


def _client_size(tmux: list[str]) -> tuple[int | None, int | None]:
    """The client's cells, in ONE call. `(None, None)` when it cannot be read.

    Height matters as much as width now: the popup is sized from what the modal
    needs, and "what it needs" is only useful next to what the screen has.
    """
    result = subprocess.run(
        [*tmux, "display-message", "-p", "#{client_width} #{client_height}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, None
    parts = result.stdout.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None, None
    return int(parts[0]), int(parts[1])


def _content_rows(
    kind: str, args: argparse.Namespace, interior: int | None = None
) -> int | None:
    """Terminal rows this modal would occupy, measured from its own lines.

    None when the modal cannot say — an unknown kind, a store that will not read,
    anything — and the caller then keeps the old percentage. A sizing hint that
    guesses is worse than one that abstains: too small clips, too large is the
    full-screen box for a five-item menu that made this look badly designed.

    `interior` is the popup's usable width. Given it, the count includes lines
    the terminal will WRAP: counting logical lines under-reports the height
    whenever a line is wider than the popup, which the footer is on a narrow
    client, and the row that then leaves the screen is the top one.
    """
    if kind != "options":
        return None
    try:
        from .optionsmodal import IdeOptionsModal
        from .store import StateStore
        from . import modal_io

        store = StateStore(args.state_home, args.workspace_key)
        record = store.find_session(args.session_id)
        if record is None:
            return None
        # Rendered with room for everything, so this is the height the modal
        # WANTS — the same lines `_draw` writes, not an arithmetic beside them.
        lines, _rows = IdeOptionsModal._lines(
            record, store.workspace_key, 0, rows=10_000
        )
    except Exception:  # noqa: BLE001 — a sizing hint must never break the popup
        return None
    if interior is None:
        return len(lines)
    return modal_io.wrapped_rows(lines, interior)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hive_ide.popup")
    parser.add_argument(
        "--kind", choices=("agent", "options", "error", "card", "keys"), required=True
    )
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--workspace-key", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--tmux-socket")
    args = parser.parse_args(argv)
    modules = {
        "agent": "agentmodal",
        "options": "optionsmodal",
        "error": "errormodal",
        "card": "info",
        "keys": "info",
    }
    sizes = {
        "agent": ("56%", "48%"),
        "options": ("62%", "58%"),
        "error": ("56%", "48%"),
        "card": ("64%", "60%"),
        "keys": ("62%", "52%"),
    }
    module = modules[args.kind]
    command = PythonCommand.module_command(
        module,
        [
            *(
                ["--kind", args.kind]
                if args.kind in {"card", "keys"}
                else []
            ),
            "--state-home",
            args.state_home,
            "--workspace-key",
            args.workspace_key,
            "--session-id",
            args.session_id,
            *(
                ["--tmux-socket", args.tmux_socket]
                if args.tmux_socket and args.kind in {"agent", "options"}
                else []
            ),
        ],
        python=sys.executable,
    )
    width, height = sizes[args.kind]
    tmux = ["tmux"]
    if args.tmux_socket:
        tmux.extend(["-L", args.tmux_socket])
    width_cells, height_cells = _client_size(tmux)
    if width_cells is not None and width_cells < 120:
        width, height = "96%", "92%"
    # Size the HEIGHT from the content where the modal can say what it needs.
    # The percentages were the whole defect on a short screen: 92% of a handheld
    # minus tmux's popup border is less than this modal's 21-22 rows, and the
    # overflow leaves from the top. `+2` is that border; the clamp keeps it on
    # screen, and the floor keeps a tiny popup usable.
    # The popup's usable width, so the sizing hint can count wrapped rows: tmux
    # takes the percentage of the client, and the border costs two columns.
    interior = None
    if width_cells is not None and width.endswith("%"):
        interior = int(width_cells * int(width[:-1]) / 100) - 2
    needed = _content_rows(args.kind, args, interior)
    if needed is not None and height_cells:
        height = str(max(MIN_POPUP_ROWS, min(needed + 2, height_cells)))
    result = subprocess.run(
        [*tmux, "display-popup", "-E", "-w", width, "-h", height, command]
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
