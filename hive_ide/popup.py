"""Open a context-bearing popup from a tmux run-shell binding."""

from __future__ import annotations

import argparse
import subprocess
import sys

from .python_cmd import PythonCommand


def _client_width(tmux: list[str]) -> int | None:
    result = subprocess.run(
        [*tmux, "display-message", "-p", "#{client_width}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    return int(raw) if raw.isdigit() else None


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
    if (width_cells := _client_width(tmux)) is not None and width_cells < 120:
        width, height = "96%", "92%"
    result = subprocess.run(
        [*tmux, "display-popup", "-E", "-w", width, "-h", height, command]
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
