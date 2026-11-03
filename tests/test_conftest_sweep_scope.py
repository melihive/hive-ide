"""The end-of-session leak sweep must reach only THIS session's basetemp.

Rooted at the shared `pytest-of-<user>` parent, a finishing suite killed a
concurrent suite's live tmux servers and sidebars, failing tests that pass alone.
"""
from __future__ import annotations

import getpass
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

import conftest


class _Session:
    def __init__(self, config):
        self.config = config


def test_sweep_root_is_the_session_basetemp_not_the_shared_parent(pytestconfig):
    root = conftest._session_sweep_root(_Session(pytestconfig))
    shared = Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"

    assert root == pytestconfig._tmp_path_factory.getbasetemp()
    assert root.resolve() != shared.resolve()


def _sleeper(*argv: str) -> subprocess.Popen:
    # Extra argv after `-c` shows up in the command line pgrep reads.
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "hive_ide.sidebar", *argv]
    )


def _wait_listed(root: Path, pid: int) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pid in {found for found, _ in conftest._pytest_tmp_processes(root)}:
            return True
        time.sleep(0.05)
    return False


# A custom --basetemp may hold regex metacharacters; matching must stay literal.
@pytest.mark.parametrize("name", ["pytest-12", "run[1].x"])
def test_sweep_matches_only_processes_inside_its_own_root(tmp_path, name):
    root = tmp_path / name
    prefix_sibling = tmp_path / f"{name}3"         # name starts with ours
    other_session = tmp_path / "pytest-7"
    own = _sleeper("--state-home", str(root / "t0" / "state"))
    others = [
        _sleeper("--state-home", str(prefix_sibling / "t0" / "state")),
        _sleeper("--state-home", str(other_session / "t0" / "state")),
    ]
    try:
        assert _wait_listed(root, own.pid)
        found = {pid for pid, _ in conftest._pytest_tmp_processes(root)}
        assert own.pid in found
        assert not found & {proc.pid for proc in others}
    finally:
        for proc in (own, *others):
            proc.kill()
            proc.wait(5)
