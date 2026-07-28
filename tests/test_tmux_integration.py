from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from hive_ide import __version__
from hive_ide.cli import main
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.layout import IdeLayout
from hive_ide.store import StateStore


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")


def _source() -> dict:
    return {
        "kind": "stable",
        "interpreter": sys.executable,
        "version": __version__,
    }


def _pane_rows(frame: Frame, window_id: str) -> list[tuple[str, str, str, str]]:
    result = frame.tmux(
        [
            "list-panes",
            "-t",
            window_id,
            "-F",
            "#{pane_index}\t#{@hive_ide_pane}\t#{pane_dead}\t#{pane_start_command}",
        ]
    )
    assert result.returncode == 0, result.stderr
    return [tuple(line.split("\t", 3)) for line in result.stdout.splitlines()]


def _git(directory, *args):
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        check=True,
        text=True,
    )


def test_linked_checkout_status_reaches_the_live_sidebar(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    repo = tmp_path / "repo"
    linked = tmp_path / "linked"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "worktree", "add", "-b", "feature/status", str(linked))

    store = StateStore(tmp_path / "state", repo)
    driver = bundled_drivers()["term"]
    record = store.create_session(
        name="FEATURE",
        working_dir=linked,
        source=_source(),
        driver=driver.resolve(
            name="FEATURE",
            working_dir=str(linked),
            conversation_reference=None,
        ),
    )
    frame = Frame(
        store,
        socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}",
    )
    try:
        frame.open(no_attach=True)
        window = frame.windows()[record["id"]]
        frame.tmux(["resize-window", "-t", window, "-x", "180", "-y", "40"])
        frame.tmux(["resize-pane", "-t", f"{window}.0", "-x", "24"])
        frame.tmux(["resize-pane", "-t", f"{window}.2", "-x", "86"])
        time.sleep(1.7)
        captured = frame.tmux(["capture-pane", "-p", "-e", "-t", f"{window}.0"])
        assert captured.returncode == 0, captured.stderr
        assert "🚢" in captured.stdout
    finally:
        frame.tmux(["kill-server"])


def test_real_tmux_lifecycle_is_id_targeted_and_three_paned(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["term"]
    records = [
        store.create_session(
            name=name,
            working_dir=workspace,
            source=_source(),
            driver=driver.resolve(
                name=name,
                working_dir=str(workspace),
                conversation_reference=None,
            ),
        )
        for name in ("ALPHA", "BETA")
    ]
    alpha, beta = records
    socket = f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    frame = Frame(store, socket=socket)
    base = [
        "--state-home",
        str(store.home),
        "--workspace-key",
        store.workspace_key,
    ]

    try:
        opened = frame.open(no_attach=True)
        assert opened["built"] == [alpha["id"], beta["id"]]
        windows = frame.windows()
        assert set(windows) == {alpha["id"], beta["id"]}
        hooks = frame.tmux(["show-hooks", "-t", frame.target]).stdout
        assert "client-resized" in hooks
        assert "after-select-pane" in hooks
        assert "session-window-changed" in hooks
        assert (
            frame.tmux(["show-option", "-v", "-t", frame.target, "set-titles"]).stdout.strip()
            == "on"
        )
        assert (
            frame.tmux(
                ["show-option", "-v", "-t", frame.target, "set-titles-string"]
            ).stdout.strip()
            == "HIVE IDE workspace"
        )
        keys = frame.tmux(["list-keys", "-T", "prefix"]).stdout
        for key in (" n ", " p ", " l ", " c ", " e ", " a ", " i ", " g ", " r ", " k "):
            assert key in keys
        key_lines = {line.split()[3]: line for line in keys.splitlines() if " -T prefix " in line}
        for key in ("a", "i", "k", "x"):
            assert "run-shell -b" in key_lines[key]
        assert "hive_ide.popup --kind agent" in keys
        assert "hive_ide.popup --kind card" in keys
        assert "hive_ide.popup --kind keys" in keys
        assert "hive_ide.popup --kind error" in keys
        inherited_prefix = frame.tmux(["show-option", "-gv", "prefix"]).stdout.strip()
        frame.settings["keys"] = {
            "prefix": "C-z",
            "bindings": {"next": "N"},
        }
        frame.bind_keys()
        assert frame.tmux(["show-option", "-gv", "prefix"]).stdout.strip() == "C-z"
        changed_keys = frame.tmux(["list-keys", "-T", "prefix"]).stdout
        assert " -T prefix N " in changed_keys
        assert " -T prefix n " not in changed_keys
        frame.settings["keys"] = {"prefix": None, "bindings": {"next": "n"}}
        frame.bind_keys()
        assert (
            frame.tmux(["show-option", "-gv", "prefix"]).stdout.strip()
            == inherited_prefix
        )

        for window_id in windows.values():
            rows = _pane_rows(frame, window_id)
            assert [(index, role, dead) for index, role, dead, _ in rows] == [
                ("0", "sidebar", "0"),
                ("1", "agent", "0"),
                ("2", "plan", "0"),
            ]
            assert f"{sys.executable} -m hive_ide.sidebar" in rows[0][3]
            assert all("unset NO_COLOR;" in row[3] for row in rows)

        frame.tmux(["set-environment", "-g", "NO_COLOR", "1"])
        frame.tmux(["set-option", "-g", "status-format[1]", "diagnostic"])
        frame.tmux(["set-option", "-g", "status-format[2]", "diagnostic"])
        frame.bind_keys()
        assert frame.tmux(["show-environment", "-g", "NO_COLOR"]).returncode != 0
        status_rows = frame.tmux(
            ["show-options", "-g", "status-format"]
        ).stdout.splitlines()
        assert len(status_rows) == 1
        assert status_rows[0].startswith("status-format[0] ")

        alpha_window = windows[alpha["id"]]
        beta_window = windows[beta["id"]]
        frame.tmux(["select-window", "-t", beta_window])
        frame.open(no_attach=True)
        active = frame.tmux(
            ["display-message", "-p", "-t", frame.target, "#{window_id}"]
        ).stdout.strip()
        assert active == beta_window
        frame.tmux(["select-window", "-t", alpha_window])
        frame.tmux(["resize-window", "-t", alpha_window, "-x", "180", "-y", "40"])
        assert main(
            [
                *base,
                "relayout",
                f"--tmux-socket={socket}",
                "--mode=snap",
            ]
        ) == 0
        widths = frame.tmux(
            [
                "list-panes",
                "-t",
                alpha_window,
                "-F",
                "#{pane_width}",
            ]
        ).stdout.splitlines()
        assert widths[0] == str(Frame.SIDEBAR_W)
        assert widths[2] == str(IdeLayout.plan_width(180))
        beta_geometry = frame.tmux(
            [
                "display-message",
                "-p",
                "-t",
                beta_window,
                "#{window_width}x#{window_height}:#{window_zoomed_flag}",
            ]
        ).stdout.strip()
        assert beta_geometry == "180x40:0"
        alpha["name"] = "ALPHA RENAMED"
        store.write("sessions", alpha["id"], alpha)
        assert frame.rename(alpha["id"], alpha["name"])
        assert frame.windows()[alpha["id"]] == alpha_window
        renamed = frame.tmux(
            ["display-message", "-p", "-t", alpha_window, "#{window_name}"]
        )
        assert renamed.stdout.strip() == "ALPHA RENAMED"

        moved = tmp_path / "moved"
        moved.mkdir()
        alpha_index = frame.tmux(
            ["display-message", "-p", "-t", alpha_window, "#{window_index}"]
        ).stdout.strip()
        assert main(
            [
                *base,
                "working-dir-set",
                f"--session-id={alpha['id']}",
                f"--working-dir={moved}",
                f"--tmux-socket={socket}",
            ]
        ) == 0
        rebuilt = frame.windows()
        assert rebuilt[alpha["id"]] != alpha_window
        assert rebuilt[beta["id"]] == beta_window
        beta_window = rebuilt[beta["id"]]
        alpha_window = rebuilt[alpha["id"]]
        assert (
            frame.tmux(
                ["display-message", "-p", "-t", alpha_window, "#{window_index}"]
            ).stdout.strip()
            == alpha_index
        )

        assert main(
            [
                *base,
                "rebuild",
                f"--session-id={alpha['id']}",
                f"--tmux-socket={socket}",
            ]
        ) == 0
        rebuilt_again = frame.windows()
        assert rebuilt_again[alpha["id"]] != alpha_window
        assert rebuilt_again[beta["id"]] == beta_window
        assert (
            frame.tmux(
                [
                    "display-message",
                    "-p",
                    "-t",
                    rebuilt_again[alpha["id"]],
                    "#{window_index}",
                ]
            ).stdout.strip()
            == alpha_index
        )
        assert main(
            [
                *base,
                "relayout",
                f"--tmux-socket={socket}",
                "--mode=snap",
            ]
        ) == 0

        archived = store.archive_session(alpha["id"])
        assert frame.close(alpha["id"])
        assert set(frame.windows()) == {beta["id"]}
        assert archived["id"] == alpha["id"]

        resumed = store.resume_session(alpha["id"])
        assert frame.ensure(resumed)
        assert set(frame.windows()) == {alpha["id"], beta["id"]}
        assert resumed["id"] == alpha["id"]
        assert store.path("sessions", alpha["id"]).is_file()
        assert not store.path("archive", alpha["id"]).exists()
    finally:
        frame.tmux(["kill-server"])


def test_open_isolates_and_reports_a_missing_session_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = tmp_path / "removed-worktree"
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["term"]
    healthy = store.create_session(
        name="HEALTHY",
        working_dir=workspace,
        source=_source(),
        driver=driver.resolve(
            name="HEALTHY",
            working_dir=str(workspace),
            conversation_reference=None,
        ),
    )
    stale = store.create_session(
        name="STALE",
        working_dir=missing,
        source=_source(),
        driver=driver.resolve(
            name="STALE",
            working_dir=str(missing),
            conversation_reference=None,
        ),
    )
    frame = Frame(
        store,
        socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}",
    )

    try:
        opened = frame.open(no_attach=True)
        assert opened["built"] == [healthy["id"]]
        assert opened["windows"] == 1
        assert opened["failed"] == [
            {
                "session_id": stale["id"],
                "name": "STALE",
                "error": f"Session working directory does not exist: {missing}",
            }
        ]
        assert set(frame.windows()) == {healthy["id"]}
        error = store.read("errors", stale["id"])
        assert error is not None
        assert error["component"] == "frame:open"
        assert error["retryable"] is True

        missing.mkdir()
        reopened = frame.open(no_attach=True)
        assert reopened["built"] == [stale["id"]]
        assert reopened["failed"] == []
        assert reopened["windows"] == 2
        assert store.read("errors", stale["id"]) is None
    finally:
        frame.tmux(["kill-server"])
