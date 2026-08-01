from __future__ import annotations

import getpass
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path


PYTEST_TMP_ROOT = Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"
LEAK_PATTERNS = (
    "hive_ide.sidebar",
    "tmux -L hive-ide-",
    "codex resume -C",
)


def _pytest_tmp_processes() -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["pgrep", "-af", str(PYTEST_TMP_ROOT)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    rows: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        if not any(pattern in line for pattern in LEAK_PATTERNS):
            continue
        pid, _, command = line.partition(" ")
        if pid.isdigit() and int(pid) != os.getpid():
            rows.append((int(pid), command))
    return rows


def _tmux_sockets(processes: list[tuple[int, str]]) -> set[str]:
    sockets: set[str] = set()
    for _pid, command in processes:
        if "tmux -L " not in command:
            continue
        tokens = command.split()
        for index, token in enumerate(tokens[:-1]):
            if token == "-L" and tokens[index + 1].startswith("hive-ide-"):
                sockets.add(tokens[index + 1])
    return sockets


def _kill_processes(pids: list[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Release gates must not leave real tmux/sidebar loops behind."""
    processes = _pytest_tmp_processes()
    for socket in _tmux_sockets(processes):
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(0.2)
    _kill_processes([pid for pid, _command in _pytest_tmp_processes()])
