from __future__ import annotations

import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from hive_ide import __version__
from hive_ide.cli import main
from hive_ide.drivers import bundled_drivers
from hive_ide.frame import Frame
from hive_ide.layout import IdeLayout
from hive_ide.store import StateStore


pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")


def _processes_referencing(path) -> list[int]:
    needle = str(path).encode()
    pids: list[int] = []
    try:
        entries = list(os.scandir("/proc"))
    except FileNotFoundError:
        entries = []
    if entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            try:
                with open(os.path.join(entry.path, "cmdline"), "rb") as handle:
                    cmdline = handle.read()
            except OSError:
                continue
            if needle in cmdline:
                pids.append(pid)
        return pids

    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    needle_text = str(path)
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == os.getpid():
            continue
        if needle_text in parts[1]:
            pids.append(pid)
    return pids


def _tmux_sockets_referencing(path) -> set[str]:
    try:
        result = subprocess.run(
            ["pgrep", "-af", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    sockets: set[str] = set()
    for line in result.stdout.splitlines():
        if "tmux -L " not in line:
            continue
        tokens = line.split()
        for index, token in enumerate(tokens[:-1]):
            if token == "-L" and tokens[index + 1].startswith("hive-ide-"):
                sockets.add(tokens[index + 1])
    return sockets


@pytest.fixture(autouse=True)
def _cleanup_tmux_test_processes(tmp_path):
    """Real tmux tests must not leave sidebar/agent loops under pytest tmp dirs.

    `kill-server` handles the happy path. This finalizer is the backstop for assertion
    failures, interrupted release gates, and children that outlive their tmux server.
    It is scoped to the current test's tmp_path so live IDE sessions are never touched.
    """
    yield
    for socket in _tmux_sockets_referencing(tmp_path):
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    time.sleep(0.2)
    pids = _processes_referencing(tmp_path)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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


def test_processes_referencing_falls_back_to_ps_without_proc(tmp_path, monkeypatch):
    def missing_proc(_path):
        raise FileNotFoundError

    def fake_run(args, **kwargs):
        assert args == ["ps", "-axo", "pid=,command="]
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=(
                f" 123 python -m hive_ide.sidebar {tmp_path}\n"
                f" {os.getpid()} python -m pytest {tmp_path}\n"
                " 456 python -m hive_ide.sidebar /elsewhere\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(os, "scandir", missing_proc)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _processes_referencing(tmp_path) == [123]


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
    monkeypatch.delenv("HIVE_IDE_HOST_NAME", raising=False)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_CLIENT", raising=False)
    monkeypatch.setenv("HOSTNAME", "vivo")
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
        assert "client-attached" in hooks
        assert "client-active" not in hooks
        assert "client-focus-in" not in hooks
        assert "after-select-pane" not in hooks
        assert "session-window-changed" in hooks
        client_resized = next(
            line for line in hooks.splitlines() if line.startswith("client-resized")
        )
        assert "run-shell -b" in client_resized
        assert "-m hive_ide.relayout" in client_resized
        assert "--client-width" in client_resized
        assert "#{window_width}" in client_resized
        assert "--client-height" in client_resized
        assert "#{window_height}" in client_resized
        assert "--window-id" in client_resized
        assert "#{window_id}" in client_resized
        assert "run-shell -b" in hooks
        assert "--client-width" in hooks
        assert "#{window_width}" in hooks
        assert "#{window_height}" in hooks
        assert "--window-id" in hooks
        assert "#{window_id}" in hooks
        assert "--client-width '\"'\"'#{client_width}" not in hooks
        assert "--client-height '\"'\"'#{client_height}" not in hooks
        assert (
            frame.tmux(["show-option", "-v", "-t", frame.target, "set-titles"]).stdout.strip()
            == "on"
        )
        assert frame.tmux(["show-option", "-gv", "window-size"]).stdout.strip() == "latest"
        assert (
            frame.tmux(
                ["show-option", "-v", "-t", frame.target, "set-titles-string"]
            ).stdout.strip()
            == "workspace IDE vivo"
        )
        assert (
            frame.tmux(["show-option", "-gv", "set-titles-string"]).stdout.strip()
            == "workspace IDE vivo"
        )
        keys = frame.tmux(["list-keys", "-T", "prefix"]).stdout
        root_keys = frame.tmux(["list-keys", "-T", "root"]).stdout
        for key in (" n ", " p ", " l ", " c ", " e ", " a ", " o ", " i ", " g ", " r ", " k "):
            assert key in keys
        mouse_lines = [
            line for line in root_keys.splitlines()
            if " MouseDown1Pane " in line
        ]
        assert any("select-pane -t = \\; send-keys -M" in line for line in mouse_lines)
        assert "#{@hive_ide_pane},sidebar" not in root_keys
        assert "#{client_width}" in keys
        assert "resize-pane -Z ; select-pane -t .0" in keys
        key_lines = {line.split()[3]: line for line in keys.splitlines() if " -T prefix " in line}
        for key in ("a", "o", "i", "k", "x"):
            assert "run-shell -b" in key_lines[key]
        assert "run-shell -b" in key_lines["g"]
        assert " plan " in key_lines["g"]
        assert ">/dev/null 2>&1" in key_lines["g"]
        assert "hive_ide.popup --kind agent" in keys
        assert "hive_ide.popup --kind options" in keys
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
        frame.tmux(["swap-pane", "-s", f"{alpha_window}.0", "-t", f"{alpha_window}.1"])
        swapped = _pane_rows(frame, alpha_window)
        assert [(index, role) for index, role, _, _ in swapped] == [
            ("0", "agent"),
            ("1", "sidebar"),
            ("2", "plan"),
        ]
        assert main(
            [
                *base,
                "relayout",
                f"--tmux-socket={socket}",
                "--mode=snap",
            ]
        ) == 0
        repaired = _pane_rows(frame, alpha_window)
        assert [(index, role) for index, role, _, _ in repaired] == [
            ("0", "sidebar"),
            ("1", "agent"),
            ("2", "plan"),
        ]
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
        agent_pane = frame.role_panes(alpha["id"])["agent"]
        agent_title = frame.tmux(
            ["display-message", "-p", "-t", agent_pane, "#{@hive_ide_title}|#{pane_title}"]
        )
        assert agent_title.stdout.strip() == "ALPHA RENAMED|ALPHA RENAMED"

        plan_file = workspace / "alpha-plan.md"
        plan_file.write_text("# Alpha Plan\n", encoding="utf-8")
        plan_pane = frame.role_panes(alpha["id"])["plan"]

        def plan_title() -> str:
            return frame.tmux(
                ["display-message", "-p", "-t", plan_pane, "#{@hive_ide_title}"]
            ).stdout.strip()

        plan_args = [*base, "plan-set", f"--session-id={alpha['id']}"]
        assert main(
            [*plan_args, f"--path={plan_file}", f"--tmux-socket={socket}"]
        ) == 0
        assert plan_title() == "Alpha Plan"
        assert main([*plan_args, "--clear", f"--tmux-socket={socket}"]) == 0
        assert plan_title() == "No plan"
        alpha = store.find_session(alpha["id"])

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
        assert rebuilt[alpha["id"]] == alpha_window
        assert rebuilt[beta["id"]] == beta_window
        assert (
            frame.tmux(
                ["display-message", "-p", "-t", alpha_window, "#{window_index}"]
            ).stdout.strip()
            == alpha_index
        )

        assert main(
            [
                *base,
                "force-rebuild",
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


def test_switching_windows_repaints_the_sidebar_before_its_order_can_look_stale(
    tmp_path, monkeypatch
):
    """A backgrounded sidebar keeps its last painted frame, and the list is ordered by
    activity — so the frame goes stale while you are away and switching back shows a
    WRONG ORDER until the keep-alive shell next probes, up to a second later.

    This asserts the order is right within a budget far tighter than that poll, so a
    run against the unfixed code fails instead of settling into a pass. It also pins
    the mechanism: the pane is respawned, so its pid changes on the switch.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    # The frame's hooks run `<python> -m hive_ide.sidebar` as tmux-server children, and
    # a bare interpreter resolves the INSTALLED hive-ide, not this working tree. Without
    # this the test would silently grade the released package instead of the change in
    # front of it — and pass or fail for reasons that have nothing to do with it.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["term"]

    def _make(name: str) -> dict:
        return store.create_session(
            name=name,
            working_dir=workspace,
            source=_source(),
            driver=driver.resolve(
                name=name, working_dir=str(workspace), conversation_reference=None
            ),
        )

    first = _make("ALPHA")
    second = _make("BRAVO")
    frame = Frame(
        store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    )
    try:
        frame.open(no_attach=True)
        windows = frame.windows()
        win_first, win_second = windows[first["id"]], windows[second["id"]]
        for window in (win_first, win_second):
            frame.tmux(["resize-window", "-t", window, "-x", "180", "-y", "40"])

        # BRAVO is displayed long enough to paint, with ALPHA the most recent.
        frame.tmux(["select-window", "-t", win_second])
        time.sleep(1.8)
        stale = frame.tmux(["capture-pane", "-p", "-t", f"{win_second}.0"]).stdout
        assert "ALPHA" in stale and "BRAVO" in stale, stale
        assert stale.index("ALPHA") < stale.index("BRAVO"), stale

        # Leave BRAVO: its process exits and the frame above is frozen on screen.
        frame.tmux(["select-window", "-t", win_first])
        time.sleep(1.2)
        pid_before = frame.tmux(
            ["display-message", "-p", "-t", f"{win_second}.0", "#{pane_pid}"]
        ).stdout.strip()

        # BRAVO becomes the most recent while we are away, so the order must flip.
        record = store.read("sessions", second["id"])
        record["last_active"] = "2099-01-01T00:00:00+00:00"
        store.write("sessions", second["id"], record)
        assert [s["name"] for s in store.list("sessions")][0] == "BRAVO"

        # Sample from the instant of the switch. The assertion that matters is not how
        # fast the right order arrives — it is how long the WRONG one stays readable.
        # The hook cannot be instant (it respawns the pane, which costs an interpreter
        # start), so a sliver at t=0 is expected and harmless. What is not acceptable is
        # the frame still being there a third of a second later. Measured on this
        # machine: 1527ms unfixed, 41ms fixed — so this bound fails the defect and
        # passes the fix with room either side, and a test that simply waited for the
        # right order would pass against the defect.
        STALE_BUDGET_MS = 250
        frame.tmux(["select-window", "-t", win_second])
        started = time.monotonic()
        stale_until_ms = 0.0
        fresh_at_ms = None
        while time.monotonic() - started < 4.0:
            shown = frame.tmux(
                ["capture-pane", "-p", "-t", f"{win_second}.0"]
            ).stdout
            elapsed_ms = (time.monotonic() - started) * 1000
            if "ALPHA" in shown and "BRAVO" in shown:
                if shown.index("BRAVO") < shown.index("ALPHA"):
                    fresh_at_ms = elapsed_ms
                    break
                stale_until_ms = elapsed_ms
            time.sleep(0.01)
        assert stale_until_ms < STALE_BUDGET_MS, (
            f"the pre-switch order was still on screen {stale_until_ms:.0f}ms after the "
            f"switch (budget {STALE_BUDGET_MS}ms): the sidebar was not woken, so the "
            "window is showing the order from the last time it was displayed"
        )
        assert fresh_at_ms is not None, "sidebar never repainted in the new order"
        pid_after = frame.tmux(
            ["display-message", "-p", "-t", f"{win_second}.0", "#{pane_pid}"]
        ).stdout.strip()
        assert pid_after and pid_after != pid_before, (pid_before, pid_after)
    finally:
        frame.tmux(["kill-server"])
