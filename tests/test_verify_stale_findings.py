"""`verify` must name the two conditions that were invisible when they bit.

Both were found on one fleet machine on 2026-10-13, and neither was reported
by anything:

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

from hive_ide.cli import _agentless_sessions, _conversation_holders
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


REF = "a1b2c3d4e5f6a7b8"


def test_verify_reports_an_orphan_holding_a_conversation(store, monkeypatch, tmp_path):
    """The orphan, named. pid 900 at ppid 1 holds the conversation while the
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
    sessions = [{"id": "s1", "name": "MOBILE", "driver": {"resume": {"reference": REF}}}]

    found = _conversation_holders(frame, sessions, ConversationHolders.snapshot())

    assert len(found) == 1, found
    assert found[0]["pid"] == 900
    assert found[0]["orphaned"] is True
    assert found[0]["session"] == "MOBILE"


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

    assert _conversation_holders(frame, sessions, ConversationHolders.snapshot()) == []


def test_verify_reports_nothing_when_the_process_table_is_unreadable(store, monkeypatch):
    """"could not observe" must not be reported as "found a problem" — a verify that
    cries wolf on an unreadable `ps` teaches people to ignore it."""
    from hive_ide.cli import _conversation_holders

    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(ConversationHolders, "snapshot", classmethod(lambda cls: []))
    sessions = [{"id": "s1", "name": "ALPHA", "driver": {"resume": {"reference": REF}}}]

    assert _conversation_holders(frame, sessions, ConversationHolders.snapshot()) == []


def test_verify_does_not_read_the_process_table_with_no_frame_running(tmp_path, monkeypatch):
    """No frame means nothing to compare processes against, so the scan is skipped
    at the caller — which is also what keeps `verify` cheap on an unopened
    workspace."""
    from hive_ide import cli

    (tmp_path / "workspace").mkdir()
    called: list = []
    monkeypatch.setattr(cli, "_refresh_workspace_sources", lambda store: None)
    monkeypatch.setattr(Frame, "verify_user_config", lambda self: [])
    monkeypatch.setattr(Frame, "exists", lambda self: False)
    monkeypatch.setattr(Frame, "attached_clients", lambda self: [])
    # `exists` is faked True with no real server behind it, so the pane scan would
    # correctly report "could not scan" and make verify incomplete. Stubbed like
    # every other collaborator here: this test is about a different condition.
    monkeypatch.setattr(Frame, "half_built_windows", lambda self: [])
    monkeypatch.setattr(
        ConversationHolders, "snapshot", classmethod(lambda cls: called.append(1) or [])
    )
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert called == [], "read ps with no frame to compare it against"
    assert result["incomplete"] == [], "no frame is not an incomplete check"


def test_an_unreadable_process_table_makes_verify_incomplete_not_ok(tmp_path, monkeypatch):
    """A check that could not run is not a check that passed.

    Without this, an unreadable process table silently skips two findings and
    `verify` reports a confidently healthy frame it never inspected. Note this is
    the OPPOSITE policy to a driver launch, which proceeds when it cannot observe —
    a launch must stay available, while a report must not overstate what it saw.
    """
    from hive_ide import cli

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "_refresh_workspace_sources", lambda store: None)
    monkeypatch.setattr(cli, "inspect_interpreter", lambda interp: {"package_version": "0"})
    monkeypatch.setattr(Frame, "verify_user_config", lambda self: [])
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "attached_clients", lambda self: [])
    # `exists` is faked True with no real server behind it, so the pane scan would
    # correctly report "could not scan" and make verify incomplete. Stubbed like
    # every other collaborator here: this test is about a different condition.
    monkeypatch.setattr(Frame, "half_built_windows", lambda self: [])
    monkeypatch.setattr(Frame, "unmanaged_sessions", lambda self: [])
    monkeypatch.setattr(ConversationHolders, "snapshot", classmethod(lambda cls: []))
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["incomplete"], "the skipped checks were not reported"
    assert result["ok"] is False, "verify claimed healthy without running two checks"


def test_an_unmanaged_session_is_reported_but_does_not_fail_verify(tmp_path, monkeypatch):
    """A frame does not own its socket by contract — `Frame` takes a configured
    socket independently of its workspace-derived session name, so another
    workspace pointed at the same socket is legitimate. Reported as inventory;
    failing this machine's verify over someone else's frame would be wrong."""
    from hive_ide import cli

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "_refresh_workspace_sources", lambda store: None)
    monkeypatch.setattr(cli, "inspect_interpreter", lambda interp: {"package_version": "0"})
    monkeypatch.setattr(Frame, "verify_user_config", lambda self: [])
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "attached_clients", lambda self: [])
    # `exists` is faked True with no real server behind it, so the pane scan would
    # correctly report "could not scan" and make verify incomplete. Stubbed like
    # every other collaborator here: this test is about a different condition.
    monkeypatch.setattr(Frame, "half_built_windows", lambda self: [])
    monkeypatch.setattr(
        Frame,
        "unmanaged_sessions",
        lambda self: [{"name": "other", "created_epoch": 1, "attached": False, "windows": 1}],
    )
    monkeypatch.setattr(ConversationHolders, "snapshot", classmethod(lambda cls: [(1, 0, "init")]))
    monkeypatch.setattr(cli, "_conversation_holders", lambda frame, sessions, rows: [])
    monkeypatch.setattr(cli, "_agentless_sessions", lambda frame, sessions, rows: [])
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["unmanaged_findings"], "the session was not reported at all"
    assert result["ok"] is True, "an unmanaged session must not fail verify"



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
    monkeypatch.setattr(Frame, "unmanaged_sessions", lambda self: [])
    monkeypatch.setattr(
        cli,
        "_conversation_holders",
        lambda frame, sessions, rows: value if field == "holders" else [],
    )
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["ok"] is False, f"{field} did not affect ok: {result}"
    key = {"clients": "client_findings", "holders": "holder_findings"}[field]
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
    monkeypatch.setattr(Frame, "half_built_windows", lambda self: [])
    monkeypatch.setattr(cli, "_conversation_holders", lambda frame, sessions, rows: [])
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["ok"] is True, result
    assert result["client_findings"] == []
    assert result["unmanaged_findings"] == []
    assert result["holder_findings"] == []


def _agent_session(name, driver_id="claude", sleeping=False, reference=None):
    session = {"id": f"id-{name}", "name": name, "driver": {"id": driver_id}}
    if reference:
        session["driver"]["resume"] = {"reference": reference}
    if sleeping:
        session["sleep"] = {"state": "sleeping"}
    return session


def _agentless(frame, sessions, rows):
    from hive_ide.cli import _agentless_sessions

    return _agentless_sessions(frame, sessions, rows)


def test_an_agent_pane_with_no_driver_is_reported(store, monkeypatch):
    """A pane with no agent in it looks exactly like a working one: the frame is up,
    the layout is right, and the last frame the agent painted is still on screen.
    That is the shape of every "frozen session" that turned out to be a dead agent."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "/bin/sh -c agent"), (101, 100, "fish")]

    found = _agentless(frame, [_agent_session("STATZ")], rows)

    assert [f["session"] for f in found] == ["STATZ"], found
    assert found[0]["driver"] == "claude"


def test_an_agent_pane_with_a_live_driver_is_not_reported(store, monkeypatch):
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "/bin/sh -c agent"), (101, 100, "claude")]

    assert _agentless(frame, [_agent_session("HIDE")], rows) == []


def test_a_driver_nested_deeper_in_the_pane_still_counts(store, monkeypatch):
    """The pane runs `sh -c`, which may run a shell, which runs the driver."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent"), (101, 100, "fish"), (102, 101, "codex")]

    assert _agentless(frame, [_agent_session("ADMIN", "codex")], rows) == []


def test_a_sleeping_session_is_not_reported(store, monkeypatch):
    """A sleeping session has deliberately stopped its own driver, so an absent one
    is correct. On the live frame 10 of 13 sessions were sleeping — without this
    exclusion verify would cry wolf about every one of them, every run."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent")]

    assert _agentless(frame, [_agent_session("HIVE DEV", sleeping=True)], rows) == []


def test_a_terminal_session_is_not_reported(store, monkeypatch):
    """A `term` session has no driver by definition."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent")]

    assert _agentless(frame, [_agent_session("TERMINAL", "term")], rows) == []


def test_a_pane_whose_pid_cannot_be_read_is_not_called_broken(store, monkeypatch):
    """Unknown is not a verdict: an unobservable pane is not a missing agent."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: None)

    assert _agentless(frame, [_agent_session("STATZ")], [(1, 0, "init")]) == []


def test_verify_is_not_ok_when_a_session_has_no_agent(tmp_path, monkeypatch):
    from hive_ide import cli

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(cli, "_refresh_workspace_sources", lambda store: None)
    monkeypatch.setattr(cli, "inspect_interpreter", lambda interp: {"package_version": "0"})
    monkeypatch.setattr(Frame, "verify_user_config", lambda self: [])
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "attached_clients", lambda self: [])
    # `exists` is faked True with no real server behind it, so the pane scan would
    # correctly report "could not scan" and make verify incomplete. Stubbed like
    # every other collaborator here: this test is about a different condition.
    monkeypatch.setattr(Frame, "half_built_windows", lambda self: [])
    monkeypatch.setattr(Frame, "unmanaged_sessions", lambda self: [])
    monkeypatch.setattr(cli, "_conversation_holders", lambda frame, sessions, rows: [])
    monkeypatch.setattr(ConversationHolders, "snapshot", classmethod(lambda cls: [(1, 0, "init")]))
    monkeypatch.setattr(
        cli,
        "_agentless_sessions",
        lambda frame, sessions, rows: [{"session": "STATZ", "driver": "claude", "pane_id": "%2"}],
    )
    monkeypatch.setattr(cli.HookInstaller, "verify", lambda self: [])

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["ok"] is False
    assert result["agent_findings"] and "STATZ" in result["agent_findings"][0]


def test_a_pane_whose_ROOT_process_is_the_driver_is_not_reported(store, monkeypatch):
    """An agent pane is spawned as `sh -c '<argv>'`, and a shell given a simple
    command `exec`s it instead of forking — so the pane's root pid can BE the
    driver, with no child at all.

    Found by mutation: excluding the root passed the whole suite, which meant
    nothing covered this shape. It is the expensive direction to get wrong, because
    flagging healthy sessions is how a verify finding gets ignored.
    """
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "/opt/homebrew/bin/claude --resume abc")]   # exec'd: no child

    assert _agentless(frame, [_agent_session("HIDE")], rows) == []
    assert [d["pid"] for d in ConversationHolders.drivers_in_tree(100, rows)] == [100]


@pytest.mark.parametrize("driver_id", ["someplugin", "unknown-driver", "term", None])
def test_only_drivers_this_can_recognise_are_judged_for_a_missing_agent(
    store, monkeypatch, driver_id
):
    """Reporting "no agent" for a driver there is no way to see flags healthy
    sessions, and a diagnostic that cries wolf gets ignored.

    The package supports declarative and plugin drivers this module knows nothing
    about, and `term` has no driver at all. `antigravity` is deliberately NOT in this
    list — its `agy` executable IS recognisable, so it can be judged; it just never
    carries its conversation in argv, which limits the holder check and not this one.
    """
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent")]        # no driver anywhere in the tree

    session = {"id": "s1", "name": "X", "driver": {"id": driver_id} if driver_id else {}}
    assert _agentless(frame, [session], rows) == [], driver_id


def test_a_claude_session_IS_still_judged(store, monkeypatch):
    """The inverse, so the parametrisation above is not passing by excluding
    everything."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)

    found = _agentless(frame, [_agent_session("STATZ", "claude")], [(100, 1, "sh -c agent")])
    assert [f["session"] for f in found] == ["STATZ"]


def test_a_holder_is_not_reported_when_the_pane_pid_is_unknown(store, monkeypatch):
    """Review finding: without the pane's pid, the session's OWN healthy driver
    cannot be told from an outside one — and reporting it would make this finding
    noise on every healthy session."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: None)
    rows = [(100, 1, "sh -c agent"), (101, 100, f"claude --resume {REF}")]
    sessions = [{"id": "s1", "name": "ALPHA", "driver": {"id": "claude", "resume": {"reference": REF}}}]

    assert _conversation_holders(frame, sessions, rows) == []


def test_a_healthy_antigravity_pane_is_not_reported(store, monkeypatch):
    """Antigravity launches as `agy -c`. Review finding: with `agy` missing from the
    known executables a perfectly healthy tree was reported as having no agent —
    the expensive direction, since flagging working sessions is how a finding gets
    ignored."""
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    rows = [(100, 1, "sh -c agent"), (101, 100, "agy -c")]

    assert _agentless(frame, [_agent_session("DESIGN", "antigravity")], rows) == []
