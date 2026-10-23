from __future__ import annotations

import os
import shlex
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


def _sidebar_running(pane_pid: str) -> bool | None:
    """Is a `hive_ide.sidebar` process running under this keep-alive shell?

    None when the process table cannot be read: unknown, not "no".
    """
    try:
        result = subprocess.run(
            ["pgrep", "-P", pane_pid, "-f", "hive_ide.sidebar"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1):
        return None
    return result.returncode == 0


def _until(predicate, *, timeout: float, interval: float = 0.02):
    """Poll `predicate` until it is truthy or the deadline passes; return its last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def test_switching_windows_shows_the_right_order_and_never_blanks_the_pane(
    tmp_path, monkeypatch
):
    """A window switch must leave a correct sidebar on screen, and must never show
    an empty one on the way there.

    Measured on one machine across the three implementations:

      no wake at all     blank    0ms   stale 1492ms   fresh 1504ms
      wake by respawn    blank  401ms   stale   60ms   fresh  413ms
      wake by painting   blank    0ms   stale   61ms   fresh   73ms

    The earlier version of this test asserted ONLY `stale`, and a blank pane has no
    order in it at all — so its sampling loop skipped every blank frame and scored
    the respawn as a near-perfect 60ms. Clearing the pane was the fastest possible
    way to pass it. That shipped, and the sidebar blinked empty on every switch.

    No assertion here depends on how fast the machine is. The version before this
    one told "woken" from "not woken" with a 250ms stale budget, because the only
    other thing that repaints a backgrounded sidebar is its keep-alive shell, about
    1.5s later. Machine load pushed a working wake past 250ms (259-633ms measured)
    and turned ship verify red. Instead, the keep-alive is FROZEN for BRAVO once it
    is left (its visibility probe answers "hidden" while a freeze file exists), so
    nothing but the wake hook can repaint it. The correct order arriving at all is
    then the proof of the wake; the 5s deadline is only a ceiling on "did it paint",
    not a latency promise — the promise (well under a second) is the table above.

    `blank == 0` and an unchanged pane pid pin the mechanism: painted over, not
    cleared and not respawned.
    """
    monkeypatch.setenv("SHELL", "/bin/sh")
    # The frame's hooks run `<python> -m hive_ide.<module>` as tmux-server children, and
    # a bare interpreter resolves the INSTALLED hive-ide, not this working tree. Without
    # this the test would silently grade the released package instead of the change in
    # front of it — and pass or fail for reasons that have nothing to do with it.
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parent.parent))
    freeze = tmp_path / "freeze-keepalive"
    real_probe = Frame._sidebar_visible_probe()
    monkeypatch.setattr(
        Frame,
        "_sidebar_visible_probe",
        staticmethod(lambda: f"[ ! -e {shlex.quote(str(freeze))} ] && {{ {real_probe}; }}"),
    )
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
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")

    def shown() -> str:
        return frame.tmux(["capture-pane", "-p", "-t", f"{win_second}.0"]).stdout

    def order_is(earlier: str, later: str) -> bool:
        text = shown()
        return earlier in text and later in text and text.index(earlier) < text.index(later)

    def laid_out() -> bool:
        """Has the frame's layout given BRAVO's sidebar its own column yet?

        `resize-window` and the window switch re-lay the frame out from tmux hooks
        that run in the background. Until they have, the sidebar pane can span the
        whole window, and a frame painted then is reflowed away when the layout
        lands — after which a sidebar that has exited cannot repaint it.
        """
        sizes = frame.tmux(
            ["display-message", "-p", "-t", f"{win_second}.0", "#{pane_width} #{window_width}"]
        ).stdout.split()
        return len(sizes) == 2 and all(v.isdigit() for v in sizes) and int(sizes[0]) < int(sizes[1])

    try:
        frame.open(no_attach=True)
        windows = frame.windows()
        win_first, win_second = windows[first["id"]], windows[second["id"]]
        for window in (win_first, win_second):
            frame.tmux(["resize-window", "-t", window, "-x", "180", "-y", "40"])

        # Setup: BRAVO is left showing a complete frame with ALPHA the most recent,
        # and its keep-alive is frozen so only the wake hook can repaint it.
        #
        # The frame a sidebar leaves behind is whatever it last painted, and its last
        # paint can race the switch away: under load it was seen to leave a frame with
        # only one of the two sessions, or none. That is the starting state, not the
        # property under test, so an unusable one is set up again (bounded) rather than
        # scored. Each attempt waits on real conditions only: painted in its laid-out
        # column, then its sidebar process gone.
        frozen = False
        for _attempt in range(5):
            freeze.unlink(missing_ok=True)
            frame.tmux(["select-window", "-t", win_second])
            assert _until(
                lambda: laid_out() and order_is("ALPHA", "BRAVO"), timeout=15.0
            ), shown()
            frame.tmux(["select-window", "-t", win_first])
            freeze.touch()
            pid_before = frame.tmux(
                ["display-message", "-p", "-t", f"{win_second}.0", "#{pane_pid}"]
            ).stdout.strip()
            assert pid_before
            assert _until(lambda: _sidebar_running(pid_before) is False, timeout=15.0), (
                "BRAVO's sidebar process never exited after its window was left"
            )
            if order_is("ALPHA", "BRAVO"):
                frozen = True
                break
        assert frozen, f"BRAVO never kept a complete frame after being left: {shown()!r}"

        # BRAVO becomes the most recent while we are away, so the order must flip.
        record = store.read("sessions", second["id"])
        record["last_active"] = "2099-01-01T00:00:00+00:00"
        store.write("sessions", second["id"], record)
        assert [s["name"] for s in store.list("sessions")][0] == "BRAVO"

        PAINT_CEILING_S = 5.0
        frame.tmux(["select-window", "-t", win_second])
        started = time.monotonic()
        fresh_at_ms = None
        blank_samples: list[float] = []
        while time.monotonic() - started < PAINT_CEILING_S:
            text = shown()
            elapsed_ms = (time.monotonic() - started) * 1000
            has_alpha, has_bravo = "ALPHA" in text, "BRAVO" in text
            if not has_alpha and not has_bravo:
                # Neither name anywhere: the pane is empty. This is the sample the old
                # test discarded, and the whole defect lived in it.
                blank_samples.append(elapsed_ms)
            elif has_alpha and has_bravo and text.index("BRAVO") < text.index("ALPHA"):
                fresh_at_ms = elapsed_ms
                break
            time.sleep(0.01)

        assert not blank_samples, (
            f"the sidebar was EMPTY at {[round(ms) for ms in blank_samples]}ms after "
            f"the switch (last blank {blank_samples[-1]:.0f}ms): the pane was cleared "
            "rather than painted over, which is the regression this test exists for"
        )
        assert fresh_at_ms is not None, (
            f"the pre-switch order was still on screen {PAINT_CEILING_S}s after the "
            "switch, with the keep-alive frozen: the wake hook did not repaint the "
            "sidebar, so the window shows the order from the last time it was displayed"
        )
        # Still frozen: the keep-alive cannot have painted it, and did not start.
        assert _sidebar_running(pid_before) is False
        # The mechanism, pinned without a clock: the pane was painted, not rebuilt.
        pid_after = frame.tmux(
            ["display-message", "-p", "-t", f"{win_second}.0", "#{pane_pid}"]
        ).stdout.strip()
        assert pid_after and pid_after == pid_before, (
            f"the pane was respawned ({pid_before} -> {pid_after}); a respawn clears it, "
            "which is what made the sidebar blink empty on every switch"
        )
    finally:
        frame.tmux(["kill-server"])


def test_a_driver_launch_finds_a_per_user_driver_from_a_minimal_path_client(
    tmp_path, monkeypatch
):
    """The launchd case, on a real server: the client (and so the server it
    starts) has only `/usr/bin:/bin`, and the driver lives in `~/.local/bin`.
    tmux gives the pane the CLIENT's PATH, so only a PATH set inside the pane
    command reaches the driver — through `build` and through `respawn_agent`."""
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    marker = tmp_path / "launched.txt"
    stub = bin_dir / "hive-ide-stub-driver"
    stub.write_text(f'#!/bin/sh\necho "$1" >> {shlex.quote(str(marker))}\n', encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("SHELL", "/bin/sh")
    for var in ("PIPX_BIN_DIR", "PNPM_HOME", "NPM_CONFIG_PREFIX", "TMUX", "TMUX_PANE"):
        monkeypatch.delenv(var, raising=False)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="STUB",
        working_dir=workspace,
        source=_source(),
        driver=bundled_drivers()["term"].resolve(
            name="STUB", working_dir=str(workspace), conversation_reference=None
        ),
    )
    record["driver"]["launch_argv"] = ["hive-ide-stub-driver", "built"]
    store.write("sessions", record["id"], record)
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")

    def launched() -> list[str]:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists():
                lines = marker.read_text(encoding="utf-8").split()
                if lines and lines[-1] == record["driver"]["launch_argv"][-1]:
                    return lines
            time.sleep(0.1)
        return marker.read_text(encoding="utf-8").split() if marker.exists() else []

    try:
        target = frame.build(record)
        assert frame.tmux(["show-environment", "-g", "PATH"]).stdout.strip() == "PATH=/usr/bin:/bin"
        assert launched() == ["built"]

        record["driver"]["launch_argv"] = ["hive-ide-stub-driver", "respawned"]
        pane = frame._role_panes_in_window(target)["agent"]
        assert frame.respawn_agent(record, pane) is True
        assert launched() == ["built", "respawned"]
    finally:
        frame.tmux(["kill-server"])
