"""`create --prompt`: a session's first instruction rides the driver's launch argv.

Nothing is typed into a pane. The prompt is stored on the record, passed as the
driver's positional prompt argument at the first launch, then dropped so a later
relaunch never repeats it.
"""

from __future__ import annotations

import json
import shlex
import sys

import pytest

from hive_ide.cli import main
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.store import StateStore


PROMPT = "You are a relay probe. Reply `ack <nonce>` when asked."


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": "test"}


def _record(tmp_path, driver_id: str, **extra) -> tuple[StateStore, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="S",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()[driver_id].resolve(
            name="S", working_dir=str(workspace), conversation_reference=None
        ),
        **extra,
    )
    return store, record


def _base(tmp_path) -> list[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ["--state-home", str(tmp_path / "state"), "--workspace-key", str(workspace)]


@pytest.fixture
def drivers_on_path(monkeypatch):
    monkeypatch.setattr("hive_ide.drivers.shutil.which", lambda name: f"/usr/bin/{name}")


@pytest.mark.parametrize("driver_id", ["claude", "codex"])
def test_create_prompt_is_stored_and_reported(tmp_path, capsys, drivers_on_path, driver_id):
    assert main([*_base(tmp_path), "create", "--name=P", f"--driver={driver_id}",
                 f"--prompt={PROMPT}"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["launch_prompt"] == PROMPT


def test_create_without_prompt_stores_none(tmp_path, capsys, drivers_on_path):
    assert main([*_base(tmp_path), "create", "--name=P", "--driver=claude"]) == 0
    assert "launch_prompt" not in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    ("driver_id", "prompt", "problem"),
    [
        ("term", PROMPT, "only the claude and codex drivers"),
        ("antigravity", PROMPT, "only the claude and codex drivers"),
        ("claude", "", "empty"),
        ("claude", "   ", "empty"),
        ("codex", "x" * 4001, "longer than 4000"),
        ("claude", " lead", "whitespace"),
        ("codex", "--dangerously-bypass-approvals-and-sandbox", "must not start with -"),
        ("claude", "-p", "must not start with -"),
        ("claude", "esc \x1b[2J", "control characters"),
        ("codex", "bell\x07", "control characters"),
        ("codex", "del\x7f", "control characters"),
    ],
)
def test_create_refuses_an_unusable_prompt_before_creating(
    tmp_path, capsys, drivers_on_path, driver_id, prompt, problem
):
    base = _base(tmp_path)
    assert main([*base, "create", "--name=P", f"--driver={driver_id}", f"--prompt={prompt}"]) == 2
    assert problem in capsys.readouterr().err
    assert main([*base, "list"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_create_prompt_with_adopt_is_refused(tmp_path, capsys, drivers_on_path):
    assert main([*_base(tmp_path), "create", "--driver=claude", "--adopt",
                 f"--prompt={PROMPT}"]) == 2
    assert "cannot be given to an adopted one" in capsys.readouterr().err


def test_multiline_prompt_is_accepted():
    assert Frame.launch_prompt_problem("line one\nline two\tend", "codex") is None


@pytest.mark.parametrize("driver_id", ["claude", "codex"])
def test_first_launch_passes_the_prompt_as_the_last_argv(tmp_path, driver_id):
    _store, record = _record(tmp_path, driver_id, launch_prompt=PROMPT)
    argv = list(record["driver"]["launch_argv"])
    assert Frame._argv_with_handoff_prompt(argv, record) == [*argv, PROMPT]
    assert shlex.join([*argv, PROMPT]) in Frame._agent_command(record)


def test_no_prompt_leaves_the_launch_argv_alone(tmp_path):
    _store, record = _record(tmp_path, "claude")
    argv = list(record["driver"]["launch_argv"])
    assert Frame._argv_with_handoff_prompt(argv, record) == argv


def test_a_prompt_on_a_driver_that_takes_none_is_never_appended(tmp_path):
    # written around `create`, e.g. by a hand-edited record
    _store, record = _record(tmp_path, "term", launch_prompt=PROMPT)
    argv = list(record["driver"]["launch_argv"])
    assert Frame._argv_with_handoff_prompt(argv, record) == argv


def test_a_stored_unsafe_prompt_is_never_appended(tmp_path):
    _store, record = _record(tmp_path, "codex", launch_prompt="--yolo")
    argv = list(record["driver"]["launch_argv"])
    assert Frame._argv_with_handoff_prompt(argv, record) == argv


def test_a_handoff_prompt_wins_over_the_launch_prompt(tmp_path):
    _store, record = _record(tmp_path, "claude", launch_prompt=PROMPT)
    record["handoff"] = {"target_driver_prompt": "continue the handoff"}
    argv = list(record["driver"]["launch_argv"])
    assert Frame._argv_with_handoff_prompt(argv, record) == [*argv, "continue the handoff"]


def test_the_prompt_is_consumed_once_delivered(tmp_path):
    store, record = _record(tmp_path, "codex", launch_prompt=PROMPT)
    Frame(store)._clear_consumed_handoff(record)
    assert "launch_prompt" not in record
    assert "launch_prompt" not in store.read("sessions", record["id"])
    argv = list(record["driver"]["launch_argv"])
    assert Frame._argv_with_handoff_prompt(argv, record) == argv
    assert PROMPT not in Frame._agent_command(record)


def test_capabilities_advertise_launch_prompt_not_nudge(capsys):
    assert main(["capabilities"]) == 0
    features = json.loads(capsys.readouterr().out)["features"]
    assert "launch-prompt" in features
    assert "nudge" not in features
