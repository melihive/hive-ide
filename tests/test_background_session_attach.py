"""A backgrounded session holds its own conversation — and must be attached, not refused.

The holder guard added in 1.0.96 asks "is anything else holding this conversation?"
and refuses a launch when something is. It was right about the fault it was built
for (an abandoned driver at ppid 1) and wrong about a case that looks identical to
it: a session the user deliberately moved into the background.

Claude Code runs a backgrounded session under `claude bg-pty-host`, which is
detached ON PURPOSE so it outlives the terminal that started it. Being reparented
to init is therefore its normal state, not evidence of abandonment. Reading it as
an orphan produced three bad outcomes at once on 2026-10-22, on a live session
(AGENT CHAT) that was busy working the whole time:

1. the recorded error told the user to "stop that process" — i.e. to kill the very
   work they were trying to get back to;
2. the launch was refused, so a session that only needed `claude attach` could not
   be opened at all; and
3. because the refusal raises in the middle of `build`, each retry left a window
   holding only a sidebar pane, with no role or session tags — unselectable, and
   re-probing tmux once a second. Nine accumulated.

So the three things pinned here are: a background host is not an orphan, it does
not refuse a launch, and a failed build leaves no window behind.
"""
from __future__ import annotations

import subprocess

import pytest

from hive_ide.errors import HiveIdeError
from hive_ide.frame import Frame
from hive_ide.holders import ConversationHolders
from hive_ide.store import StateStore

REF = "beaa08ff-f536-44c3-868f-9ae6017ae4ec"

#: The real shape, with the paths made generic. Two details matter: the reference
#: rides on `--session-id` (NOT on the `--resume`, which names a different
#: transcript entirely — the session was forked from it), and ppid is 1.
BG_HOST = (
    "claude bg-pty-host --bg-pty-host /tmp/cc-daemon/abc/pty/beaa08ff.sock 49 34 -- "
    f"/opt/claude/versions/2.1.305 --session-id {REF} --fork-session "
    "--resume /var/lib/projects/forked-from.jsonl --reply-on-resume --permission-mode auto"
)
ABANDONED = f"/opt/claude/bin/claude --resume {REF}"


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def test_a_background_host_is_a_holder_but_not_an_orphan():
    held = ConversationHolders.holders(REF, rows=[(3837949, 1, BG_HOST)])
    assert [h["pid"] for h in held] == [3837949], "it does hold the conversation"
    assert held[0]["background"] is True
    assert held[0]["orphaned"] is False, "ppid 1 is by design for a detached host"


def test_an_abandoned_driver_at_ppid_1_is_still_an_orphan():
    """The original fault must keep reporting as an orphan — this is the case the
    guard exists for, and the exemption above must not widen to cover it."""
    held = ConversationHolders.holders(REF, rows=[(4242, 1, ABANDONED)])
    assert held[0]["background"] is False
    assert held[0]["orphaned"] is True


def test_background_hosts_returns_only_the_background_holders():
    rows = [(3837949, 1, BG_HOST), (4242, 1, ABANDONED)]
    assert [h["pid"] for h in ConversationHolders.holders(REF, rows=rows)] == [3837949, 4242]
    assert [h["pid"] for h in ConversationHolders.background_hosts(REF, rows=rows)] == [3837949]


def test_the_session_id_flag_selects_a_conversation():
    """`--session-id <ref>` is ownership. Without it in the selection vocabulary the
    background host above matched only by accident, through the unrelated `--resume`
    that happens to sit in the same argv."""
    rows = [(10, 1, f"claude --session-id {REF}")]
    assert [h["pid"] for h in ConversationHolders.holders(REF, rows=rows)] == [10]


def test_attach_id_is_the_first_segment_of_the_conversation_id():
    assert ConversationHolders.attach_id(REF) == "beaa08ff"
    assert ConversationHolders.attach_id("nodashes") == "nodashes", "passed through, not cut"


def _claude_record(reference: str) -> dict:
    return {
        "id": "s1",
        "name": "AGENT CHAT",
        "working_dir": "/tmp",
        "driver": {
            "id": "claude",
            "launch_argv": ["claude", "--resume", reference, "--name", "AGENT CHAT"],
            "resume": {"strategy": "conversation_id", "reference": reference},
        },
    }


def test_the_agent_command_attaches_when_the_conversation_is_backgrounded(monkeypatch):
    monkeypatch.setattr(
        ConversationHolders,
        "background_hosts",
        classmethod(lambda cls, ref, **kw: [{"pid": 1, "background": True}]),
    )
    command = Frame._agent_command(_claude_record(REF))
    assert "claude attach beaa08ff" in command
    assert "--resume" not in command, "a second --resume is the thing that blanks the pane"


def test_the_agent_command_resumes_when_no_background_host_is_observed(monkeypatch):
    """Not seeing a host is not evidence there is none, so the ordinary resume stays
    the default — it is the recoverable direction, where a wrong attach reconnects
    to nothing."""
    monkeypatch.setattr(
        ConversationHolders, "background_hosts", classmethod(lambda cls, ref, **kw: [])
    )
    command = Frame._agent_command(_claude_record(REF))
    assert "--resume" in command
    assert "attach" not in command


def test_a_backgrounded_conversation_does_not_refuse_the_launch(store, monkeypatch):
    """The refusal is for a holder that would COLLIDE with a fresh driver. A
    background host is one you reconnect to, so refusing it just locks the user out
    of a session that is running fine."""
    record = store.create_session(
        name="AGENT CHAT",
        working_dir=store.workspace_key,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=_claude_record(REF)["driver"],
    )
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    monkeypatch.setattr(
        ConversationHolders,
        "holders",
        classmethod(
            lambda cls, ref, **kw: [
                {"pid": 3837949, "ppid": 1, "command": BG_HOST,
                 "orphaned": False, "background": True}
            ]
        ),
    )

    frame.refuse_held_conversation(record)      # must not raise


def test_a_failed_build_leaves_no_half_built_window(store, monkeypatch):
    """Otherwise every retry adds an unselectable one-pane window, which is how nine
    of them accumulated for one session."""
    record = store.create_session(
        name="AGENT CHAT",
        working_dir=str(store.workspace_key),
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=_claude_record(REF)["driver"],
    )
    frame = Frame(store, socket="sock")
    calls: list[list[str]] = []

    def fake_tmux(self, argv, **kwargs):
        calls.append(list(argv))
        if argv[0] in ("new-window", "new-session"):
            return subprocess.CompletedProcess(argv, 0, stdout="@7\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(Frame, "tmux", fake_tmux)
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "_refresh_source_if_needed", lambda self, rec, interp: None)
    monkeypatch.setattr(
        Frame,
        "_finish_build",
        lambda self, rec, target, wd: (_ for _ in ()).throw(HiveIdeError("held")),
    )

    with pytest.raises(HiveIdeError):
        frame.build(record)

    assert ["kill-window", "-t", "@7"] in calls, "the window it created must be removed"


def test_verify_does_not_report_a_backgrounded_session_as_a_holder(store, monkeypatch):
    """`verify` folds holder findings into `ok`, so reporting a background host made
    the whole frame unhealthy for using a supported feature — and the remedy it
    prints is "stop that process", which here means killing live work."""
    from hive_ide.cli import _conversation_holders

    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    sessions = [{"id": "s1", "name": "AGENT CHAT", "driver": {"resume": {"reference": REF}}}]
    rows = [(100, 1, "sh -c agent"), (3837949, 1, BG_HOST)]

    assert _conversation_holders(frame, sessions, rows) == []


def test_verify_still_reports_an_abandoned_driver_as_a_holder(store, monkeypatch):
    """The exemption above must not swallow the fault the finding exists for."""
    from hive_ide.cli import _conversation_holders

    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    sessions = [{"id": "s1", "name": "AGENT CHAT", "driver": {"resume": {"reference": REF}}}]
    rows = [(100, 1, "sh -c agent"), (4242, 1, ABANDONED)]

    found = _conversation_holders(frame, sessions, rows)
    assert [f["pid"] for f in found] == [4242]
    assert found[0]["orphaned"] is True
