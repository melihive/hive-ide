"""A held conversation costs one session, never the whole frame.

Found on a fleet machine that could not open its IDE from 2026-10-30 to
2026-11-02. Every attempt ended in

    hive-ide: No session windows could be opened; 3 session(s) failed.
    Run hive-ide verify and inspect the recorded session errors.

and `hive-ide verify` answered `ok: true`. Three things had to line up:

  - every session on that workspace was an agent session, and each conversation
    was held by a process outside the frame, so `open` was allowed to build
    nothing and raised instead of opening a degraded frame;
  - the per-session reasons were computed, written to the error store, and then
    dropped from the output, because the raise happens before `open` returns;
  - `verify` never read that store, and skipped the holder scan entirely when
    the frame was down — which is exactly when a holder blocks every open.

So the one message the user saw pointed at the one command that could not answer.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid

import pytest

from hive_ide import __version__
from hive_ide.cli import _conversation_holders, _recorded_open_errors
from hive_ide.drivers import bundled_drivers
from hive_ide.errors import HeldConversationError, UsageError
from hive_ide.frame import Frame
from hive_ide.holders import ConversationHolders
from hive_ide.store import StateStore


HELD_PID = 424242


def _source() -> dict:
    return {"kind": "stable", "interpreter": os.sys.executable, "version": __version__}


def _hold(monkeypatch, *, orphaned: bool = True) -> None:
    """Every conversation is held by an outside process."""
    monkeypatch.setattr(
        ConversationHolders,
        "holders",
        classmethod(
            lambda cls, reference, *, exclude_tree=None, rows=None: [
                {"pid": HELD_PID, "ppid": 1 if orphaned else 99, "orphaned": orphaned}
            ]
        ),
    )
    monkeypatch.setattr(
        ConversationHolders,
        "background_tree",
        classmethod(lambda cls, reference, rows=None: set()),
    )


def _session(store, name, *, driver_id="term", reference=None, working_dir=None):
    driver = bundled_drivers()[driver_id]
    directory = working_dir if working_dir is not None else store.workspace_key
    return store.create_session(
        name=name,
        working_dir=directory,
        source=_source(),
        driver=driver.resolve(
            name=name,
            working_dir=str(directory),
            conversation_reference=reference,
        ),
    )


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


class _Reply:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


# -- the frame opens anyway -------------------------------------------------


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")
def test_a_held_conversation_still_gets_its_window(store, monkeypatch, tmp_path):
    """The window goes up without a driver, and only that session is affected.

    Without this the frame had no window at all, so the session could not be
    reached to fix it: no sidebar row, nothing for the error key to show, and
    nowhere to run `hive-ide chat` from.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    held = _session(store, "HELD", driver_id="claude", reference="7b4fe520-held")
    plain = _session(store, "PLAIN")
    _hold(monkeypatch)
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")

    try:
        opened = frame.open(no_attach=True)

        assert opened["built_driverless"] == [held["id"]]
        assert opened["built"] == [plain["id"]]
        assert opened["windows"] == 2
        assert set(frame.windows()) == {held["id"], plain["id"]}

        # Reported, not swallowed: the session is in `failed` with the reason.
        assert [failure["name"] for failure in opened["failed"]] == ["HELD"]
        assert f"already held by pid {HELD_PID}" in opened["failed"][0]["error"]

        # The agent pane carries the sleeping message, NOT a second driver on the
        # held conversation — which is the whole point of the refusal.
        agent = (frame.role_panes(held["id"]) or {})["agent"]
        started = frame.tmux(
            ["display-message", "-p", "-t", agent, "#{pane_start_command}"]
        ).stdout
        assert "hive-ide chat" in started
        assert "--resume" not in started
        assert "7b4fe520-held" not in started

        # The error stands until the session actually gets a driver, so the
        # sidebar keeps marking it and the error key keeps answering.
        error = store.read("errors", held["id"])
        assert error is not None
        assert error["component"] == "frame:open"
        assert f"already held by pid {HELD_PID}" in error["detail"]
        assert "session directory or package source" not in error["recovery"]

        # A second open finds the window standing and must not quietly clear the
        # error: the session is still driverless.
        again = frame.open(no_attach=True)
        assert again["windows"] == 2
        assert store.read("errors", held["id"]) is not None
    finally:
        frame.tmux(["kill-server"])


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")
def test_the_fatal_message_carries_each_sessions_reason(store, monkeypatch, tmp_path):
    """When nothing can be built, the raise says why — per session.

    It used to say only how many had failed and point at `verify`, so the
    reasons it already held never reached the one person who needed them.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    missing = tmp_path / "removed-worktree"
    _session(store, "STALE", working_dir=missing)
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")

    try:
        with pytest.raises(UsageError) as raised:
            frame.open(no_attach=True)
        message = str(raised.value)
        assert "1 session(s) failed" in message
        assert f"STALE: Session working directory does not exist: {missing}" in message
    finally:
        frame.tmux(["kill-server"])


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")
def test_a_sleeping_session_is_never_refused_for_a_held_conversation(store, monkeypatch):
    """A sleeping session starts no driver, so nothing can collide with it.

    The launch check sat in the build path ahead of the sleeping branch, so a
    holder refused a window that was never going to start a driver at all.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    record = _session(store, "DOZING", driver_id="claude", reference="7b4fe520-held")
    record["sleep"] = {"state": "sleeping", "since": "2026-11-02T00:00:00+00:00"}
    store.write("sessions", record["id"], record)
    _hold(monkeypatch)
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")

    try:
        opened = frame.open(no_attach=True)

        assert opened["built_sleeping"] == [record["id"]]
        assert opened["failed"] == []
        assert opened["built_driverless"] == []
        assert record["id"] in frame.windows()
        assert store.read("errors", record["id"]) is None
    finally:
        frame.tmux(["kill-server"])


def test_a_relaunched_driver_clears_the_recorded_error(store, monkeypatch):
    """`hive-ide chat` is the documented recovery, so it must settle the error."""
    record = _session(store, "HELD", driver_id="claude", reference="abc-123")
    frame = Frame(store, socket="sock")
    frame._record_open_error(record, HeldConversationError("held by pid 1"))
    assert store.read("errors", record["id"]) is not None

    monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: False)
    monkeypatch.setattr(Frame, "prepare_agent_launch", lambda self, rec: None)
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply())

    assert frame.respawn_agent(record, "%1") is True
    assert store.read("errors", record["id"]) is None


# -- and verify can finally answer -----------------------------------------


def test_verify_reads_back_the_recorded_open_errors(store):
    """The store `open` writes is the record of the last failure; verify shows it."""
    first = _session(store, "ONE", driver_id="claude", reference="aaa-1")
    second = _session(store, "TWO", driver_id="claude", reference="bbb-2")
    frame = Frame(store, socket="sock")
    frame._record_open_error(first, HeldConversationError("held by pid 11"))
    time.sleep(0.01)
    frame._record_open_error(second, HeldConversationError("held by pid 22"))

    rows = _recorded_open_errors(store, [first, second])

    assert {row["session_id"] for row in rows} == {first["id"], second["id"]}
    assert all(row["component"] == "frame:open" for row in rows)
    assert "held by pid" in rows[0]["detail"]
    # Newest first: the last failure is the one being chased.
    assert rows == sorted(rows, key=lambda row: row["observed_at"], reverse=True)


def test_an_error_for_a_session_that_is_gone_is_not_a_finding(store):
    """Nothing deletes the error row of a session archived while it was failing.

    Reported, it would hold `ok: false` for the whole workspace with nothing left
    to fix — a health report that can never go green again is one nobody reads.
    """
    record = _session(store, "GONE", driver_id="claude", reference="ccc-3")
    Frame(store, socket="sock")._record_open_error(
        record, HeldConversationError("held by pid 33")
    )

    assert _recorded_open_errors(store, [record]) != []
    assert _recorded_open_errors(store, []) == []


def test_verify_finds_a_holder_while_the_frame_is_down(store, monkeypatch):
    """A holder outlives the frame, and that is the state that blocks every open.

    Gated on `frame.exists()`, this check reported nothing in the one situation
    it was written for, and `verify` called the machine healthy.
    """
    record = _session(store, "HELD", driver_id="claude", reference="7b4fe520-held")
    frame = Frame(store, socket="sock")
    # No server: has-session fails, so exists() is False and there are no panes.
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply(returncode=1))
    assert frame.exists() is False
    _hold(monkeypatch)

    found = _conversation_holders(frame, [record], [(HELD_PID, 1, "claude --resume x")])

    assert [row["session"] for row in found] == ["HELD"]
    assert found[0]["pid"] == HELD_PID
    assert found[0]["orphaned"] is True


def test_an_unreadable_process_table_is_not_a_clean_bill(store, monkeypatch):
    """No rows means "could not look", which must not read as "no holders"."""
    record = _session(store, "HELD", driver_id="claude", reference="7b4fe520-held")
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply(returncode=1))
    _hold(monkeypatch)

    assert _conversation_holders(frame, [record], []) == []


def test_verify_reports_the_recorded_error_and_is_not_ok(store, monkeypatch, tmp_path):
    """End to end through the command: a standing error makes verify say so."""
    from hive_ide import cli

    record = _session(store, "HELD", driver_id="claude", reference="7b4fe520-held")
    Frame(store, socket="sock")._record_open_error(
        record, HeldConversationError("Conversation 7b4fe520 is already held by pid 1")
    )
    monkeypatch.setattr(cli, "_context", lambda args: (store, {}))
    monkeypatch.setattr(cli, "_refresh_workspace_sources", lambda store: None)
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply(returncode=1))
    monkeypatch.setattr(
        ConversationHolders, "snapshot", classmethod(lambda cls: [(1, 0, "init")])
    )

    args = type(
        "Args",
        (),
        {"state_home": None, "config": None, "workspace_key": None, "stable_python": None},
    )()
    result = cli.cmd_verify(args)

    assert result["ok"] is False
    assert len(result["open_errors"]) == 1
    assert "already held by pid 1" in result["error_findings"][0]
    assert "Could not open session HELD" in result["error_findings"][0]
