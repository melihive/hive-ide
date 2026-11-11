"""An upgrade has to reach the server that is already running.

A tmux server keeps the hooks it was given when it started. `bind_keys` only
runs from `open`, so installing a newer hive-ide updates the CLI and leaves every
live frame on the hooks of whatever version opened it.

That is not a theoretical gap. On 2026-11-10 a release fixed a frame being held
hostage by an idle narrow client, went out to PyPI and all four hosts — and
minutes later the user hit the same bug on a frame whose server dated from
1.0.113. The fix was correct, released, installed, and unreachable. Telling
people "your running frames still have the old hooks" is a warning about a
defect, not a fix for one.

So: `FRAME_HOOKS` is what this version installs, `missing_frame_hooks` says what
a live server lacks, `activate` puts them there without building or respawning
anything, and `verify` reports the gap instead of leaving it to be discovered.
"""
from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from hive_ide import cli
from hive_ide.frame import Frame
from hive_ide.hooks import HookInstaller
from hive_ide.store import StateStore


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


@pytest.fixture
def frame(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Frame(StateStore(tmp_path / "state", workspace), socket="hive-ide-test-a")


def _show_hooks(names: list[str]) -> str:
    return "".join(f"{n}[0] run-shell -b 'x'\n" for n in names)


def test_frame_hooks_lists_every_hook_bind_keys_installs(tmp_path, monkeypatch):
    """The drift guard, and the reason FRAME_HOOKS is data rather than a comment.

    A hook added to `bind_keys` later would otherwise be invisible to the very
    check that exists to notice a stale server — the frame would be missing it
    and nothing would say so.
    """
    store = StateStore(tmp_path / "state", tmp_path)
    frame = Frame(store, socket="fixture")
    calls: list[list[str]] = []

    def tmux(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(frame, "tmux", tmux)
    monkeypatch.setattr(frame, "_module", lambda module, args: f"module={module}")

    frame.bind_keys()

    installed = {
        args[-2] if args[-1].startswith(("run-shell", "set-option", "if-shell")) else args[-1]
        for args in calls
        if args[0] == "set-hook" and "-u" not in args
    }
    installed = {name for name in installed if "-" in name and not name.startswith("-")}
    assert installed, "no hooks were observed at all — the harness is wrong, not the code"
    assert installed <= set(Frame.FRAME_HOOKS), (
        f"bind_keys installs hook(s) FRAME_HOOKS does not list: "
        f"{sorted(installed - set(Frame.FRAME_HOOKS))}"
    )


def test_missing_frame_hooks_names_what_a_stale_server_lacks(frame, monkeypatch):
    """A 1.0.113 server: both election hooks absent, the rest present."""
    present = [h for h in Frame.FRAME_HOOKS if h not in ("client-focus-in", "client-active")]
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Result(_show_hooks(present))
    )

    assert frame.missing_frame_hooks() == ["client-focus-in", "client-active"]


def test_a_current_server_is_missing_nothing(frame, monkeypatch):
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Result(_show_hooks(list(Frame.FRAME_HOOKS)))
    )

    assert frame.missing_frame_hooks() == []


def test_no_frame_running_is_not_a_stale_frame(frame, monkeypatch):
    """Absence is its own state.

    Every machine has workspaces whose frame is not open. Reporting those as
    missing every hook would make a healthy host look broken, and would bury the
    one frame that IS stale among a dozen that simply are not running.
    """
    monkeypatch.setattr(Frame, "exists", lambda self: False)
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: pytest.fail("must not query a dead frame")
    )

    assert frame.missing_frame_hooks() == []


def test_a_failed_show_hooks_reports_nothing_rather_than_everything(frame, monkeypatch):
    """Could-not-read is not an empty server (`unknown-is-not-a-verdict`).

    Taking a failed `show-hooks` at face value would claim every hook is missing
    and send `activate` to rewrite a frame it never managed to inspect.
    """
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Result(_show_hooks(["client-resized"]), 1)
    )

    assert frame.missing_frame_hooks() == []


def test_a_server_that_answers_with_no_hooks_at_all_is_the_stalest_state(
    frame, monkeypatch
):
    """Codex review finding, 2026-11-11. Empty is an ANSWER, not a shrug.

    `show-hooks` succeeding with nothing to list means the server carries no
    hooks at all — the state most in need of repair. Collapsing it into the same
    branch as an unreadable query reported it healthy, so `verify` passed the one
    frame that needed `activate` most.
    """
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Result("", 0))

    assert frame.missing_frame_hooks() == list(Frame.FRAME_HOOKS)


def test_every_hook_named_but_written_by_an_older_version_is_stale(frame, monkeypatch):
    """Codex review finding, 2026-11-11. Names are not behaviour.

    A version that changes what an EXISTING hook runs — as 1.0.117 did, wrapping
    three of them in the attention stamp — leaves every name in place on a
    server that is thoroughly stale. Checking names alone passed it.
    """
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "missing_frame_hooks", lambda self: [])
    monkeypatch.setattr(Frame, "binding_version", lambda self: "1.0.113")

    state = frame.stale_bindings()

    assert state["missing_hooks"] == [], "the premise is that no NAME is missing"
    assert state["stale"] is True, "a server running 1.0.113's hooks read as current"


def test_an_unstamped_server_is_not_called_stale_on_that_alone(frame, monkeypatch):
    """A server predating the stamp answers None, and None is not a mismatch.

    Treating it as one would flag such a frame on every run, forever, with a
    remedy that could not clear it.
    """
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "missing_frame_hooks", lambda self: [])
    monkeypatch.setattr(Frame, "binding_version", lambda self: None)

    assert frame.stale_bindings()["stale"] is False


def test_binding_version_reports_nothing_when_it_cannot_be_read(frame, monkeypatch):
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Result("1.0.113", 1))

    assert frame.binding_version() is None


def test_reinstall_reports_what_it_repaired(frame, monkeypatch):
    state = {"hooks": [h for h in Frame.FRAME_HOOKS if h != "client-active"]}
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "binding_version", lambda self: "1.0.113")
    monkeypatch.setattr(
        Frame, "missing_frame_hooks", lambda self: [
            h for h in Frame.FRAME_HOOKS if h not in state["hooks"]
        ]
    )
    monkeypatch.setattr(
        Frame,
        "bind_keys",
        lambda self: state.__setitem__("hooks", list(Frame.FRAME_HOOKS)),
    )

    result = frame.reinstall_hooks()

    assert result["missing_before"] == ["client-active"]
    assert result["missing_after"] == []
    assert result["was_stale"] is True


def _activate(monkeypatch, tmp_path, *, running: bool, all_workspaces: bool = False):
    elected: list[tuple[str, bool]] = []
    bound: list[str] = []
    workspace = tmp_path / "ws"
    workspace.mkdir()

    monkeypatch.setattr(Frame, "exists", lambda self: running)
    # Stateful on purpose: a constant would model a frame that is stale before
    # AND after the repair, which is a different test (and the one below).
    monkeypatch.setattr(Frame, "bind_keys", lambda self: bound.append(self.socket))
    monkeypatch.setattr(
        Frame, "missing_frame_hooks", lambda self: [] if bound else (["client-active"] if running else [])
    )
    monkeypatch.setattr(
        cli.IdeClients,
        "elect",
        staticmethod(
            lambda home, key, socket, relayout_on_change=False: (
                elected.append((socket, relayout_on_change)) or "/dev/pts/0"
            )
        ),
    )
    monkeypatch.setattr(
        cli.WorkspaceMap,
        "build",
        lambda self, **kw: {"workspaces": [{"workspace_key": str(workspace)}]},
    )
    args = argparse.Namespace(
        state_home=str(tmp_path / "state"),
        workspace_key=str(workspace),
        all=all_workspaces,
    )
    monkeypatch.setattr(
        Frame,
        "stale_bindings",
        lambda self: {
            "running": running,
            "missing_hooks": [] if bound else (["client-active"] if running else []),
            "bound_version": ("9.9.9" if bound else "1.0.113") if running else None,
            "stale": running and not bound,
        },
    )
    return cli.cmd_activate(args), elected, bound


def test_activate_is_serialized_with_other_frame_mutations():
    """Codex review finding, 2026-11-11.

    `activate` runs `bind_keys`, which is a long sequence of tmux writes. Left
    outside the serialized set it could interleave with `open` — one command
    half-way through installing hooks while the other rebuilds the frame — and
    leave bindings partly applied, which is the hardest kind of broken to see:
    every command reported success.
    """
    assert "activate" in cli.WORKSPACE_MUTATIONS


def test_activate_reads_the_workspace_from_the_environment(tmp_path, monkeypatch):
    """Codex review finding, 2026-11-11.

    Inside a frame pane the workspace is in the environment, not the cwd. Reading
    only the cwd re-hooked whatever workspace the shell happened to sit in and
    left the one asked for untouched — the quietest possible failure for a
    command whose whole job is to repair a specific frame.
    """
    intended = tmp_path / "intended"
    intended.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    monkeypatch.setenv("HIVE_IDE_WORKSPACE_KEY", str(intended))
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(Frame, "exists", lambda self: False)

    result = cli.cmd_activate(
        argparse.Namespace(
            state_home=str(tmp_path / "state"), workspace_key=None, all=False
        )
    )

    assert [f["workspace"] for f in result["frames"]] == [str(intended)], (
        f"activate targeted {result['frames']} instead of the workspace named in "
        "the environment"
    )


def test_activate_rehooks_and_elects_a_running_frame(tmp_path, monkeypatch):
    """Electing is half the job.

    A frame that was stuck on an old election is stuck right now; re-hooking it
    without re-electing leaves it correct and still unusable.
    """
    result, elected, bound = _activate(monkeypatch, tmp_path, running=True)

    assert result["running"] == 1
    assert result["repaired"] == 1
    assert bound, "bind_keys never ran, so no hook was installed"
    assert [relayout for _socket, relayout in elected] == [True], (
        "elected without relayout-on-change, so a promoted screen stays zoomed"
    )


def test_activate_does_not_claim_success_when_the_frame_is_still_stale(
    tmp_path, monkeypatch
):
    """Codex review finding, 2026-11-11.

    `bind_keys` does not check the return code of its tmux writes, so a write
    that failed leaves the frame exactly as stale as it found it — with every
    hook NAME present, because those were never the thing that was wrong. Judging
    the repair by names alone reported `ok: true` on a frame it had not repaired,
    which is worse than a command that does nothing: it ends the search.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "bind_keys", lambda self: None)
    monkeypatch.setattr(
        Frame,
        "stale_bindings",
        lambda self: {
            "running": True,
            "missing_hooks": [],          # every NAME is present…
            "bound_version": "1.0.113",   # …and the server is still old
            "stale": True,
        },
    )
    monkeypatch.setattr(
        cli.IdeClients, "elect", staticmethod(lambda *a, **k: "/dev/pts/0")
    )

    result = cli.cmd_activate(
        argparse.Namespace(
            state_home=str(tmp_path / "state"),
            workspace_key=str(workspace),
            all=False,
        )
    )

    assert result["ok"] is False, "claimed success on a frame it did not repair"
    assert result["unrepaired"] == [str(workspace)]
    assert result["repaired"] == 0, "counted a failed repair as a repair"


def test_activate_leaves_a_workspace_with_no_frame_alone(tmp_path, monkeypatch):
    """Most workspaces on a machine have no frame open; none of them is a fault."""
    result, elected, bound = _activate(monkeypatch, tmp_path, running=False)

    assert result["running"] == 0
    assert result["ok"] is True
    assert result["frames"] == [{"workspace": str(tmp_path / "ws"), "running": False}]
    assert not bound and not elected, "touched a workspace with no server"


def test_activate_writes_without_being_asked_twice(tmp_path, monkeypatch):
    """Not a dry run, deliberately — unlike `reap`.

    `reap` waits for `--apply` because it kills processes that may own panes
    somebody is reading. This writes hooks, bindings and options and nothing
    else, so a confirmation step would only be ceremony in the path of a fix.
    """
    result, _elected, bound = _activate(monkeypatch, tmp_path, running=True)

    assert bound, "did nothing without an --apply flag it does not have"
    assert "apply" not in result


def test_verify_reports_a_stale_server_and_fails_on_it(tmp_path, monkeypatch, capsys):
    """The detection half. Without it, a stale frame is found by hitting the bug.

    `HookInstaller.verify` is silenced because it reports on the HARNESS this
    suite runs under, not on anything under test: left alone it holds `ok` false
    by itself, and the assertion below would pass even with the frame hooks left
    out of the verdict entirely.
    """
    # `stale_bindings` is stubbed rather than `exists`: claiming a frame exists
    # when none does makes the pane listing fail, which fills `incomplete` and
    # holds `ok` false by itself — and then this test passes with the frame hooks
    # left out of the verdict entirely. A mutation run caught exactly that.
    monkeypatch.setattr(
        Frame,
        "stale_bindings",
        lambda self: {
            "running": True,
            "missing_hooks": ["client-focus-in", "client-active"],
            "bound_version": "1.0.113",
            "stale": True,
        },
    )
    monkeypatch.setattr(HookInstaller, "verify", lambda self: [])
    monkeypatch.chdir(tmp_path)
    cli.main(["--state-home", str(tmp_path / "state"), "verify"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["frame_hooks_missing"] == ["client-focus-in", "client-active"]
    assert payload["frame_bound_version"] == "1.0.113"
    assert payload["frame_hook_findings"], payload
    assert "hive-ide activate" in payload["frame_hook_findings"][0], (
        "named the problem without naming the remedy"
    )
    # Nothing else may be wrong, or `ok` could be false without the verdict ever
    # counting this finding. Asserted rather than assumed, so a future check that
    # starts firing here fails the test instead of silently masking it.
    others = {
        key: value
        for key, value in payload.items()
        if (key.endswith("findings") or key == "incomplete")
        and key != "frame_hook_findings"
        and value
    }
    assert others == {}, f"something else is also wrong, so `ok` proves nothing: {others}"
    assert payload["ok"] is False


def test_verify_says_nothing_about_a_frame_that_is_not_running(
    tmp_path, monkeypatch, capsys
):
    """Not running is not stale — the inert outcome is no finding."""
    monkeypatch.setattr(Frame, "exists", lambda self: False)
    monkeypatch.setattr(HookInstaller, "verify", lambda self: [])
    monkeypatch.chdir(tmp_path)
    cli.main(["--state-home", str(tmp_path / "state"), "verify"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["frame_hooks_missing"] == []
    assert payload["frame_hook_findings"] == []
