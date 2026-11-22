"""Process identity must survive platform differences and hostile argv text."""
from __future__ import annotations

import os
import struct
import subprocess
import sys

import pytest

from hive_ide.darwin_env import _environ_from_procargs
from hive_ide.frame import Frame
from hive_ide.holders import ConversationHolders
from hive_ide.repair import SessionRepair
from hive_ide.store import StateStore


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


def test_darwin_tree_uses_one_snapshot_and_repair_preserves_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    rows = [(100, 1, "sh"), (101, 100, "sh"), (102, 101, "codex --no-daemon"),
            (200, 1, "codex --no-daemon")]
    calls = []
    def snapshot():
        calls.append(True)
        return rows
    monkeypatch.setattr(ConversationHolders, "snapshot", staticmethod(snapshot))
    assert Frame._process_tree(100) == [100, 101, 102]
    assert len(calls) == 1
    frame = Frame(StateStore(tmp_path / "state", tmp_path))
    monkeypatch.setattr(frame, "agent_pane_pid", lambda record: 100)
    repair = SessionRepair(frame.store, frame)
    record = {"driver": {"id": "codex", "launch_argv": ["codex"]}}
    assert repair._shell_agent_has_live_driver_child(record) is True
    rows[2] = (102, 101, "sleep 30")
    assert repair._shell_agent_has_live_driver_child(record) is False
    rows[:] = [(100, 1, "sh")]
    assert repair._shell_agent_has_live_driver_child(record) is False
    rows.clear()  # process-table failure, not evidence that the driver exited
    assert repair._shell_agent_has_live_driver_child(record) is True
    rows[:] = [(200, 1, "sleep 30")]  # root unobservable even though ps succeeded
    assert repair._shell_agent_has_live_driver_child(record) is True


# -- is the caller inside one of this server's panes? -------------------------------
#
# The environment cannot settle this alone. A marker is an ordinary variable an
# unrelated process can inherit, so marker-only evidence stays unknown — and an
# agent driving a release has the marker but no `$TMUX` and no `$TMUX_PANE`, which
# read as unknown and made the caller guard refuse the supervisory board on seven
# consecutive runs. The kernel's view cannot be inherited, so it is asked instead.

import shutil
import uuid


requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux is unavailable"
)


@pytest.fixture
def scratch_frame(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    socket = f"hive-ide-caller-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    made = Frame(StateStore(tmp_path / "state", workspace), socket=socket)
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-s", made.target, "sh", "-c",
         "while :; do sleep 30; done"],
        capture_output=True, text=True,
    )
    try:
        yield made
    finally:
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"], capture_output=True, text=True
        )


def _a_pane(frame) -> tuple[str, str]:
    """(pid, tty) of a real pane on this server."""
    out = subprocess.run(
        ["tmux", "-L", frame.socket, "list-panes", "-a", "-F", "#{pane_pid} #{pane_tty}"],
        capture_output=True, text=True,
    ).stdout.split()
    return out[0], out[1]


@requires_tmux
def test_a_terminal_belonging_to_no_pane_reads_as_outside(scratch_frame, monkeypatch):
    monkeypatch.setattr(
        Frame, "caller_ancestry", lambda self: ([999999], "pts/999999")
    )

    assert scratch_frame.caller_outside_panes() is True


@requires_tmux
def test_a_terminal_that_is_a_panes_reads_as_inside(scratch_frame, monkeypatch):
    _pid, tty = _a_pane(scratch_frame)
    # As `ps` spells it, which is not how `pane_tty` spells it.
    monkeypatch.setattr(
        Frame, "caller_ancestry", lambda self, t=tty: ([999999], t.removeprefix("/dev/"))
    )

    assert scratch_frame.caller_outside_panes() is False


@requires_tmux
def test_an_ancestor_that_is_a_panes_own_process_reads_as_inside(scratch_frame, monkeypatch):
    pid, _tty = _a_pane(scratch_frame)
    # No terminal at all, but ancestry places us under the pane's shell: a
    # descendant of the pane IS inside it, whatever it is attached to.
    monkeypatch.setattr(
        Frame, "caller_ancestry", lambda self, p=int(pid): ([999999, p], None)
    )

    assert scratch_frame.caller_outside_panes() is False


@requires_tmux
def test_no_terminal_anywhere_is_unknown_not_outside(scratch_frame, monkeypatch):
    """The double-fork hazard, and why `True` needs positive evidence.

    A process that forked out of a pane and lost its terminal looks identical to
    one that was never in a pane. Reading that as "outside" would let it replace
    the pane it is sitting in, which is the incident the caller guard exists for —
    a run once lost its own session that way.
    """
    monkeypatch.setattr(Frame, "caller_ancestry", lambda self: ([999999], None))

    assert scratch_frame.caller_outside_panes() is None


@requires_tmux
def test_a_server_that_cannot_be_listed_is_unknown(scratch_frame, monkeypatch):
    monkeypatch.setattr(
        Frame, "tmux",
        lambda self, args, **kw: subprocess.CompletedProcess(args, 1, "", "no server"),
    )

    assert scratch_frame.caller_outside_panes() is None


def test_linux_and_macos_terminal_spellings_compare_equal():
    """`pane_tty` says /dev/ttys001; `ps -o tty=` says s001. Same terminal.

    Comparing the raw strings never matches on macOS, and a comparison that can
    never match reads exactly like "the caller is somewhere else" — the verdict
    that lets a destructive helper proceed.
    """
    assert Frame._tty_names("/dev/ttys001") & Frame._tty_names("s001")
    assert Frame._tty_names("/dev/pts/88") & Frame._tty_names("pts/88")
    assert not (Frame._tty_names("/dev/pts/88") & Frame._tty_names("pts/89"))


@requires_tmux
def test_the_guard_uses_kernel_evidence_when_the_environment_cannot_tell(
    scratch_frame, monkeypatch
):
    """The actual refusal this fixes, and the shape that must still refuse."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("HIVE_IDE_TMUX_SOCKET", scratch_frame.socket)
    pane = subprocess.run(
        ["tmux", "-L", scratch_frame.socket, "list-panes", "-a", "-F", "#{pane_id}"],
        capture_output=True, text=True,
    ).stdout.split()[0]

    # Provably outside every pane -> a verdict, so the guard allows the target.
    monkeypatch.setattr(Frame, "caller_outside_panes", lambda self: True)
    assert scratch_frame.pane_is_caller(pane) is False

    # Cannot tell, and no tmux variable either -> the environment settles it:
    # tmux exports `$TMUX` and `$TMUX_PANE` into every pane it creates, so a
    # caller with neither is in no pane. Until 2026-11-22 this was deferred, and
    # the deferral proved unclearable (see
    # `test_a_caller_with_no_tmux_variables_at_all_is_outside`).
    monkeypatch.setattr(Frame, "caller_outside_panes", lambda self: None)
    assert scratch_frame.pane_is_caller(pane) is False

    # The shape that must STILL be unsettled: `$TMUX` present and the kernel
    # unable to answer. A process double-forked out of a pane looks exactly like
    # this, because the environment survives a fork — so the ENVIRONMENT rule
    # above must not fire for it.
    #
    # Asserted on `caller_in_no_pane`, not `pane_is_caller`: with `$TMUX` naming
    # this server and no pane id at all, `pane_is_caller` answers `False` for an
    # older and separate reason — tmux exports `$TMUX_PANE` into every pane, so
    # a process on this server without one is a popup, a `run-shell` or a hook,
    # none of which can BE a pane. What the double-fork shape must not do is
    # license a window REBUILD, and that path asks `caller_location`
    # (`test_a_caller_that_still_has_tmux_defers_when_the_kernel_cannot_tell`).
    monkeypatch.setenv("TMUX", f"{scratch_frame.socket_path()},1,0")
    window = subprocess.run(
        ["tmux", "-L", scratch_frame.socket, "display-message", "-p", "#{window_id}"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert scratch_frame.caller_in_no_pane() is None
    assert scratch_frame.caller_location(window) is None


def test_a_process_table_that_fails_midway_is_unknown_not_truncated(tmp_path, monkeypatch):
    """A half-read ancestry must not become a verdict.

    `caller_outside_panes` says "outside" partly because no ancestor is a pane's
    own process — which is only meaningful if the ancestry was read in full. If a
    `ps` call fails on the third hop and the walk simply stops, the answer looks
    complete: a terminal was already found, no pane pid was seen among the two
    ancestors that WERE read, and the unchecked ones are silently assumed
    innocent. That reads as "outside" for a caller that may be sitting in the
    pane it is about to replace.

    Found by mutation, not by the tests above: every one of them stubs
    `caller_ancestry` wholesale, so none of them exercises the walk failing.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace))
    calls: list[int] = []
    real_run = subprocess.run

    def flaky_ps(args, **kwargs):
        if args[:1] == ["ps"]:
            calls.append(1)
            if len(calls) == 1:
                return subprocess.CompletedProcess(args, 0, "4242 pts/7\n", "")
            raise OSError("process table unavailable")
        return real_run(args, **kwargs)

    monkeypatch.setattr("hive_ide.frame.subprocess.run", flaky_ps)

    assert frame.caller_ancestry() is None, (
        "a failed ps left a truncated ancestry that reads as a complete one"
    )


@pytest.mark.parametrize(
    "name,result",
    [
        ("nonzero exit", subprocess.CompletedProcess(["ps"], 1, "", "no such process")),
        ("empty output", subprocess.CompletedProcess(["ps"], 0, "", "")),
        ("ppid is not a number", subprocess.CompletedProcess(["ps"], 0, "nope pts/7\n", "")),
    ],
)
def test_every_incomplete_ancestry_is_unknown(tmp_path, monkeypatch, name, result):
    """Four ways the walk can end early; all of them are unknown.

    `caller_outside_panes` answers "outside" partly BECAUSE no ancestor was a
    pane's own process — which only means anything if every ancestor was read. An
    earlier version returned partial evidence for a nonzero exit, empty output and
    a malformed ppid, and only the raised exception was covered by a test. Review
    reproduced `pane_is_caller()` answering False after a failed `ps`, and False
    is the verdict that lets a helper replace the caller's own pane.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace))
    real_run = subprocess.run

    def stub(args, **kwargs):
        if args[:1] == ["ps"]:
            return result
        return real_run(args, **kwargs)

    monkeypatch.setattr("hive_ide.frame.subprocess.run", stub)

    assert frame.caller_ancestry() is None, f"{name} produced a partial ancestry"


def test_an_ancestry_deeper_than_the_hop_budget_is_unknown(tmp_path, monkeypatch):
    """The fourth way: a chain longer than the budget leaves ancestors unread."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace))
    real_run = subprocess.run

    def endless(args, **kwargs):
        if args[:1] == ["ps"]:
            # Always one more parent, never reaching init.
            return subprocess.CompletedProcess(args, 0, "4242 pts/7\n", "")
        return real_run(args, **kwargs)

    monkeypatch.setattr("hive_ide.frame.subprocess.run", endless)

    assert frame.caller_ancestry() is None


def test_the_interactive_attach_carries_the_pinned_socket_directory(tmp_path, monkeypatch):
    """`tmux()` passes it per subprocess; `execvp` would inherit the ambient one.

    A pinned frame could be built successfully and then attach to nothing — or to
    a different server sharing the name — because the attach replaces this process
    and never goes through `tmux()`. Reaching that path the way
    `test_attach_clients.py` does, since it lives inside `open()`.
    """
    class _Result:
        def __init__(self, out=""):
            self.returncode = 0
            self.stdout = out
            self.stderr = ""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    frame = Frame(store, socket="hive-ide-test-pin")
    frame.socket_tmpdir = "/pinned/base"
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    seen: dict[str, str] = {}

    monkeypatch.setattr(
        "hive_ide.frame.os.execvp",
        lambda file, argv: seen.update(
            tmpdir=os.environ.get("TMUX_TMPDIR", ""), argv=" ".join(argv)
        ),
    )
    monkeypatch.setattr(Frame, "require_tmux", lambda self: None)
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "ensure", lambda self, record, **_kw: False)
    monkeypatch.setattr(Frame, "bind_keys", lambda self: None)
    monkeypatch.setattr(Frame, "windows", lambda self: {"s1": "@1"})
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Result("@1")
    )
    monkeypatch.setattr(store, "list", lambda collection: [{"id": "s1", "name": "ONE"}])

    frame.open()

    assert seen.get("argv"), "open() never attached"
    assert seen["tmpdir"] == "/pinned/base", (
        "the attach would have looked for the server in the wrong directory"
    )
