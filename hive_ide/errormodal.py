"""Render the latest normalized frame or session error."""

from __future__ import annotations

import argparse
import sys

from .store import StateStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hive_ide.errormodal")
    parser.add_argument("--state-home", required=True)
    parser.add_argument("--workspace-key", required=True)
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)
    store = StateStore(args.state_home, args.workspace_key)
    error = (
        store.read("errors", args.session_id)
        if args.session_id
        else store.read_path(store.frame_error_path())
    )
    if error is None:
        print("\n  No current error.")
    else:
        retry = "retryable" if error.get("retryable") else "not retryable"
        print(f"\n  {error.get('summary') or 'Unknown error'}")
        print(f"  {error.get('component') or 'unknown component'} · {retry}")
        if error.get("detail"):
            print(f"\n  {error['detail']}")
        if error.get("recovery"):
            print(f"\n  Recovery: {error['recovery']}")
    if sys.stdin.isatty():
        print("\n  (press Enter to close)", end="", flush=True)
        try:
            sys.stdin.readline()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
