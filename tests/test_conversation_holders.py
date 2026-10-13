"""Who is holding a conversation, and the refusal that depends on it.

`ConversationGuard` answers whether a conversation exists. It cannot say who is
USING one, and that gap froze a session on 2026-10-13: an orphaned `claude` at
ppid 1 still held its conversation, re-opening the frame started a second
`--resume` on it, the pane never painted, and it looked exactly like a hung agent.

Two failure directions matter here and they pull against each other. Missing a
holder recreates that freeze. Inventing one REFUSES a launch the user asked for —
and that is not hypothetical: the first live probe of this module flagged the very
`bash -c` running it, because `~/.claude/` appeared in an unrelated path in its
argv while the conversation reference was one of its arguments. Hence argv[0].
"""
from __future__ import annotations

import os

import pytest

from hive_ide.agents import AgentResumeState
from hive_ide.errors import HiveIdeError, UsageError
from hive_ide.frame import Frame
from hive_ide.holders import ConversationHolders
from hive_ide.store import StateStore

REF = "a1b2c3d4e5f6a7b8"


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def _rows(*entries: tuple[int, int, str]) -> list[tuple[int, int, str]]:
    return list(entries)


def test_a_driver_resuming_the_conversation_is_a_holder():
    rows = _rows((4242, 1, f"/opt/homebrew/bin/claude --resume {REF}"))
    held = ConversationHolders.holders(REF, rows=rows)
    assert [h["pid"] for h in held] == [4242]
    assert held[0]["orphaned"] is True, "ppid 1 is the orphan shape"


@pytest.mark.parametrize(
    "command",
    [
        f"/bin/bash -c 'something /home/kojip/.claude/x {REF}'",
        f"grep -r {REF} /home/kojip/.claude",
        f"python3 -m hive_ide.sidebar --session-id {REF}",
        f"/bin/sh -c 'echo {REF}'",
        f"vim notes-{REF}.md",
        f"tail -f /var/log/{REF}.log",
    ],
)
def test_naming_a_conversation_is_not_holding_it(command):
    """The false-positive direction, which costs a refused launch.

    Every one of these carries the reference, and the first of them is the real
    command that tripped the first version of this module. None of them is a driver:
    the test is argv[0], not a substring of the command line.
    """
    assert ConversationHolders.holders(REF, rows=_rows((99, 1, command))) == [], command


def test_a_reference_too_short_to_match_safely_finds_nothing():
    """A handful of hex characters occurs in unrelated argv by chance, and a false
    holder refuses a launch — so a short reference declines to answer."""
    rows = _rows((7, 1, "claude --resume ab"))
    assert ConversationHolders.holders("ab", rows=rows) == []


def test_the_pane_being_respawned_is_not_its_own_holder():
    """A relaunch deliberately replaces the pane's own agent. Counting it would
    refuse every ordinary respawn."""
    rows = _rows(
        (100, 1, "sh -c agent"),                              # the pane
        (101, 100, f"claude --resume {REF}"),                  # its agent
    )
    assert ConversationHolders.holders(REF, exclude_tree=100, rows=rows) == []
    # Without the exclusion it IS found — otherwise this test proves nothing.
    assert [h["pid"] for h in ConversationHolders.holders(REF, rows=rows)] == [101]


def test_a_holder_deeper_in_the_pane_tree_is_still_excluded():
    rows = _rows(
        (100, 1, "sh -c agent"),
        (101, 100, "fish"),
        (102, 101, f"claude --resume {REF}"),
    )
    assert ConversationHolders.holders(REF, exclude_tree=100, rows=rows) == []


def test_a_holder_outside_the_pane_tree_is_found():
    rows = _rows(
        (100, 1, "sh -c agent"),
        (101, 100, "fish"),
        (900, 1, f"claude --resume {REF}"),     # the orphan
    )
    assert [h["pid"] for h in ConversationHolders.holders(REF, exclude_tree=100, rows=rows)] == [900]


def test_the_calling_process_is_never_its_own_holder():
    rows = _rows((os.getpid(), 1, f"claude --resume {REF}"))
    assert ConversationHolders.holders(REF, rows=rows) == []


def test_an_unreadable_process_table_reports_unobservable_not_empty(monkeypatch):
    """"could not observe" and "nothing holds it" are different answers, and the
    caller has to be able to tell them apart."""
    monkeypatch.setattr(ConversationHolders, "_ps", staticmethod(lambda: []))
    assert ConversationHolders.observable() is False
    assert ConversationHolders.holders(REF) == []


def _session(store, reference: str | None):
    from hive_ide.drivers import bundled_drivers

    driver = bundled_drivers()["term"]
    record = store.create_session(
        name="ALPHA",
        working_dir=store.workspace_key,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=driver.resolve(
            name="ALPHA", working_dir=str(store.workspace_key), conversation_reference=None
        ),
    )
    if reference:
        record["driver"] = {**(record.get("driver") or {}), "resume": {"reference": reference}}
        store.write("sessions", record["id"], record)
    return record


def test_prepare_agent_launch_refuses_when_another_process_holds_the_conversation(
    store, monkeypatch
):
    """The guard lives in `prepare_agent_launch`, which is the ONE thing every driver
    launch goes through.

    Review finding: it was in `respawn_agent` only, and the window `build` launches
    drivers too — so `open`, the path the original fault actually came through, was
    still unguarded. Putting it at the shared choke point covers both.
    """
    record = _session(store, REF)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    monkeypatch.setattr(Frame, "safe_working_dir", lambda self, rec: "/tmp")
    monkeypatch.setattr(
        ConversationHolders,
        "holders",
        classmethod(
            lambda cls, ref, **kw: [
                {"pid": 900, "ppid": 1, "command": "claude", "orphaned": True}
            ]
        ),
    )

    with pytest.raises(HiveIdeError) as excinfo:
        frame.prepare_agent_launch(record)

    assert "pid 900" in str(excinfo.value)
    assert "orphaned" in str(excinfo.value)


def test_respawn_agent_goes_through_prepare_agent_launch(store, monkeypatch):
    """So the guard above cannot be bypassed by this path."""
    record = _session(store, REF)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: False)
    seen: list = []

    def refuse(self, rec):
        seen.append(rec["id"])
        raise HiveIdeError("held")

    monkeypatch.setattr(Frame, "prepare_agent_launch", refuse)

    with pytest.raises(HiveIdeError):
        frame.respawn_agent(record, "%5")
    assert seen == [record["id"]]


def test_a_held_conversation_fails_that_session_in_open(tmp_path, monkeypatch):
    """End to end on the reported path: a held conversation must make `open` REPORT
    the session as failed, not build a window whose agent silently never paints."""
    import os
    import sys
    import uuid

    from hive_ide import __version__
    from hive_ide.drivers import bundled_drivers
    from hive_ide.store import StateStore

    monkeypatch.setenv("SHELL", "/bin/sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["claude"]
    record = store.create_session(
        name="ALPHA",
        working_dir=workspace,
        source={"kind": "stable", "interpreter": sys.executable, "version": __version__},
        driver=driver.resolve(
            name="ALPHA", working_dir=str(workspace), conversation_reference=REF
        ),
    )
    assert AgentResumeState.reference_from_driver(record["driver"]) == REF, record["driver"]

    monkeypatch.setattr(
        ConversationHolders,
        "holders",
        classmethod(
            lambda cls, ref, **kw: [
                {"pid": 900, "ppid": 1, "command": "claude", "orphaned": True}
            ]
        ),
    )
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with pytest.raises(UsageError):
            # Every session failed, so no window could be opened — which is the
            # correct outcome here, and the error names the count.
            frame.open(no_attach=True)
        recorded = store.read("errors", record["id"])
        assert recorded is not None, "open did not record why the session failed"
        assert "already held" in recorded["detail"], recorded["detail"]
    finally:
        frame.tmux(["kill-server"])


def test_respawn_agent_proceeds_when_nothing_holds_the_conversation(store, monkeypatch):
    record = _session(store, REF)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: False)
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: 100)
    monkeypatch.setattr(ConversationHolders, "holders", classmethod(lambda cls, ref, **kw: []))
    monkeypatch.setattr(Frame, "prepare_agent_launch", lambda self, rec: None)
    monkeypatch.setattr(Frame, "safe_working_dir", lambda self, rec: "/tmp")
    monkeypatch.setattr(Frame, "_environment", lambda self, rec, role=None: [])
    monkeypatch.setattr(Frame, "_agent_command", lambda self, rec: "true")

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Ok())

    assert frame.respawn_agent(record, "%5") is True


def test_respawn_agent_proceeds_when_the_session_has_no_conversation(store, monkeypatch):
    """A term session, or a driver that has never resumed, has nothing to collide."""
    record = _session(store, None)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: False)
    probed: list = []
    monkeypatch.setattr(
        ConversationHolders, "holders", classmethod(lambda cls, ref, **kw: probed.append(ref) or [])
    )
    monkeypatch.setattr(Frame, "prepare_agent_launch", lambda self, rec: None)
    monkeypatch.setattr(Frame, "safe_working_dir", lambda self, rec: "/tmp")
    monkeypatch.setattr(Frame, "_environment", lambda self, rec, role=None: [])
    monkeypatch.setattr(Frame, "_agent_command", lambda self, rec: "true")

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Ok())

    assert frame.respawn_agent(record, "%5") is True
    assert probed == [], "no reference means no process scan at all"


def test_respawn_agent_still_refuses_its_own_pane_first(store, monkeypatch):
    """The caller guard comes first: an unprovable pane is left alone without even
    looking at conversations."""
    record = _session(store, REF)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: None)
    probed: list = []
    monkeypatch.setattr(
        ConversationHolders, "holders", classmethod(lambda cls, ref, **kw: probed.append(ref) or [])
    )

    assert frame.respawn_agent(record, "%5") is False
    assert probed == []


@pytest.mark.parametrize(
    "command,expected,why",
    [
        (f"claude --resume {REF} --name X", True, "the real claude resume argv"),
        (f"codex resume -C /tmp {REF}", True, "the real codex resume argv"),
        (
            f"node /x/node_modules/@anthropic-ai/claude-code/cli.js --resume {REF}",
            True,
            "an npm-installed claude, where argv[0] is the interpreter",
        ),
        (f"claude -p Explain transcript {REF}", False, "a PROMPT that quotes the id"),
        (f"codex exec explain {REF}", False, "codex exec rather than resume"),
        (f"claude-helper --resume {REF}", False, "a similarly named non-driver"),
        (f"/bin/bash -c x ~/.claude/y {REF}", False, "the shell that fooled the first cut"),
        (f"grep -r {REF} /home/x/.claude", False, "a grep over the transcripts"),
        (f"claude --resume /tmp/{REF}.json", False, "the id inside a path, not a token"),
        (f"node /x/server.js --resume {REF}", False, "an interpreter with no driver entrypoint"),
    ],
)
def test_only_an_argv_that_SELECTS_the_conversation_is_a_holder(command, expected, why):
    """Both halves of the rule, and why each is needed.

    Carrying a conversation id is not holding it: a driver asked to explain a
    transcript names one while owning nothing — and an agent investigating an
    incident is exactly the process that would. So the argv must also carry a
    recognised selection token. And the executable must be a driver by exact name,
    because the first version matched `claude` anywhere in the command line and
    flagged the very shell running its own probe.
    """
    rows = [(4242, 1, command)]
    assert bool(ConversationHolders.holders(REF, rows=rows)) is expected, why


def test_prepare_agent_launch_does_not_refuse_when_the_pane_pid_is_unknown(
    store, monkeypatch
):
    """Review finding, and the mutation that first passed without this test.

    Without the pane's pid there is no way to exclude the session's own agent, so
    every holder looks external — including the healthy driver this launch is
    replacing. Refusing then blocks a relaunch the user asked for, on no evidence.
    "Could not establish ownership" has to stay distinct from "owned by someone
    else".
    """
    record = _session(store, REF)
    frame = Frame(store, socket="sock")
    monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"agent": "%2"})
    monkeypatch.setattr(Frame, "pane_pid", lambda self, pane: None)
    monkeypatch.setattr(Frame, "safe_working_dir", lambda self, rec: "/tmp")
    probed: list = []
    monkeypatch.setattr(
        ConversationHolders,
        "holders",
        classmethod(
            lambda cls, ref, **kw: probed.append(kw)
            or [{"pid": 900, "ppid": 1, "command": "claude", "orphaned": True}]
        ),
    )

    frame.prepare_agent_launch(record)      # must not raise

    assert probed == [], "it scanned for holders it could not have attributed"
