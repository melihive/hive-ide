"""`verify` must name the two conditions that were invisible when they bit.

Both were found on mim on 2026-10-13 and neither was reported by anything:

  - two `_preheat` tmux sessions from 2026-10-05, unattached, one `fish` window
    each, sharing a hive-ide socket and keeping that server alive;
  - an orphaned driver at ppid 1 holding a conversation for over 16 hours, which
    only surfaced when reopening the frame started a second driver on it.

Reported, not reaped: nothing in this package creates a `_preheat` session, so
there is no producer to guard against, and killing another process or another
tmux session on a heuristic is not something a verify command should do.
"""
from __future__ import annotations

import time

import pytest

from hive_ide.frame import Frame
from hive_ide.holders import ConversationHolders
from hive_ide.store import StateStore


class _Reply:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def test_unmanaged_sessions_excludes_the_frames_own_session(store, monkeypatch):
    frame = Frame(store, socket="sock")
    eight_days = int(time.time()) - 8 * 86400
    listing = (
        f"{frame.target}\t{int(time.time())}\t1\t13\n"
        f"_preheat\t{eight_days}\t0\t1\n"
        f"something-else\t{eight_days}\t0\t2\n"
    )
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply(listing))

    names = [s["name"] for s in frame.unmanaged_sessions()]
    assert names == ["_preheat", "something-else"], names
    assert frame.target not in names


def test_unmanaged_sessions_reports_age_and_attachment(store, monkeypatch):
    """Age and attachment are what make the call actionable — an 8-day-old
    unattached session is abandoned; a fresh attached one is someone working."""
    frame = Frame(store, socket="sock")
    created = int(time.time()) - 8 * 86400
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Reply(f"_preheat\t{created}\t0\t1\n")
    )

    found = frame.unmanaged_sessions()
    assert len(found) == 1
    assert found[0]["attached"] is False
    assert found[0]["windows"] == 1
    assert found[0]["created_epoch"] == created


def test_unmanaged_sessions_is_empty_when_tmux_will_not_answer(store, monkeypatch):
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply("", 1))
    assert Frame(store, socket="sock").unmanaged_sessions() == []


def test_a_malformed_listing_row_is_skipped_not_guessed_at(store, monkeypatch):
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda self, args, **kw: _Reply("_preheat\t123\t0\t1\ntruncated\trow\n"),
    )
    assert [s["name"] for s in frame.unmanaged_sessions()] == ["_preheat"]


REF = "9a9bb400aabbccdd"


def test_verify_reports_an_orphan_holding_a_conversation(store, monkeypatch, tmp_path):
    """The mim orphan, named. pid 900 at ppid 1 holds the conversation while the
    session's own agent pane is a different process tree."""
    from hive_ide.cli import _conversation_holders

    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    monkeypatch.setattr(
        ConversationHolders,
        "snapshot",
        classmethod(
            lambda cls: [
                (100, 1, "sh -c agent"),
                (900, 1, f"/opt/homebrew/bin/claude --resume {REF}"),
            ]
        ),
    )
    sessions = [{"id": "s1", "name": "DC MOBILE", "driver": {"resume": {"reference": REF}}}]

    found = _conversation_holders(frame, sessions)

    assert len(found) == 1, found
    assert found[0]["pid"] == 900
    assert found[0]["orphaned"] is True
    assert found[0]["session"] == "DC MOBILE"


def test_verify_does_not_report_the_sessions_own_agent_as_a_holder(store, monkeypatch):
    """Every healthy session's agent holds its own conversation. Reporting that
    would make the finding permanent noise and therefore worthless."""
    from hive_ide.cli import _conversation_holders

    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    monkeypatch.setattr(
        ConversationHolders,
        "snapshot",
        classmethod(
            lambda cls: [
                (100, 1, "sh -c agent"),
                (101, 100, f"claude --resume {REF}"),     # the pane's own agent
            ]
        ),
    )
    sessions = [{"id": "s1", "name": "ALPHA", "driver": {"resume": {"reference": REF}}}]

    assert _conversation_holders(frame, sessions) == []


def test_verify_reports_nothing_when_the_process_table_is_unreadable(store, monkeypatch):
    """"could not observe" must not be reported as "found a problem" — a verify that
    cries wolf on an unreadable `ps` teaches people to ignore it."""
    from hive_ide.cli import _conversation_holders

    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(ConversationHolders, "snapshot", classmethod(lambda cls: []))
    sessions = [{"id": "s1", "name": "ALPHA", "driver": {"resume": {"reference": REF}}}]

    assert _conversation_holders(frame, sessions) == []


def test_verify_skips_the_scan_entirely_when_no_frame_is_running(store, monkeypatch):
    from hive_ide.cli import _conversation_holders

    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: False)
    called: list = []
    monkeypatch.setattr(
        ConversationHolders, "snapshot", classmethod(lambda cls: called.append(1) or [])
    )
    sessions = [{"id": "s1", "name": "ALPHA", "driver": {"resume": {"reference": REF}}}]

    assert _conversation_holders(frame, sessions) == []
    assert called == [], "no frame means nothing to compare against; do not read ps"


def _verify_args(tmp_path):
    import argparse

    return argparse.Namespace(
        workspace_key=str(tmp_path / "workspace"),
        state_home=str(tmp_path / "state"),
        config=str(tmp_path / "config.json"),
        stable_python=None,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("clients", [{"client": "/dev/pts/0", "size": "80x24", "last_active_epoch": 1},
                     {"client": "/dev/pts/1", "size": "200x50", "last_active_epoch": 2}]),
        ("unmanaged", [{"name": "_preheat", "created_epoch": 1, "attached": False, "windows": 1}]),
        ("holders", [{"session": "ALPHA", "reference": REF, "pid": 900, "ppid": 1, "orphaned": True}]),
    ],
)
def test_each_stale_condition_makes_verify_not_ok(tmp_path, monkeypatch, field, value):
    """The assertion that stops these being cosmetic.

    Each of the three conditions is folded into `ok`, and nothing asserted that
    until now — including `clients`, added in 1.0.93, whose contribution to `ok`
    was never covered. A finding that prints but leaves `ok` true is a finding
    every script ignores.
    """
    from hive_ide import cli

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "_refresh_workspace_sources", lambda store: None)
    monkeypatch.setattr(cli, "inspect_interpreter", lambda interp: {"package_version": "0"})
    monkeypatch.setattr(Frame, "verify_user_config", lambda self: [])
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "attached_clients", lambda self: value if field == "clients" else [])
    monkeypatch.setattr(
        Frame, "unmanaged_sessions", lambda self: value if field == "unmanaged" else []
    )
    monkeypatch.setattr(
        cli, "_conversation_holders", lambda frame, sessions: value if field == "holders" else []
    )
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["ok"] is False, f"{field} did not affect ok: {result}"
    key = {"clients": "client_findings", "unmanaged": "unmanaged_findings",
           "holders": "holder_findings"}[field]
    assert result[key], f"{key} was empty although {field} was present"


def test_verify_is_ok_when_none_of_the_three_conditions_is_present(tmp_path, monkeypatch):
    """The inverse, so the test above is not passing for an unrelated reason."""
    from hive_ide import cli

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "_refresh_workspace_sources", lambda store: None)
    monkeypatch.setattr(cli, "inspect_interpreter", lambda interp: {"package_version": "0"})
    monkeypatch.setattr(Frame, "verify_user_config", lambda self: [])
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame, "attached_clients", lambda self: [{"client": "/dev/pts/0", "size": "200x50", "last_active_epoch": 1}]
    )
    monkeypatch.setattr(Frame, "unmanaged_sessions", lambda self: [])
    monkeypatch.setattr(cli, "_conversation_holders", lambda frame, sessions: [])
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["ok"] is True, result
    assert result["client_findings"] == []
    assert result["unmanaged_findings"] == []
    assert result["holder_findings"] == []
