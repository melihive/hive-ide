"""A test runner must never clean up another runner's live IDE processes."""
import subprocess
from pathlib import Path

import conftest


def test_cleanup_processes_are_scoped_to_one_run(monkeypatch):
    def run(argv, **kwargs):
        assert argv == ["ps", "-axo", "pid=,command="]
        return subprocess.CompletedProcess(argv, 0, (
            " 101 python -m hive_ide.sidebar /fixtures/pytest-12/test/state\n"
            " 102 python -m hive_ide.sidebar /fixtures/pytest-123/test/state\n"
            " 103 python -m hive_ide.sidebar /fixtures/pytest-11/test/state\n"
            " 104 tmux -L hive-ide-fixture new-session /fixtures/pytest-12/test\n"
            " 105 python unrelated.py /fixtures/pytest-12/test\n"
        ))
    monkeypatch.setattr(subprocess, "run", run)
    rows = conftest._pytest_tmp_processes(Path("/fixtures/pytest-12"))
    assert [pid for pid, _ in rows] == [101, 104]
    assert conftest._tmux_sockets(rows) == {"hive-ide-fixture"}
