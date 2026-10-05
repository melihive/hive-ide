"""Every rule that keeps a socket is load-bearing: deleting a live one cuts off
every client attached to that server. Each guard gets its own test."""

from __future__ import annotations

import os
import socket
import stat
import time
from pathlib import Path

import pytest

from hive_ide.socket_gc import SocketReaper


def _dead_socket(directory: Path, name: str, *, age: float = 7200.0) -> Path:
    """A socket file with nothing listening — bound, then closed."""
    path = directory / name
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.close()
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def _live_socket(directory: Path, name: str, *, age: float = 7200.0):
    path = directory / name
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    old = time.time() - age
    os.utime(path, (old, old))
    return server, path


def test_a_dead_socket_of_ours_is_removed(tmp_path):
    path = _dead_socket(tmp_path, "hive-ide-deadbeef")

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert result["removed"] == ["hive-ide-deadbeef"]
    assert not path.exists()


def test_a_live_socket_is_never_removed(tmp_path):
    server, path = _live_socket(tmp_path, "hive-ide-alive111")
    try:
        result = SocketReaper(tmp_path).sweep(apply=True)
        assert result["dead"] == []
        assert path.exists()
    finally:
        server.close()


def test_the_socket_in_use_is_kept_even_when_dead(tmp_path):
    path = _dead_socket(tmp_path, "hive-ide-current1")

    result = SocketReaper(tmp_path).sweep(keep={"hive-ide-current1"}, apply=True)

    assert result["removed"] == []
    assert path.exists()


def test_a_recent_socket_is_kept(tmp_path):
    """A server binding right now must not be caught mid-start."""
    path = _dead_socket(tmp_path, "hive-ide-justnow1", age=5.0)

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert result["dead"] == []
    assert path.exists()


def test_a_foreign_name_is_kept(tmp_path):
    """Only names this package hands out. `ide` is the legacy adapter socket and
    anything else belongs to someone we know nothing about."""
    for name in ("ide", "default", "tmux-1000", "ide-test-123-abc", "hive-ide"):
        _dead_socket(tmp_path, name)

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert result["dead"] == []
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "default",
        "hive-ide",
        "ide",
        "ide-test-123-abc",
        "tmux-1000",
    ]


def test_a_regular_file_or_directory_is_kept(tmp_path):
    """Only sockets. A same-named regular file is not ours to delete."""
    plain = tmp_path / "hive-ide-notasock"
    plain.write_text("", encoding="utf-8")
    folder = tmp_path / "hive-ide-adirector"
    folder.mkdir()
    old = time.time() - 7200
    os.utime(plain, (old, old))
    os.utime(folder, (old, old))

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert result["dead"] == []
    assert plain.exists() and folder.is_dir()


def test_a_symlink_is_kept(tmp_path):
    """lstat, not stat: a symlink pointing at a dead socket is still a symlink,
    and following it would delete the link rather than the thing we examined."""
    target = _dead_socket(tmp_path, "hive-ide-realsock")
    link = tmp_path / "hive-ide-linked11"
    link.symlink_to(target)

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert "hive-ide-linked11" not in result["dead"]
    assert link.is_symlink()


def test_an_unreadable_connect_result_keeps_the_socket(tmp_path, monkeypatch):
    """Only an explicit refusal proves death. Anything else is unknown."""
    path = _dead_socket(tmp_path, "hive-ide-unknown1")
    monkeypatch.setattr(SocketReaper, "is_listening", staticmethod(lambda _p: None))

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert result["dead"] == []
    assert path.exists()


def test_preview_removes_nothing(tmp_path):
    path = _dead_socket(tmp_path, "hive-ide-preview1")

    result = SocketReaper(tmp_path).sweep()

    assert result["dead"] == ["hive-ide-preview1"]
    assert result["removed"] == []
    assert path.exists()


def test_a_missing_directory_is_not_an_error(tmp_path):
    result = SocketReaper(tmp_path / "nope").sweep(apply=True)

    assert result["scanned"] == 0 and result["removed"] == []


def test_the_sweep_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(SocketReaper, "MAX_REMOVALS", 3)
    for index in range(6):
        _dead_socket(tmp_path, f"hive-ide-bound{index:03d}")

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert len(result["removed"]) == 3
    assert len(list(tmp_path.iterdir())) == 3


def test_one_failed_unlink_does_not_stop_the_sweep(tmp_path, monkeypatch):
    _dead_socket(tmp_path, "hive-ide-aaa00001")
    _dead_socket(tmp_path, "hive-ide-bbb00002")
    real = Path.unlink

    def flaky(self, *args, **kwargs):
        if self.name == "hive-ide-aaa00001":
            raise OSError("nope")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert result["removed"] == ["hive-ide-bbb00002"]


def test_is_listening_distinguishes_live_dead_and_unknown(tmp_path):
    dead = _dead_socket(tmp_path, "hive-ide-dead0001")
    assert SocketReaper.is_listening(dead) is False

    server, live = _live_socket(tmp_path, "hive-ide-live0001")
    try:
        assert SocketReaper.is_listening(live) is True
    finally:
        server.close()

    assert SocketReaper.is_listening(tmp_path / "hive-ide-absent01") is None


def test_a_server_that_starts_mid_sweep_is_not_deleted(tmp_path, monkeypatch):
    """The window Codex found: between the refusal probe and the unlink, another
    workspace can bind this very path. tmux binds a NEW socket file, so the inode
    and mtime change — revalidating just before the unlink turns a deleted live
    socket into a skipped entry."""
    path = _dead_socket(tmp_path, "hive-ide-raced001")
    reaper = SocketReaper(tmp_path)
    started: list = []

    real_identity = SocketReaper._identity

    def bind_a_server_then_report(entry: Path):
        # Called from the pre-unlink revalidation: simulate the race by replacing
        # the stale socket with a live one at the same path, exactly as tmux would.
        if not started:
            path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(1)
            started.append(server)
        return real_identity(entry)

    monkeypatch.setattr(
        SocketReaper, "_identity", staticmethod(bind_a_server_then_report)
    )
    try:
        result = reaper.sweep(apply=True)
        assert result["dead"] == ["hive-ide-raced001"]
        assert result["removed"] == []
        assert path.exists()
        assert SocketReaper.is_listening(path) is True
    finally:
        started[0].close()


def test_an_entry_that_vanishes_before_the_unlink_is_skipped(tmp_path, monkeypatch):
    path = _dead_socket(tmp_path, "hive-ide-vanish01")
    real_identity = SocketReaper._identity

    def remove_it_first(entry: Path):
        if entry.name == "hive-ide-vanish01" and entry.exists():
            entry.unlink()
        return real_identity(entry)

    monkeypatch.setattr(SocketReaper, "_identity", staticmethod(remove_it_first))

    result = SocketReaper(tmp_path).sweep(apply=True)

    assert result["removed"] == []
    assert not path.exists()


def test_open_sweeps_only_this_frames_socket_directory(tmp_path, monkeypatch):
    """The sweep must stay inside the directory this frame's socket lives in.

    Taking the directory from a global default let a test that opens a frame on an
    isolated `TMUX_TMPDIR` reach the developer's real tmux directory and delete
    sockets there — observed during this package's own suite.
    """
    from hive_ide.cli import _reap_dead_sockets
    from hive_ide.frame import Frame
    from hive_ide.store import StateStore

    isolated = tmp_path / "isolated" / f"tmux-{os.getuid()}"
    isolated.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere" / f"tmux-{os.getuid()}"
    elsewhere.mkdir(parents=True)
    mine = _dead_socket(isolated, "hive-ide-inscope1")
    theirs = _dead_socket(elsewhere, "hive-ide-offlimit")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path / "isolated"))
    frame = Frame(StateStore(tmp_path / "state", workspace), socket="hive-ide-aaaa1111")

    _reap_dead_sockets(frame, keep={"hive-ide-aaaa1111"})

    assert not mine.exists(), "the frame's own directory should be swept"
    assert theirs.exists(), "no other directory may be touched"
