"""Process identity must survive platform differences and hostile argv text."""
from __future__ import annotations

import os
import struct
import subprocess
import sys

import pytest

from hive_ide.darwin_env import _environ_from_procargs
from hive_ide.frame import Frame


def test_darwin_procargs_separates_arguments_from_environment():
    raw = (struct.pack("i", 4) + b"/bin/sh\0\0\0"
           b"sh\0-c\0\0HIVE_IDE_SESSION_ID=wrong\0"
           b"HIVE_IDE_SESSION_ID=right\0PATH=/a path/bin\0EMPTY=\0\0ignored=tail\0")
    assert _environ_from_procargs(raw) == (
        b"HIVE_IDE_SESSION_ID=right\0PATH=/a path/bin\0EMPTY="
    )


@pytest.mark.parametrize("raw", [
    b"", b"\x01", struct.pack("i", -1), struct.pack("i", 999),
    struct.pack("i", 1) + b"unterminated",
    struct.pack("i", 2) + b"/bin/sh\0sh\0",
    struct.pack("i", 1) + b"/bin/sh\0sh\0IDENTITY=truncated",
    struct.pack("i", 1) + b"/bin/sh\0sh\0not-an-env-entry\0",
])
def test_darwin_malformed_observation_does_not_supply_identity(raw):
    assert _environ_from_procargs(raw) == b""


def test_darwin_children_are_direct_and_failed_observations_are_unknown(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    def run(argv, **kwargs):
        assert argv == ["ps", "-axo", "pid=,ppid="]
        assert kwargs["timeout"] == 2
        return subprocess.CompletedProcess(argv, 0, " 10 1\n 11 10\n 12 11\n bad row\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert Frame._child_pids(10) == [11]
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "11 10\n"))
    assert Frame._child_pids(10) == []
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("ps", 2)
    monkeypatch.setattr(subprocess, "run", timeout)
    assert Frame._child_pids(10) == []


def test_actual_child_environment_is_not_argv():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)", "HIVE_IDE_SESSION_ID=wrong"],
        env=dict(os.environ, HIVE_IDE_SESSION_ID="fixture-owned", HIVE_IDE_TEST_VALUE="a b=c"),
    )
    try:
        assert child.pid in Frame._child_pids(os.getpid())
        observed = Frame._process_env(child.pid)
        assert observed["HIVE_IDE_SESSION_ID"] == "fixture-owned"
        assert observed["HIVE_IDE_TEST_VALUE"] == "a b=c"
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert Frame._process_env(child.pid) == {}
