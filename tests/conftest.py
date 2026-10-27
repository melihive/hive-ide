from __future__ import annotations

import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


LEAK_PATTERNS = (
    "hive_ide.sidebar",
    "tmux -L hive-ide-",
    "codex resume -C",
)


def _session_sweep_root(session) -> Path:
    """The ONE directory this pytest session's leak sweep may reach: its own basetemp.

    Never the shared `pytest-of-<user>` parent. Every pytest session on the machine
    keeps its basetemp under that parent, so a sweep rooted there kills a concurrent
    suite's live sidebars and tmux servers when this one finishes — another
    worktree's verify gate then fails a test that passes alone (reproduced
    2026-10-27: a staggered second suite lost `test_pane_lease_tmux`'s server
    mid-`new-session`).
    """
    return Path(session.config._tmp_path_factory.getbasetemp())


def _pytest_tmp_processes(root: Path) -> list[tuple[int, str]]:
    """Leak-pattern processes whose command line references a path INSIDE `root`.

    A plain substring on `root/`, so a sibling whose name merely starts with ours
    (`pytest-12` vs `pytest-123`) never matches and a basetemp holding regex
    metacharacters still does. `ps -axww -o pid=,command=` rather than `pgrep -af`:
    on macOS `pgrep -a` means "include ancestors" and prints no command line.
    """
    inside = f"{root}{os.sep}"
    try:
        result = subprocess.run(
            ["ps", "-axww", "-o", "pid=,command="],
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
        pid, _, command = line.strip().partition(" ")
        if inside not in command:
            continue
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
    """Release gates must not leave real tmux/sidebar loops behind — of THIS session."""
    root = _session_sweep_root(session)
    processes = _pytest_tmp_processes(root)
    for socket in _tmux_sockets(processes):
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(0.2)
    _kill_processes([pid for pid, _command in _pytest_tmp_processes(root)])


def _kill_servers_under(root: Path) -> None:
    """Stop every tmux server whose socket lives under `root`.

    Addressed by socket PATH (`-S`), not by name (`-L`), so it does not depend on
    `TMUX_TMPDIR` still pointing here when this runs.
    """
    for directory in (root, root / f"tmux-{os.getuid()}"):
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            # Never follow a link out of the sandbox, and only ever address a real
            # socket. Without this, a symlink planted where this directory is
            # expected would have cleanup issuing `kill-server` against whatever it
            # points at — the machine's real tmux directory included.
            try:
                if entry.is_symlink() or not entry.is_socket():
                    continue
            except OSError:
                continue
            try:
                subprocess.run(
                    ["tmux", "-S", str(entry), "kill-server"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                continue


def _isolation_root() -> Path | None:
    """A private directory for this run's tmux sockets, or None if none can be made.

    Two constraints shape this, and both have bitten:

    `mkdtemp`, never a predictable name with `exist_ok=True`. The directory is
    created atomically at mode 0700, so another local user cannot pre-create it with
    `tmux-<uid>` symlinked at this machine's real tmux directory and have the
    cleanup below issue `kill-server` against every socket there.

    `/tmp` by preference, not `TMPDIR`. A unix socket path is capped near 108 bytes
    and tmux appends `tmux-<uid>/<name>` to whatever this returns; `TMPDIR` is
    already long on macOS (`/var/folders/…`), which pushes a real socket path past
    the limit and makes every real-tmux test fail to start rather than isolate.
    """
    for base in ("/tmp", tempfile.gettempdir()):
        try:
            return Path(tempfile.mkdtemp(prefix="hive-ide-t-", dir=base))
        except OSError:
            continue
    return None


@pytest.fixture(scope="session", autouse=True)
def _isolated_tmux_tmpdir():
    """Keep every tmux server a test starts out of this machine's tmux directory.

    tmux leaves the socket FILE behind when its server dies, and a test's own
    `kill-server` does not remove it — so a suite that spawns real servers drops one
    file per test into `/tmp/tmux-<uid>` and nothing in the suite ever collects it.
    462 had accumulated by 2026-10-22, and that is not merely untidy: it made the
    socket listing unusable for reading a real frame, which is how a stale session
    stayed hidden. The in-package reaper does collect them, but only on `open` and
    only once an hour has passed, so the suite creates them faster than it clears
    them (150 in one afternoon's runs).

    Fixing it here rather than in each teardown is deliberate. Ten test files mint a
    socket and six call `kill-server` in their own cleanup; patching each is six
    chances to miss one, and the seventh test written next month misses it for free.
    Pointing the whole run at its own directory means nothing a test starts can reach
    the real directory even if its teardown never runs at all — an assertion failure,
    a crash, or a `-x` abort included.

    Tests that assert on the DEFAULT location override this with monkeypatch, which
    restores it per test, so they are unaffected.
    """
    saved = os.environ.get("TMUX_TMPDIR")
    root = _isolation_root()
    if root is None:
        yield None          # cannot isolate; run as before rather than fail the suite
        return
    os.environ["TMUX_TMPDIR"] = str(root)
    try:
        yield root
    finally:
        _kill_servers_under(root)
        if saved is None:
            os.environ.pop("TMUX_TMPDIR", None)
        else:
            os.environ["TMUX_TMPDIR"] = saved
        shutil.rmtree(root, ignore_errors=True)


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
