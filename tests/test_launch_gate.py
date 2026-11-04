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
from hive_ide.launch_gate import IdeLaunchGate, main as gate_main


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
    assert "--delay 4 " in pane and "--label COSTS" in pane
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
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
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


def test_open_hands_each_build_its_own_place_in_the_queue(tmp_path, monkeypatch):
    """The schedule has to REACH `build`, which is the whole point of computing it.

    Pinned separately because every other test here would still pass if `open`
    worked the delay out and then built every window with no delay at all.
    """
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    for index in range(5):
        store.create_session(
            name=f"S{index}",
            working_dir=workspace,
            source={"kind": "stable", "interpreter": sys.executable, "version": "t"},
            driver={"id": "claude", "launch_argv": ["claude"]},
        )
    sessions = store.list("sessions")
    frame = Frame(store, socket="probe")
    handed: list[tuple[str, float]] = []

    monkeypatch.setattr(frame, "require_tmux", lambda: None)
    monkeypatch.setattr(frame, "exists", lambda: False)
    monkeypatch.setattr(frame, "bind_keys", lambda: None)
    monkeypatch.setattr(frame, "_is_sleeping", lambda record: False)
    monkeypatch.setattr(frame, "tmux", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    # `windows()` is empty while the schedule is worked out, then reports what was
    # built — which is what the real frame does as the loop fills it.
    monkeypatch.setattr(frame, "windows", lambda: {sid: f"@{sid}" for sid, _ in handed})

    def ensure(record, *, launch_delay=0.0):
        handed.append((record["id"], launch_delay))
        return True

    monkeypatch.setattr(frame, "ensure", ensure)
    frame.open(no_attach=True)

    assert [delay for _id, delay in handed] == [0.0, 0.0, 0.0, 0.0, 4.0]
    assert handed[0][0] == sessions[0]["id"]
