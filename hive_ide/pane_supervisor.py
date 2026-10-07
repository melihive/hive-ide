"""Run a borrowed pane command on its tty, then exec the current role default."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import termios
import time

from .frame import Frame
from .pane_lease import (
    LeaseStore,
    PaneLeases,
    process_start,
    remain_on_exit_restore_args,
)
from .store import StateStore


def _foreground_child() -> None:
    os.setpgid(0, 0)
    if os.isatty(0):
        os.tcsetpgrp(0, os.getpid())
    signal.signal(signal.SIGTTOU, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def _shell(reason: str) -> None:
    print(f"\nPane lease: {reason}; opening a shell.", flush=True)
    shell = os.environ.get("SHELL") or "/bin/sh"
    try:
        os.execl(shell, shell)
    except OSError:
        os.execl("/bin/sh", "sh")


def supervise(lease_id: str, role: str, argv: list[str]) -> None:
    child = None
    requested = None

    def forward(signum, _frame):
        nonlocal requested
        requested = signum
        if child is not None:
            try:
                os.killpg(child.pid, signum)
            except ProcessLookupError:
                pass

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    signal.signal(signal.SIGTTOU, signal.SIG_IGN)
    store = StateStore(
        os.environ["HIVE_IDE_STATE_HOME"], os.environ["HIVE_IDE_WORKSPACE_KEY"]
    )
    frame = Frame(store, socket=os.environ["HIVE_IDE_TMUX_SOCKET"])
    leases = LeaseStore(store)
    session_id = os.environ["HIVE_IDE_SESSION_ID"]
    pane = os.environ["TMUX_PANE"]
    saved_tty = termios.tcgetattr(0) if os.isatty(0) else None
    lease = None
    try:
        with store.mutation_lock():
            lease = leases.read(session_id, role)
            if not lease or lease["lease_id"] != lease_id:
                raise RuntimeError("lease record is absent or has been replaced")
            lease["pid"] = os.getpid()
            lease["pid_start"] = process_start(os.getpid())
            if not lease.get("release_requested") and not requested:
                # No pipes: stdin/stdout/stderr remain the real pane tty. Give the
                # child foreground ownership before exec so interactive reads work.
                started = time.monotonic()
                child = subprocess.Popen(argv, preexec_fn=_foreground_child)
                lease["child_pid"] = child.pid
                lease["child_start"] = process_start(child.pid)
            leases.write(lease)
        if child:
            if requested:
                forward(requested, None)
            result = child.wait()
            if result and time.monotonic() - started < 1:
                # Report every fast failure, including silent failures, without
                # interposing a pipe/pty that changes the child's terminal semantics.
                print(
                    f"\nPane lease: command exited with status {result}: {argv[0]}",
                    flush=True,
                )
                time.sleep(1)
    except Exception as exc:
        print(
            f"\nPane lease: could not run {argv[0] if argv else 'command'}: {exc}",
            flush=True,
        )
        time.sleep(1)
    finally:
        if child:
            PaneLeases.kill_child({"child_pid": child.pid})
            child.wait()
        if saved_tty is not None:
            try:
                os.tcsetpgrp(0, os.getpgrp())
                termios.tcsetattr(0, termios.TCSANOW, saved_tty)
            except OSError:
                pass

    try:
        with store.mutation_lock():
            current = leases.read(session_id, role)
            if not current or current["lease_id"] != lease_id:
                raise RuntimeError("lease ownership changed before restoration")
            record = store.find_session(session_id)
            if not record:
                raise RuntimeError("session record no longer exists")
            # Re-read both record and settings at restore time, never an acquire snapshot.
            frame = Frame(store, socket=frame.socket)
            command = frame._plan_command(record)
            title = frame._plan_title(record)
            for args in (
                ["select-pane", "-T", title, "-t", pane],
                ["set-option", "-p", "-t", pane, "@hive_ide_title", title],
                remain_on_exit_restore_args(pane, current["previous"]),
            ):
                result = frame.tmux(args)
                if result.returncode:
                    raise RuntimeError(
                        result.stderr.strip() or "could not restore pane metadata"
                    )
            os.chdir(frame.safe_working_dir(record))
            os.environ.pop("HIVE_IDE_PANE_LEASE", None)
            leases.clear(current)
        # exec owns the existing pane; no caller-pane exception or tmux respawn.
        os.execl("/bin/sh", "sh", "-c", command)
    except Exception as exc:
        # A fallback shell no longer owns a lease. Best effort on storage failure;
        # an unreadable/unwritable record must not prevent the usable shell.
        try:
            with store.mutation_lock():
                current = leases.read(session_id, role)
                if current and current["lease_id"] == lease_id:
                    leases.clear(current)
        except Exception:
            pass
        os.environ.pop("HIVE_IDE_PANE_LEASE", None)
        _shell(f"could not restore {role}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease", required=True)
    parser.add_argument("--role", choices=("plan",), required=True)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    try:
        supervise(args.lease, args.role, argv)
    except Exception as exc:
        _shell(f"supervisor startup failed: {exc}")


if __name__ == "__main__":
    main()
