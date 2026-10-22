"""Codex launches must keep command and hook identity local to each IDE pane."""
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
from hive_ide.cli import main
from hive_ide.drivers import bundled_drivers
from hive_ide.store import StateStore


@pytest.mark.parametrize("tail,prompt", [
    ([], None),
    (["resume", "-C", "/a directory", "conversation-id"], None),
    (["resume", "-C", "/a directory", "conversation-id"], "Continue the plan; don't expand $(anything)"),
])
def test_saved_codex_launch_passes_private_process_flag(tmp_path, tail, prompt):
    # Observe the argv at the executable boundary, including legacy stored records.
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "print(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o755)
    record = {"driver": {"id": "codex", "launch_argv": [str(executable), *tail]}}
    if prompt:
        record["handoff"] = {"target_driver_prompt": prompt}
    command = Frame._agent_command(record)
    shell_exit = shutil.which("true")
    assert shell_exit, "The shell fixture requires the platform's true executable"
    env = dict(os.environ, SHELL=shell_exit)
    result = subprocess.run(command, shell=True, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    observed = json.loads(result.stdout.strip().splitlines()[-1])
    assert observed == ["--no-daemon", *tail, *([prompt] if prompt else [])]
    assert record["driver"]["launch_argv"] == [str(executable), *tail]


@pytest.mark.parametrize("flags", [[], ["--no-daemon"]])
def test_terminal_chat_also_uses_private_codex_process(tmp_path, monkeypatch, flags):
    store = StateStore(tmp_path / "state", tmp_path)
    frame = Frame(store)
    record = {"id": "session-a", "working_dir": str(tmp_path),
              "driver": {"id": "codex", "launch_argv": ["codex", *flags, "resume", "thread-a"]}}
    monkeypatch.setattr(frame, "role_panes", lambda _id: {})
    monkeypatch.setattr(frame, "clear_sleep", lambda _record: None)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("hive_ide.frame.subprocess.run", run)
    assert frame.current_chat(record)["opened"] == "terminal"
    assert calls == [(["codex", "--no-daemon", "resume", "thread-a"], {"cwd": str(tmp_path)})]


def test_existing_private_codex_flag_is_not_duplicated():
    record = {"driver": {"id": "codex", "launch_argv": ["codex", "--no-daemon", "resume", "thread-a"]}}
    assert Frame._agent_command(record).count("--no-daemon") == 1


def test_other_drivers_and_custom_wrappers_keep_their_argv():
    for driver, argv in (("claude", ["claude", "--resume", "thread-a"]),
                         ("codex", ["custom-codex-wrapper", "resume", "thread-a"])):
        command = Frame._agent_command({"driver": {"id": driver, "launch_argv": argv}})
        assert "--no-daemon" not in command
        assert shlex.join(argv) in command


def test_cli_resume_rebuild_cannot_remove_private_process_flag(tmp_path, monkeypatch):
    # cmd_current_chat rebuilds saved argv through driver.resolve. Test that real
    # dispatch and the executable boundary, not a hand-constructed resume command.
    observed = tmp_path / "observed.json"
    executable = tmp_path / "codex"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\nfrom pathlib import Path\n"
        f"Path({str(observed)!r}).write_text(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(Frame, "role_panes", lambda *_: {})
    monkeypatch.setattr(Frame, "clear_sleep", lambda *_: None)
    config = tmp_path / "config.json"
    config.write_text("{}")
    store = StateStore(tmp_path / "state", str(tmp_path))
    driver = bundled_drivers()["codex"].resolve(
        name="RESUME", working_dir=str(tmp_path), conversation_reference="fixture-thread"
    )
    driver["launch_argv"] = ["codex", "--no-daemon", "--saved-only-flag"]
    record = store.create_session(name="RESUME", working_dir=str(tmp_path),
                                  source={}, driver=driver)
    assert main(["--state-home", str(store.home), "--workspace-key", str(tmp_path),
                 "--config", str(config), "chat", "--session-id", record["id"]]) == 0
    assert json.loads(observed.read_text()) == [
        "--no-daemon", "resume", "-C", str(tmp_path), "fixture-thread"
    ]
    # It really went through regeneration: persisted argv has neither saved flag.
    assert store.find_session(record["id"])["driver"]["launch_argv"] == [
        "codex", "resume", "-C", str(tmp_path), "fixture-thread"
    ]
