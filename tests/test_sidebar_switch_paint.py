"""Painting a sidebar frame onto a pane's tty, which is how a window switch
repaints without destroying the pane.

`canonical_frame` is the frame an UNFOCUSED sidebar paints — no filter, no archive
browsing, cursor on the window's own session — so it is reproducible from the store
alone, with no sidebar process involved. `paint_tty` sizes it to the target tty and
writes it there in one call.

The "one call" matters: the pane's own sidebar may start a moment later and paint
too, and each `_paint` is a complete frame (home, clear, rows). Two complete frames
in sequence are indistinguishable from one; a frame split across writes could have
the other writer's bytes land in the middle of it.
"""
from __future__ import annotations

import fcntl
import os
import re
import struct
import termios

import pytest

from hive_ide.drivers import bundled_drivers
from hive_ide.sidebar import IdeSidebar
from hive_ide.store import StateStore


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def _make(store, name: str, last_active: str) -> dict:
    driver = bundled_drivers()["term"]
    record = store.create_session(
        name=name,
        working_dir=store.workspace_key,
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver=driver.resolve(
            name=name, working_dir=str(store.workspace_key), conversation_reference=None
        ),
    )
    record["last_active"] = last_active
    store.write("sessions", record["id"], record)
    return record


def _order(text: str, names: list[str]) -> list[str]:
    """The names present, in the order they appear on screen."""
    found = [(text.index(n), n) for n in names if n in text]
    return [n for _i, n in sorted(found)]


def test_canonical_frame_lists_sessions_in_activity_order(store):
    _make(store, "OLDEST", "2020-01-01T00:00:00+00:00")
    middle = _make(store, "MIDDLE", "2021-01-01T00:00:00+00:00")
    _make(store, "NEWEST", "2022-01-01T00:00:00+00:00")

    frame = IdeSidebar.canonical_frame(
        store.home, store.workspace_key, middle["id"], 60, 24
    )

    assert _order(frame, ["NEWEST", "MIDDLE", "OLDEST"]) == [
        "NEWEST",
        "MIDDLE",
        "OLDEST",
    ], frame


def test_canonical_frame_reflects_an_order_change_in_the_store(store):
    """The whole reason the switch repaints: the order changes while you are away."""
    first = _make(store, "ALPHA", "2022-01-01T00:00:00+00:00")
    second = _make(store, "BRAVO", "2021-01-01T00:00:00+00:00")

    before = IdeSidebar.canonical_frame(
        store.home, store.workspace_key, second["id"], 60, 24
    )
    assert _order(before, ["ALPHA", "BRAVO"]) == ["ALPHA", "BRAVO"]

    record = store.read("sessions", second["id"])
    record["last_active"] = "2099-01-01T00:00:00+00:00"
    store.write("sessions", second["id"], record)

    after = IdeSidebar.canonical_frame(
        store.home, store.workspace_key, second["id"], 60, 24
    )
    assert _order(after, ["ALPHA", "BRAVO"]) == ["BRAVO", "ALPHA"], after
    assert first["id"] != second["id"]


ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def _paint_into_pty(store, session_id: str, cols: int, rows: int) -> str:
    """Paint a frame onto a real pty of a given size and return what was written."""
    master, slave = os.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        assert IdeSidebar.paint_tty(
            store.home, store.workspace_key, session_id, os.ttyname(slave)
        ) is True
        os.set_blocking(master, False)
        return os.read(master, 1 << 20).decode(errors="replace")
    finally:
        os.close(master)
        os.close(slave)


def _visible_width(written: str) -> int:
    """Widest row with the escape sequences removed — what the eye sees."""
    return max(len(ANSI.sub("", line)) for line in written.splitlines())


def test_paint_tty_puts_the_frame_on_the_pane(store):
    _make(store, "ALPHA", "2022-01-01T00:00:00+00:00")
    target = _make(store, "BRAVO", "2099-01-01T00:00:00+00:00")

    written = _paint_into_pty(store, target["id"], cols=58, rows=24)

    assert _order(written, ["ALPHA", "BRAVO"]) == ["BRAVO", "ALPHA"], written


def test_paint_tty_sizes_the_frame_to_the_TARGET_pane_not_its_own_stdout(store):
    """The painter runs in a tmux hook process whose stdout is not the pane — and is
    usually not a terminal at all, so `os.get_terminal_size` on its own stdout raises
    and falls back to a 24-column default. That fallback is a plausible-looking frame,
    which is why this asserts the width actually TRACKS the target pane: a narrow and
    a wide pane must not produce the same rows. Sizing from its own stdout passes any
    assertion that only bounds the width from above.
    """
    target = _make(store, "ALPHA", "2022-01-01T00:00:00+00:00")
    _make(store, "BRAVO", "2021-01-01T00:00:00+00:00")

    narrow = _visible_width(_paint_into_pty(store, target["id"], cols=40, rows=24))
    wide = _visible_width(_paint_into_pty(store, target["id"], cols=120, rows=24))

    assert narrow <= 40, f"rows overflowed a 40-column pane: {narrow}"
    assert wide <= 120, f"rows overflowed a 120-column pane: {wide}"
    assert wide > narrow + 20, (
        f"the frame was {narrow} columns wide for a 40-column pane and {wide} for a "
        "120-column one — the width is not coming from the pane it is painting"
    )


def test_paint_tty_writes_the_whole_frame_in_one_call(store, monkeypatch):
    """A complete frame per write, so a sidebar starting a moment later repaints the
    same bytes instead of landing inside this frame."""
    _make(store, "ALPHA", "2022-01-01T00:00:00+00:00")
    target = _make(store, "BRAVO", "2099-01-01T00:00:00+00:00")
    master, slave = os.openpty()
    real_write = os.write
    writes: list[int] = []

    def counting_write(fd, data):
        writes.append(len(data))
        return real_write(fd, data)

    try:
        monkeypatch.setattr(os, "write", counting_write)
        IdeSidebar.paint_tty(
            store.home, store.workspace_key, target["id"], os.ttyname(slave)
        )
        monkeypatch.undo()
        os.set_blocking(master, False)
        os.read(master, 1 << 20)
    finally:
        os.close(master)
        os.close(slave)

    assert len(writes) == 1, f"frame was split across {len(writes)} writes: {writes}"


def test_paint_tty_refuses_a_path_it_cannot_open(store):
    """A pane that vanished mid-switch must raise rather than silently 'succeed' —
    the caller is fail-open and logs nothing, so a False-y success would be
    indistinguishable from a real paint."""
    target = _make(store, "ALPHA", "2022-01-01T00:00:00+00:00")
    with pytest.raises(OSError):
        IdeSidebar.paint_tty(
            store.home, store.workspace_key, target["id"], "/dev/does-not-exist-xyz"
        )
