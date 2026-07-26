"""Open a context-bearing popup from a tmux run-shell binding."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hive_ide.popup")
    parser.add_argument("--kind", choices=("agent", "error"), required=True)
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--workspace-key", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--tmux-socket")
    args = parser.parse_args(argv)
    module = "agentmodal" if args.kind == "agent" else "errormodal"
    command = shlex.join(
        [
            sys.executable,
            "-I",
            "-m",
            f"hive_ide.{module}",
            "--state-home",
            args.state_home,
            "--workspace-key",
            args.workspace_key,
            "--session-id",
            args.session_id,
            *(
                ["--tmux-socket", args.tmux_socket]
                if args.tmux_socket and args.kind == "agent"
                else []
            ),
        ]
    )
    result = subprocess.run(
        ["tmux", "display-popup", "-E", "-w", "56%", "-h", "48%", command]
    )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
