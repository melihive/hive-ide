from __future__ import annotations

import getpass
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


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


@pytest.fixture(autouse=True)
def _isolated_agent_stores(tmp_path_factory):
    """Keep conversation probes off this machine's real Claude and Codex stores.

    Both point at a dir that does not exist, so every probe answers unknown
    unless a test builds a store of its own. Plain os.environ, not monkeypatch:
    requesting monkeypatch here would reorder its teardown after other files'
    cleanup fixtures and leave their patches active during cleanup.
    """
    missing = tmp_path_factory.mktemp("agent-stores") / "absent"
    keys = {"CLAUDE_CONFIG_DIR": missing / "claude", "CODEX_HOME": missing / "codex"}
    saved = {key: os.environ.get(key) for key in keys}
    os.environ.update({key: str(value) for key, value in keys.items()})
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


CALLER_IDENTITY_KEYS = ("TMUX_PANE", "TMUX", "HIVE_IDE_TMUX_SOCKET")


@pytest.fixture(autouse=True)
def _isolated_caller_identity():
    """Start every test as a caller with NO tmux identity.

    The frame's caller guard is tri-state and fails closed: a pane id without a
    server, or a server marker that disagrees with `$TMUX`, is "unknown" and
    defers every destructive step. pytest itself often runs inside a tmux pane
    (an IDE session), and that inherited identity would turn into deferrals in
    tests that never asked about the caller. Tests that need an identity set it
    explicitly with monkeypatch. Plain os.environ for the same teardown-order
    reason as `_isolated_agent_stores`.
    """
    saved = {key: os.environ.get(key) for key in CALLER_IDENTITY_KEYS}
    for key in CALLER_IDENTITY_KEYS:
        os.environ.pop(key, None)
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
