"""The plan pane picks up a new build's launch flags, and says what it did.

Phase 72 of hive-ide-pkg-03: 1.0.111 shipped `micro -mouse false`, and not one
open plan pane got it. `current_plan` checks for a running viewer BEFORE the
respawn branch, so a pane that already held micro was only ever steered — the
flags could not reach it, and activation meant quitting micro by hand. These
pin the three parts of that: noticing a stale viewer, re-binding a running
tmux server after an install, and not returning silently.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from hive_ide import __version__
from hive_ide.frame import Frame


CURRENT = ["micro", "-readonly", "true", "-mouse", "false", "/p.md"]
BEFORE_1_0_111 = ["micro", "-readonly", "true", "/p.md"]
BEFORE_ANY_FLAGS = ["micro", "/p.md"]


def viewer(monkeypatch, argv, *, editor="micro", pid=4242):
    """A Frame whose plan pane is running `argv`, with no tmux and no store."""
    frame = object.__new__(Frame)
    monkeypatch.setattr(Frame, "_editor_argv", lambda self: [editor])
    monkeypatch.setattr(Frame, "_pane_pid", lambda self, pane: pid)
    monkeypatch.setattr(Frame, "_process_tree", staticmethod(lambda p: [p]))
    monkeypatch.setattr(
        Frame, "_process_command_name",
        staticmethod(lambda p: Path(argv[0]).name if argv else None),
    )
    monkeypatch.setattr(Frame, "_process_argv", staticmethod(lambda p: argv))
    return frame


@pytest.mark.parametrize("argv", [BEFORE_1_0_111, BEFORE_ANY_FLAGS])
def test_a_viewer_missing_a_current_flag_is_stale(monkeypatch, argv):
    assert viewer(monkeypatch, argv)._plan_viewer_is_stale("%1") is True


def test_a_viewer_launched_with_every_current_flag_is_not_stale(monkeypatch):
    assert viewer(monkeypatch, CURRENT)._plan_viewer_is_stale("%1") is False


def test_an_unreadable_cmdline_is_not_stale(monkeypatch):
    """Unknown is not a verdict: a cmdline we cannot read must not kill a pane."""
    frame = viewer(monkeypatch, CURRENT)
    monkeypatch.setattr(Frame, "_process_argv", staticmethod(lambda p: None))
    assert frame._plan_viewer_is_stale("%1") is False


def test_an_unresolvable_pane_pid_is_not_stale(monkeypatch):
    frame = viewer(monkeypatch, BEFORE_1_0_111)
    monkeypatch.setattr(Frame, "_pane_pid", lambda self, pane: None)
    assert frame._plan_viewer_is_stale("%1") is False


def test_a_pane_running_something_other_than_the_viewer_is_not_stale(monkeypatch):
    frame = viewer(monkeypatch, BEFORE_1_0_111)
    monkeypatch.setattr(Frame, "_process_command_name", staticmethod(lambda p: "fish"))
    assert frame._plan_viewer_is_stale("%1") is False


def test_an_editor_with_no_viewer_flags_is_never_stale(monkeypatch):
    """`nano` carries no entry in PLAN_PANE_VIEWER_FLAGS, so there is nothing to miss."""
    frame = viewer(monkeypatch, ["nano", "/p.md"], editor="nano")
    assert frame._plan_viewer_is_stale("%1") is False


def test_an_empty_editor_resolution_is_not_stale(monkeypatch):
    frame = viewer(monkeypatch, CURRENT)
    monkeypatch.setattr(Frame, "_editor_argv", lambda self: [])
    assert frame._plan_viewer_is_stale("%1") is False


class Stamped:
    """A Frame stub that records tmux reads/writes and bind_keys calls."""

    def __init__(self, stamp):
        self.stamp, self.bound = stamp, 0

    def tmux(self, argv):
        assert argv == ["show-option", "-gvq", "@hive_ide_bound_version"]
        return type("R", (), {"stdout": f"{self.stamp}\n"})()

    def bind_keys(self):
        self.bound += 1
        self.stamp = __version__


@pytest.mark.parametrize("stamp", ["", "1.0.110", "0.1.0.dev0"])
def test_a_server_stamped_by_another_build_is_rebound(stamp):
    frame = Stamped(stamp)
    assert Frame.ensure_key_bindings(frame) is True
    assert frame.bound == 1


def test_a_server_already_on_this_build_is_left_alone():
    frame = Stamped(__version__)
    assert Frame.ensure_key_bindings(frame) is False
    assert frame.bound == 0


def test_a_stale_steer_says_how_to_pick_up_the_new_flags():
    said = Frame._plan_steer_say(Path("/p.md"), stale=True)
    assert "/p.md" in said and "--force" in said


def test_a_current_steer_says_what_it_did_without_the_reopen_hint():
    said = Frame._plan_steer_say(Path("/p.md"), stale=False)
    assert "/p.md" in said and "--force" not in said


def test_a_quiet_command_still_prints_its_say_line(capsys, monkeypatch):
    """`plan` is in QUIET_SUCCESS_COMMANDS, so its steer path printed nothing at
    all and read as a no-op. A quiet command may still say what it did."""
    from hive_ide import cli

    monkeypatch.setattr(cli, "_context", lambda args: (_Store(), {}))
    monkeypatch.setattr(
        cli, "build_parser", lambda: _parser_returning(
            {"say": "Steered the plan pane's open viewer.", "opened": "plan-pane"}
        )
    )
    assert cli.main(["plan"]) == 0
    assert capsys.readouterr().out.strip() == "Steered the plan pane's open viewer."


def test_a_quiet_command_with_nothing_to_say_stays_silent(capsys, monkeypatch):
    from hive_ide import cli

    monkeypatch.setattr(cli, "_context", lambda args: (_Store(), {}))
    monkeypatch.setattr(
        cli, "build_parser", lambda: _parser_returning({"opened": "terminal"})
    )
    assert cli.main(["plan"]) == 0
    assert capsys.readouterr().out == ""


def _parser_returning(result):
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.set_defaults(handler=lambda args: result, quiet=False)
    return parser


class _Store:
    """`plan` takes the workspace mutation lock; nothing here needs a real store."""

    @contextlib.contextmanager
    def mutation_lock(self):
        yield


class Pane(Frame):
    """A frame whose plan pane holds a viewer, with every tmux call recorded.

    `current_plan` is the wiring these tests exist for: `_plan_viewer_is_stale`
    being right is worth nothing if the branch that consumes it still steers.
    """

    def __init__(self, monkeypatch, *, verdict, stale):
        from hive_ide import pane_lease

        self.store, self.calls, self.steers, self.bound = _Store(), [], [], 0
        monkeypatch.setattr(pane_lease.LeaseStore, "live", lambda self, sid: None)
        monkeypatch.setattr(pane_lease.PaneLeases, "check", lambda self, sid, force=False: None)
        monkeypatch.setattr(Frame, "plan_path", lambda self, record: Path("/p.md"))
        monkeypatch.setattr(Frame, "role_panes", lambda self, sid: {"plan": "%1"})
        monkeypatch.setattr(Frame, "pane_is_caller", lambda self, pane: verdict)
        monkeypatch.setattr(Frame, "_pane_has_micro", lambda self, pane: True)
        monkeypatch.setattr(Frame, "_plan_viewer_is_stale", lambda self, pane: stale)
        monkeypatch.setattr(Frame, "safe_working_dir", lambda self, record: "/tmp")
        monkeypatch.setattr(Frame, "_environment", lambda self, record, role: [])
        monkeypatch.setattr(Frame, "_plan_command", lambda self, record, line=None: ":")
        monkeypatch.setattr(Frame, "_plan_title", lambda self, record: "plan")
        monkeypatch.setattr(Frame, "ensure_key_bindings", lambda self: self._bind())
        monkeypatch.setattr(Frame, "tmux", lambda self, argv: self._tmux(argv))
        monkeypatch.setattr(
            Frame, "_send_micro_command", lambda self, pane, cmd: self.steers.append(cmd)
        )

    def _bind(self):
        self.bound += 1
        return False

    def _tmux(self, argv):
        self.calls.append(argv)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def run(self, **kwargs):
        return self.current_plan({"id": "s1"}, **kwargs)

    @property
    def respawned(self):
        return any(argv[:1] == ["respawn-pane"] for argv in self.calls)


def test_a_stale_viewer_is_never_replaced_unasked(monkeypatch):
    """Codex P1: README documents editing from a long-lived plan pane
    (`set readonly false`), and that runtime change never reaches the launch
    argv — so a pane that reads stale may hold unsaved edits. Staleness alone
    must not respawn it; it is steered, and says how to pick the flags up."""
    pane = Pane(monkeypatch, verdict=False, stale=True)
    result = pane.run()
    assert result["action"] == "steered" and result["stale_viewer"] is True
    assert "--force" in result["say"]
    assert not pane.respawned and pane.steers == ["set readonly true"]


def test_a_current_viewer_is_steered_not_rebuilt(monkeypatch):
    pane = Pane(monkeypatch, verdict=False, stale=False)
    result = pane.run()
    assert result["action"] == "steered" and result["stale_viewer"] is False
    assert pane.steers == ["set readonly true"] and not pane.respawned


@pytest.mark.parametrize("stale", [True, False])
def test_force_rebuilds_an_open_viewer(monkeypatch, stale):
    pane = Pane(monkeypatch, verdict=False, stale=stale)
    assert pane.run(force=True)["action"] == "respawned"
    assert pane.respawned and pane.steers == []


def test_force_still_refuses_to_rebuild_an_unsettled_pane(monkeypatch):
    """Even asked, a pane that is not provably someone else's is not killed."""
    pane = Pane(monkeypatch, verdict=None, stale=True)
    assert pane.run(force=True)["action"] == "steered"
    assert not pane.respawned


def test_a_stale_viewer_on_an_unsettled_pane_is_steered_and_says_so(monkeypatch):
    """A respawn kills the pane, so an unproven pane is never rebuilt — not even
    to activate flags. It steers, and the say line carries the reopen hint."""
    pane = Pane(monkeypatch, verdict=None, stale=True)
    result = pane.run()
    assert result["action"] == "steered" and result["stale_viewer"] is True
    assert "--force" in result["say"]
    assert not pane.respawned and pane.steers == ["set readonly true"]


def test_opening_the_plan_pane_reaches_a_stale_servers_bindings(monkeypatch):
    pane = Pane(monkeypatch, verdict=False, stale=False)
    pane.run()
    assert pane.bound == 1


def test_the_cli_forwards_force_to_the_viewer(monkeypatch):
    """Codex P2: `--force` promised to rebuild an open viewer but was consumed
    by the lease check and never reached `current_plan`. The frame-level tests
    could not see it, because they call the frame directly."""
    import argparse

    from hive_ide import cli

    seen = {}
    monkeypatch.setattr(cli, "_context", lambda args: (_Store(), {}))
    monkeypatch.setattr(cli, "_current_record", lambda args: (_Store(), {"id": "s1"}))
    monkeypatch.setattr(cli, "configured_registry", lambda config: None)
    monkeypatch.setattr(cli.LeaseStore, "live", lambda self, sid: None)
    monkeypatch.setattr(cli.PaneLeases, "check", lambda self, sid, force=False: None)
    monkeypatch.setattr(
        cli.SessionRepair, "__init__", lambda self, *a, **kw: None
    )
    monkeypatch.setattr(cli.SessionRepair, "repair", lambda self, record: {"ok": True})
    monkeypatch.setattr(_Store, "find_session", lambda self, sid: {"id": sid}, raising=False)
    monkeypatch.setattr(
        cli.Frame, "__init__",
        lambda self, store, socket=None: setattr(self, "store", store),
    )
    monkeypatch.setattr(
        cli.Frame, "current_plan",
        lambda self, record, focus=False, force=False: seen.update(force=force) or {},
    )
    monkeypatch.setattr(cli, "_socket", lambda store, socket: "sock")

    args = argparse.Namespace(
        session_id=None, tmux_socket=None, focus=False, force=True
    )
    cli.cmd_current_plan(args)
    assert seen == {"force": True}
