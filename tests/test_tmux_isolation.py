"""The suite must not spawn tmux servers in this machine's tmux directory.

tmux leaves the socket file behind when its server dies, and `kill-server` in a
test's own teardown does not remove it — so before the session fixture existed,
every real-tmux test dropped one file into `/tmp/tmux-<uid>` permanently. 462 had
accumulated by 2026-10-22, which made the socket listing unusable for reading a
real frame and hid a stale session.

This is the kind of fix that silently stops working: nothing fails when a socket
lands in the wrong directory, it just accrues. So the isolation is asserted, not
assumed.
"""
from __future__ import annotations

import os
from pathlib import Path

from hive_ide.frame import Frame
from hive_ide.socket_gc import SocketReaper
from hive_ide.store import StateStore


def _real_tmux_directory() -> Path:
    return Path("/tmp") / f"tmux-{os.getuid()}"


def test_the_run_has_its_own_tmux_directory():
    assert os.environ.get("TMUX_TMPDIR"), "the session fixture must set TMUX_TMPDIR"
    assert SocketReaper.default_directory() != _real_tmux_directory()


def test_a_frames_socket_lands_outside_the_real_tmux_directory(tmp_path):
    """The path a real server would actually bind, not just the env var."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(StateStore(tmp_path / "state", workspace), socket="hive-ide-isolation")
    socket_path = Path(frame.socket_path())

    assert socket_path.parent != _real_tmux_directory(), socket_path
    assert str(socket_path).startswith(os.environ["TMUX_TMPDIR"]), socket_path


def test_the_socket_path_stays_within_the_unix_limit(tmp_path):
    """The reason the isolated directory is short rather than under pytest's tmp:
    a unix socket path is capped near 108 bytes and tmux appends `tmux-<uid>/<name>`,
    so a long root silently breaks every real-tmux test instead of isolating it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    frame = Frame(
        StateStore(tmp_path / "state", workspace),
        socket="hive-ide-lease-test-deadbeef",
    )
    assert len(frame.socket_path().encode()) < 100, frame.socket_path()


# ── verify reports what the suite used to leave behind ────────────────────────


class _Reply:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _frame(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Frame(StateStore(tmp_path / "state", workspace), socket="hive-ide-halfbuilt")


def test_a_one_pane_untagged_window_is_reported_as_half_built(tmp_path, monkeypatch):
    """The state that made a session unopenable: the window exists, nothing maps the
    session to it, and its sidebar loop probes tmux once a second."""
    frame = _frame(tmp_path)
    listing = (
        "@0\tAGENT CHAT\t\n"          # one pane, no role: the aborted build
        "@1\tHIDE\tsidebar\n"         # a healthy window, three tagged panes
        "@1\tHIDE\tagent\n"
        "@1\tHIDE\tplan\n"
    )
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply(listing))

    assert frame.half_built_windows() == [{"window": "@0", "name": "AGENT CHAT"}]


def test_a_healthy_three_pane_window_is_not_reported(tmp_path, monkeypatch):
    frame = _frame(tmp_path)
    listing = "@1\tHIDE\tsidebar\n@1\tHIDE\tagent\n@1\tHIDE\tplan\n"
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply(listing))

    assert frame.half_built_windows() == []


def test_a_multi_pane_window_whose_first_pane_is_untagged_is_not_half_built(
    tmp_path, monkeypatch
):
    """Half-built means ONE pane, specifically. This separates the pane-count rule
    from the role-tag rule: with an untagged pane listed first, a check that looked
    only at the tag would report a window that has panes beyond the sidebar — and
    the version of this test that put a TAGGED pane first could not tell the two
    rules apart at all."""
    frame = _frame(tmp_path)
    listing = "@2\tPARTIAL\t\n@2\tPARTIAL\tagent\n"
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Reply(listing))

    assert frame.half_built_windows() == []


def test_a_single_pane_window_that_carries_its_role_is_not_half_built(tmp_path, monkeypatch):
    """A tagged pane means the build got far enough to mark it, so whatever else is
    wrong with the window, it is not THIS."""
    frame = _frame(tmp_path)
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Reply("@3\tSOLO\tsidebar\n")
    )

    assert frame.half_built_windows() == []


def test_the_scan_is_scoped_to_this_frames_own_session(tmp_path, monkeypatch):
    """An unmanaged session sharing the socket legitimately has untagged one-pane
    windows, so a socket-wide scan would turn this finding into noise."""
    frame = _frame(tmp_path)
    seen: list = []

    def record(self, args, **kw):
        seen.append(list(args))
        return _Reply("")

    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "tmux", record)
    frame.half_built_windows()

    assert seen, "it must ask tmux something"
    assert "-a" not in seen[0], "socket-wide would include unmanaged sessions"
    assert ["-s", "-t", frame.target] == seen[0][1:4], seen[0]


def test_a_failed_scan_is_unknown_not_empty(tmp_path, monkeypatch):
    """Review finding 3, and the reason this test changed: it used to assert `== []`
    on a failed scan, which encoded the bug — `verify` then read "could not look" as
    "nothing found" and reported a confidently healthy frame on a check that never
    ran. The fake's output is deliberately a listing that WOULD parse into a finding,
    so the assertion observes the returncode check and not an empty string."""
    frame = _frame(tmp_path)
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame,
        "tmux",
        lambda self, args, **kw: _Reply("@0\tAGENT CHAT\t\n", returncode=1),
    )

    assert frame.half_built_windows() is None


def test_scanned_and_found_nothing_is_still_an_empty_list(tmp_path, monkeypatch):
    """The other side of the tri-state: a successful scan with no half-built window
    must stay [] and NOT become the unknown value."""
    frame = _frame(tmp_path)
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame, "tmux", lambda self, args, **kw: _Reply("@1\tHIDE\tsidebar\n")
    )

    assert frame.half_built_windows() == []


def _verify_args(tmp_path):
    import argparse

    return argparse.Namespace(
        workspace_key=str(tmp_path / "workspace"),
        state_home=str(tmp_path / "state"),
        config=str(tmp_path / "config.json"),
        stable_python=None,
    )


def test_verify_refuses_to_call_a_frame_healthy_when_the_pane_scan_failed(
    tmp_path, monkeypatch
):
    """Command level, because that is where the harm was: the finding list is empty
    either way, so only `ok` and `incomplete` can tell a clean frame from an
    unchecked one."""
    from hive_ide import cli

    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "half_built_windows", lambda self: None)

    result = cli.cmd_verify(_verify_args(tmp_path))

    assert result["half_built_findings"] == [], "nothing was found, because nothing ran"
    assert any("half-built" in entry for entry in result["incomplete"]), result["incomplete"]
    assert result["ok"] is False


def test_the_isolation_root_is_private_and_unpredictable():
    """Review finding 1: a predictable name created with `exist_ok=True` let another
    local user pre-create it with `tmux-<uid>` symlinked at this machine's real tmux
    directory — and the session teardown issues `kill-server` on what it finds."""
    import stat as stat_module

    from conftest import _isolation_root

    first, second = _isolation_root(), _isolation_root()
    try:
        assert first is not None and second is not None
        assert first != second, "a predictable name is the vulnerable part"
        mode = stat_module.S_IMODE(first.stat().st_mode)
        assert mode == 0o700, oct(mode)
    finally:
        for path in (first, second):
            if path is not None:
                path.rmdir()


def test_the_isolation_root_prefers_tmp_over_a_long_platform_tempdir(
    monkeypatch, tmp_path
):
    """Review finding 2: `TMPDIR` is long on macOS (`/var/folders/…`), and tmux
    appends `tmux-<uid>/<name>` to whatever this returns, so honouring it pushes a
    real socket path past the ~108-byte unix limit — every real-tmux test then fails
    to start instead of being isolated.

    `tempfile.tempdir` is patched rather than the `TMPDIR` environment variable:
    `gettempdir()` CACHES its answer on first use, so by the time a test runs, an
    env change no longer reaches it. The first version of this test set `TMPDIR` and
    passed against the mutation that reversed the preference — it was asserting on a
    value Python had already frozen.
    """
    import tempfile as tempfile_module

    from conftest import _isolation_root

    long_tmp = tmp_path / ("d" * 90)
    long_tmp.mkdir()
    monkeypatch.setattr(tempfile_module, "tempdir", str(long_tmp))
    assert len(tempfile_module.gettempdir()) > 90, "precondition: the platform dir is long"

    root = _isolation_root()
    try:
        assert root is not None
        assert str(root).startswith("/tmp/"), root
        assert len(str(root).encode()) < 40, root
    finally:
        root.rmdir()


def test_cleanup_never_follows_a_symlink_out_of_the_sandbox(tmp_path, monkeypatch):
    """The teardown must not address anything but a real socket inside its own
    directory, or the planted-symlink route reaches the real tmux directory."""
    from conftest import _kill_servers_under

    root = tmp_path / "sandbox"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "hive-ide-link").symlink_to(outside)
    (root / "hive-ide-plain").write_text("not a socket", encoding="utf-8")

    addressed: list = []
    monkeypatch.setattr(
        "conftest.subprocess.run",
        lambda argv, **kw: addressed.append(list(argv)),
    )

    _kill_servers_under(root)

    assert addressed == [], addressed
