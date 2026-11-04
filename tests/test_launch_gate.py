"""A frame of agents must not all start in the same second.

`open` builds every window and launches every driver. On a workspace that has
grown, that burst is what the user experiences as a slow open: the frame is on
screen quickly and then unusable, because the one window they switched to is
competing with every other agent's boot, transcript parse and session-start
hooks.

So every launch except the one the user lands on goes through `launch_gate`,
which holds it for a staggered delay — and releases it the instant that window is
selected, so a queued agent is never something the user waits behind.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hive_ide.frame import Frame
from hive_ide import launch_gate
from hive_ide.launch_gate import IdeLaunchGate, main as gate_main


def _gate_env() -> dict[str, str]:
    """The environment for a real gate process in a test: this package, and no
    inherited IDE session. A test that leaves `HIVE_IDE_*` in place has the gate
    re-check whichever session the developer is sitting in."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("HIVE_IDE_")
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return env


def _schedule(sessions, current_window=None, existing=None, sleeping=()):
    """`Frame._launch_schedule` with tmux and the sleep store stubbed out."""
    frame = Frame.__new__(Frame)
    frame.windows = lambda: dict(existing or {})
    frame._is_sleeping = lambda record: record["id"] in sleeping
    return Frame._launch_schedule(frame, sessions, current_window)


def _sessions(count):
    return [{"id": f"s{index}", "name": f"S{index}"} for index in range(count)]


# ---------------------------------------------------------------- the schedule


def test_the_window_the_user_lands_on_is_never_gated():
    # With no frame yet, `open` selects the first session that gets a window, and
    # the list is activity-ordered — so the most recently worked-in session is the
    # one that must start at once.
    delays = _schedule(_sessions(4))
    assert "s0" not in delays


def test_the_rest_are_staggered_in_batches():
    delays = _schedule(_sessions(11))
    # s0 is the selected one; the other ten queue in batches of LAUNCH_BATCH.
    assert [delays[f"s{index}"] for index in range(1, 11)] == [
        0.0, 0.0, 0.0,
        4.0, 4.0, 4.0,
        8.0, 8.0, 8.0,
        12.0,
    ]


def test_a_session_that_already_has_a_window_takes_no_slot():
    # It is not rebuilt, so it launches nothing. Counting it would leave a real
    # launch waiting out a slot nothing occupies.
    delays = _schedule(_sessions(6), existing={"s1": "@1", "s2": "@2"})
    assert "s1" not in delays and "s2" not in delays
    assert [delays[f"s{index}"] for index in (3, 4, 5)] == [0.0, 0.0, 0.0]


def test_a_sleeping_session_takes_no_slot():
    # A sleeping window starts no driver at all, so there is nothing to stagger.
    delays = _schedule(_sessions(6), sleeping={"s1", "s2"})
    assert "s1" not in delays and "s2" not in delays
    assert [delays[f"s{index}"] for index in (3, 4, 5)] == [0.0, 0.0, 0.0]


def test_the_window_already_on_screen_keeps_priority():
    # Re-opening an existing frame must not gate behind the window the user is
    # already sitting in; it is where they will still be when `open` returns.
    delays = _schedule(
        _sessions(5), current_window="@7", existing={"s3": "@7"}
    )
    assert "s3" not in delays
    # s0 is NOT the selected one here, so it queues like the others.
    assert delays["s0"] == 0.0


def test_a_terminal_session_takes_no_slot():
    # Its agent pane is a shell. Nothing to wait for, and a queue notice in front
    # of a prompt would just be wrong.
    sessions = _sessions(5)
    sessions[1]["driver"] = {"id": "term"}
    sessions[2]["driver"] = {"id": "claude"}
    delays = _schedule(sessions)
    assert "s1" not in delays
    assert delays["s2"] == 0.0


def test_an_empty_workspace_schedules_nothing():
    assert _schedule([]) == {}


# ------------------------------------------------------------------- the argv


def _frame_for_argv(tmp_path):
    frame = Frame.__new__(Frame)
    frame.python = sys.executable
    frame._launch_path = lambda: "/usr/bin:/bin"
    return frame


def test_a_gated_pane_runs_the_driver_through_the_gate(tmp_path):
    argv = Frame._driver_argv(
        _frame_for_argv(tmp_path), "claude --resume x", delay=4.0, label="COSTS"
    )
    assert argv[-3:-1] == ["sh", "-c"]
    pane = argv[-1]
    assert "hive_ide.launch_gate" in pane
    assert "--delay=4" in pane and "--label=COSTS" in pane
    # The driver command itself is handed over untouched, after the separator —
    # and again as the fallback, so each branch of the pane command runs it once.
    assert "-- sh -c 'claude --resume x'" in pane
    assert pane.endswith("exec sh -c 'claude --resume x'")
    assert pane.count("claude --resume x") == 2


def test_a_gate_that_cannot_start_still_starts_the_driver(tmp_path):
    """Version skew must cost a stagger, never an agent.

    A session pinned to another package source has an interpreter that cannot
    import this gate. Proven end to end with an interpreter that fails every
    call, so the probe fails exactly as a skewed one would.
    """
    broken = tmp_path / "python-without-the-gate"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    marker = tmp_path / "it-ran"
    pane = Frame._gated_pane_command(
        _frame_for_argv(tmp_path),
        f"touch {shlex.quote(str(marker))}",
        4.0,
        "SKEWED",
        str(broken),
    )
    result = subprocess.run(["sh", "-c", pane], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "the driver never ran when the gate could not start"


def test_an_ungated_launch_has_no_gate_in_front_of_it(tmp_path):
    # A relaunch of ONE pane (`wake`, `chat`) is the user asking for that agent
    # now. Gating it would be a delay nobody asked for.
    argv = Frame._driver_argv(_frame_for_argv(tmp_path), "claude")
    assert "hive_ide.launch_gate" not in argv
    assert argv[-3:] == ["sh", "-c", "claude"]


# -------------------------------------------------------------------- the gate


def test_it_waits_its_delay_out():
    elapsed = [0.0]
    gate = IdeLaunchGate(
        socket="", pane="", sleep=lambda s: elapsed.__setitem__(0, elapsed[0] + s),
        now=lambda: elapsed[0],
    )
    assert gate.wait(4.0) == "queued"
    assert elapsed[0] == pytest.approx(4.0)


def test_selecting_the_window_jumps_the_queue():
    # The worst case for a user who switches to a queued window is one poll.
    polls = []

    def run(argv, **kwargs):
        polls.append(argv)
        return SimpleNamespace(returncode=0, stdout="1\n")

    gate = IdeLaunchGate(
        socket="sock", pane="%4", sleep=lambda s: None, now=lambda: 0.0, run=run
    )
    assert gate.wait(60.0) == "selected"
    assert polls and "#{window_active}" in polls[0]
    assert "%4" in polls[0] and "sock" in polls[0]


def test_an_inactive_window_does_not_jump_the_queue():
    clock = [0.0]
    gate = IdeLaunchGate(
        socket="sock", pane="%4",
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        now=lambda: clock[0],
        run=lambda *a, **k: SimpleNamespace(returncode=0, stdout="0\n"),
    )
    assert gate.wait(2.0) == "queued"


def test_an_unreadable_tmux_is_not_read_as_the_user_arriving():
    # Treating unknown as "active" would release every gate at once — the exact
    # burst this exists to prevent. The delay still bounds the wait.
    clock = [0.0]

    def explode(*_args, **_kwargs):
        raise OSError("no tmux here")

    gate = IdeLaunchGate(
        socket="sock", pane="%4",
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        now=lambda: clock[0], run=explode,
    )
    assert gate.window_is_active() is False
    assert gate.wait(1.0) == "queued"


@pytest.mark.parametrize("delay", ["0", "", "nonsense", "-5", "nan", "100000", None])
def test_a_delay_that_is_not_a_sane_wait_means_start_now(delay):
    gate = IdeLaunchGate(socket="", pane="", sleep=lambda s: None, now=lambda: 0.0)
    assert gate.wait(delay) == "immediate"
    assert gate.will_wait(delay) is False


def test_the_gate_execs_the_command_it_was_given(tmp_path):
    # End to end through a real process: the gate must REPLACE itself with the
    # driver, so the pane's own process becomes the driver and every downstream
    # pane inspection sees what it would have seen with no gate.
    probe = tmp_path / "driver"
    probe.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    probe.chmod(0o755)
    result = subprocess.run(
        [
            sys.executable, "-m", "hive_ide.launch_gate",
            "--delay", "0", "--label", "PROBE",
            "--", str(probe), "--resume", "thread-a",
        ],
        capture_output=True, text=True, timeout=30,
        env=_gate_env(),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["--resume", "thread-a"]


def test_the_gate_still_execs_when_its_own_wait_blows_up(monkeypatch, tmp_path):
    # Fail open, always: a gate that could refuse to exec is a way to lose an
    # agent, and no stagger is worth that.
    def explode(self, delay):
        raise RuntimeError("the gate itself is broken")

    monkeypatch.setattr(IdeLaunchGate, "wait", explode)
    execs = []
    monkeypatch.setattr(os, "execvp", lambda f, a: execs.append((f, a)))
    assert gate_main(["--delay", "9", "--", "claude", "--resume", "x"]) == 1
    assert execs == [("claude", ["claude", "--resume", "x"])]


def test_the_gate_reports_a_missing_command_instead_of_execing_nothing(capsys):
    assert gate_main(["--delay", "0", "--"]) == 2
    assert "no command" in capsys.readouterr().err


# --------------------------------------------------------------- open's wiring


def test_the_cli_stages_the_launches_repair_would_otherwise_start_at_once(
    tmp_path, monkeypatch
):
    """Through `cmd_open`, not `Frame.open` — because the CLI repairs FIRST.

    Repair builds any window that is missing, so a launch order decided inside
    `open` is decided over a frame that repair has already built in full: the
    schedule comes out empty and the whole frame starts at once. A test that
    calls `Frame.open` directly never sees that, which is how it was missed.
    """
    from hive_ide import cli
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    for index in range(6):
        store.create_session(
            name=f"S{index}",
            working_dir=workspace,
            source={"kind": "stable", "interpreter": sys.executable, "version": "t"},
            driver={"id": "claude", "launch_argv": ["claude"]},
        )
    handed: list[tuple[str, float]] = []
    windows: dict[str, str] = {}

    def build(self, record, *, force=False, no_driver=False, launch_delay=0.0):
        handed.append((record["name"], launch_delay))
        windows[record["id"]] = f"@{len(windows)}"
        return windows[record["id"]]

    monkeypatch.setattr(Frame, "build", build)
    monkeypatch.setattr(Frame, "windows", lambda self: dict(windows))
    monkeypatch.setattr(Frame, "exists", lambda self: bool(windows))
    monkeypatch.setattr(Frame, "require_tmux", lambda self: None)
    monkeypatch.setattr(Frame, "bind_keys", lambda self: None)
    monkeypatch.setattr(Frame, "role_panes", lambda self, session_id: {})
    monkeypatch.setattr(
        Frame, "tmux",
        lambda self, args, **kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(cli, "_reap_dead_sockets", lambda frame, keep: None)

    args = SimpleNamespace(
        workspace_key=str(workspace), state_home=str(tmp_path / "state"),
        config=str(tmp_path / "config.json"), tmux_socket="probe", no_attach=True,
        name=None, driver=None, working_dir=None, plan=None, source=None,
        stable_python=None,
    )
    cli.cmd_open(args)

    assert len(handed) == 6, f"every window should be built once: {handed}"
    # The one the user lands on plus a first batch start together; the rest wait.
    assert [delay for _name, delay in handed] == [0.0, 0.0, 0.0, 0.0, 4.0, 4.0]


def test_a_session_with_a_one_shot_instruction_is_never_queued():
    """Its prompt lives only in the pane once `build` has run.

    `_finish_build` drops the handoff and the launch prompt from the record as
    soon as the pane holds them, so a gate's wait is a window in which the queued
    process is the only copy left — kill the pane then and the instruction is
    gone. It is also the session someone has just set up.
    """
    sessions = _sessions(6)
    sessions[1]["handoff"] = {"target_driver_prompt": "continue the plan"}
    sessions[2]["launch_prompt"] = "you are a relay probe"
    delays = _schedule(sessions)
    assert "s1" not in delays and "s2" not in delays
    assert [delays[f"s{index}"] for index in (3, 4, 5)] == [0.0, 0.0, 0.0]


def test_verify_does_not_call_a_waiting_gate_a_dead_agent(tmp_path, monkeypatch):
    """A queued pane is Python with no driver under it — so is a dead agent.

    `verify` reporting `ok: false` for the frame's own deliberate stagger would
    make every staggered open look broken.
    """
    from hive_ide import cli
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    frame = Frame(store, socket="probe")
    session = {"id": "s1", "name": "QUEUED", "driver": {"id": "claude", "launch_argv": ["claude"]}}
    monkeypatch.setattr(frame, "role_panes", lambda _id: {"agent": "%3"})
    monkeypatch.setattr(frame, "pane_pid", lambda _pane: 4242)

    waiting = [(4242, 1, "/usr/bin/python3 -m hive_ide.launch_gate --delay=8 -- sh -c claude")]
    assert cli._agentless_sessions(frame, [session], waiting) == []

    # The same pane with the gate gone and no driver IS a finding — otherwise
    # this test would pass on a check that reports nothing at all.
    dead = [(4242, 1, "sh -c claude")]
    assert [f["session"] for f in cli._agentless_sessions(frame, [session], dead)] == ["QUEUED"]


def test_a_session_named_like_a_flag_still_starts(tmp_path):
    """`--label=<name>`, not `--label <name>`.

    A session may be called `--work`. As a separate word that name parses as
    another flag, argparse exits 2 — and the shell has already `exec`ed the gate
    by then, so the fallback is gone with it and the agent never starts.
    """
    marker = tmp_path / "it-ran"
    pane = Frame._gated_pane_command(
        _frame_for_argv(tmp_path),
        f"touch {shlex.quote(str(marker))}",
        0.01,
        "--work",
        sys.executable,
    )
    result = subprocess.run(
        ["sh", "-c", pane], capture_output=True, text=True, timeout=30, env=_gate_env(),
    )
    assert result.returncode == 0, result.stderr
    assert marker.exists(), "a flag-shaped session name cost the session its agent"


def test_ctrl_c_while_queued_starts_the_agent_rather_than_losing_it(monkeypatch):
    # KeyboardInterrupt is not an Exception, so it used to escape the fail-open
    # and close the pane. A user interrupting a wait wants the agent sooner.
    def interrupt(self, delay):
        raise KeyboardInterrupt

    monkeypatch.setattr(IdeLaunchGate, "wait", interrupt)
    execs = []
    monkeypatch.setattr(os, "execvp", lambda f, a: execs.append((f, a)))
    assert gate_main(["--delay=9", "--", "claude", "--resume", "x"]) == 1
    assert execs == [("claude", ["claude", "--resume", "x"])]


def test_a_holder_appearing_during_the_wait_stops_the_launch(monkeypatch):
    """The wait widened an existing race, so the guard runs again at the end of it.

    `prepare_agent_launch` cleared the conversation before the window was built.
    Starting the driver seconds later, after something else resumed that
    conversation, is the duplicate-driver fault the guard exists for.
    """
    monkeypatch.setattr(IdeLaunchGate, "wait", lambda self, delay: "queued")
    monkeypatch.setattr(
        launch_gate, "held_conversation_reason",
        lambda: "its conversation is held by pid 4242",
    )
    execs = []
    monkeypatch.setattr(os, "execvp", lambda f, a: execs.append((f, a)))
    assert gate_main(["--delay=4", "--", "claude", "--resume", "x"]) == 1
    assert execs and execs[0][0] == "sh", "the driver was started on a held conversation"
    assert "claude" not in " ".join(execs[0][1])


def test_an_unheld_conversation_launches_as_normal(monkeypatch):
    monkeypatch.setattr(IdeLaunchGate, "wait", lambda self, delay: "queued")
    monkeypatch.setattr(launch_gate, "held_conversation_reason", lambda: None)
    execs = []
    monkeypatch.setattr(os, "execvp", lambda f, a: execs.append((f, a)))
    assert gate_main(["--delay=4", "--", "claude", "--resume", "x"]) == 1
    assert execs == [("claude", ["claude", "--resume", "x"])]


def test_the_ownership_recheck_fails_open(monkeypatch, tmp_path):
    """Every way of not knowing means launch: a missed collision costs one
    confusing pane, a false refusal costs the user an agent for nothing."""
    monkeypatch.delenv("HIVE_IDE_STATE_HOME", raising=False)
    monkeypatch.delenv("HIVE_IDE_WORKSPACE_KEY", raising=False)
    monkeypatch.delenv("HIVE_IDE_SESSION_ID", raising=False)
    assert launch_gate.held_conversation_reason() is None


def test_a_queued_pane_refuses_typed_input_even_with_force(monkeypatch, tmp_path):
    """`driver-rename --force` typed `/rename` into a gate and called it sent.

    `pane_gate` only checked for a driver when the pane ran a shell, and a queued
    pane runs Python — so it read as a live agent and the input went nowhere a
    driver could receive it.
    """
    from hive_ide.nudge import AgentNudge
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace), socket="probe")
    gate = AgentNudge(frame)
    monkeypatch.setattr(
        gate, "pane_facts",
        lambda pane_id: {"in_mode": False, "command": "python3", "dead": False, "pid": "77"},
    )
    monkeypatch.setattr(
        gate, "_ps",
        lambda args: "/usr/bin/python3 -m hive_ide.launch_gate --delay=8 -- sh -c claude\n",
    )
    allowed, reason, _detail = gate.pane_gate("%7")
    assert allowed is False
    assert reason == "agent_queued"
