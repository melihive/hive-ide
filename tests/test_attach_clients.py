"""Attaching keeps the screens already there, and ranks them.

`open` originally ran `tmux attach` with no `-d`, so every invocation left
ANOTHER client attached and nothing ever reaped them. Found 2026-10-13 across the
fleet: two clients on most frames, three on one, the oldest idle for 6.4 days.

That was not untidiness. tmux is configured `window-size latest`, so the window
sizes itself to whichever client was active most recently — a stale 126x33 beside
a live 203x70 makes the window flip geometry and reflow every pane whenever focus
moves between them, which is what people reported as sessions freezing. 1.0.94
answered it with `-d`: one session, one client, and the second screen anyone
actually wanted was thrown off.

Since 2026-11-07 the extra VOTE is what gets removed instead of the extra client
(MIRRORING, `Frame.elect_controller`). These are the unit-level tests, with tmux
mocked; `test_client_mirroring.py` drives real clients on real ptys.
"""
from __future__ import annotations

import pytest

from hive_ide.frame import Frame


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_attach_keeps_the_existing_client_and_demotes_it_first(tmp_path, monkeypatch):
    """No `-d`, and the incumbents are demoted BEFORE the exec.

    Order matters and cannot be fixed afterwards: `execvp` replaces this
    process, so there is no "after the attach" in which to run an election.
    """
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    frame = Frame(store, socket="hive-ide-test-attach")

    events: list[str] = []
    monkeypatch.setattr(
        "hive_ide.frame.os.execvp",
        lambda file, argv: events.append(f"exec:{' '.join(argv)}"),
    )
    monkeypatch.setattr(
        Frame,
        "demote_clients_to_mirrors",
        lambda self: events.append("demote") or [],
    )
    monkeypatch.setattr(Frame, "require_tmux", lambda self: None)
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "ensure", lambda self, record, **_kw: False)
    monkeypatch.setattr(Frame, "bind_keys", lambda self: None)
    monkeypatch.setattr(Frame, "windows", lambda self: {"s1": "@1"})
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Result("@1"))
    monkeypatch.setattr(
        store, "list", lambda collection: [{"id": "s1", "name": "ONE"}]
    )

    frame.open()

    assert events, "open() never attached"
    assert events[0] == "demote", (
        "the incumbents were not demoted before the exec, so whichever screen "
        "was already here keeps a vote on the frame's geometry"
    )
    attach = next(e for e in events if e.startswith("exec:"))
    assert "attach" in attach
    assert " -d" not in attach, (
        "attach still passes -d, which throws the other screen off the session"
    )


def _row(
    name: str,
    width: int,
    height: int,
    activity: int,
    *,
    flags: str = "attached,focused,UTF-8",
    created: int | None = None,
    pid: int = 1000,
) -> str:
    created = activity if created is None else created
    return f"{name}\t{width}\t{height}\t{activity}\t{flags}\t{created}\t{pid}\n"


def _clients(frame: Frame, rows: str, monkeypatch) -> list[dict]:
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Result(rows))
    return frame.attached_clients()


@pytest.fixture
def frame(tmp_path):
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Frame(StateStore(tmp_path / "state", workspace), socket="hive-ide-test-c")


def test_attached_clients_reports_each_client_newest_first(frame, monkeypatch):
    rows = _row("/dev/pts/38", 126, 33, 1791813620) + _row(
        "/dev/pts/0", 203, 70, 1791826822
    )

    clients = _clients(frame, rows, monkeypatch)

    assert [c["client"] for c in clients] == ["/dev/pts/0", "/dev/pts/38"]
    assert [c["size"] for c in clients] == ["203x70", "126x33"]


def test_creation_order_decides_when_activity_ties(frame, monkeypatch):
    """The ordinary case: a second screen attaches within the same second.

    `client_activity` is whole seconds, so it cannot separate them at all. Ranked
    by activity this returned an arbitrary winner, and the arbitrary winner was
    the older, smaller screen.
    """
    tie = 1791826822
    rows = _row("/dev/pts/38", 126, 33, tie, created=tie, pid=400) + _row(
        "/dev/pts/0", 203, 70, tie, created=tie + 1, pid=900
    )

    clients = _clients(frame, rows, monkeypatch)

    assert [c["client"] for c in clients] == ["/dev/pts/0", "/dev/pts/38"]


def test_a_same_second_creation_tie_falls_back_to_pid(frame, monkeypatch):
    """Both timestamps are whole seconds, so creation can tie too.

    A process started later holds the higher pid, which is the last ordering
    signal available before the answer becomes arbitrary again.
    """
    tie = 1791826822
    rows = _row("/dev/pts/38", 126, 33, tie, created=tie, pid=400) + _row(
        "/dev/pts/0", 203, 70, tie, created=tie, pid=900
    )

    clients = _clients(frame, rows, monkeypatch)

    assert [c["client"] for c in clients] == ["/dev/pts/0", "/dev/pts/38"]


def test_a_mirror_is_reported_as_not_controlling(frame, monkeypatch):
    rows = _row(
        "/dev/pts/38", 126, 33, 1791813620, flags="attached,ignore-size,UTF-8"
    ) + _row("/dev/pts/0", 203, 70, 1791826822)

    clients = _clients(frame, rows, monkeypatch)

    by_name = {c["client"]: c["controller"] for c in clients}
    assert by_name == {"/dev/pts/0": True, "/dev/pts/38": False}


def test_a_flag_that_merely_contains_the_name_is_not_ignore_size(frame, monkeypatch):
    """Matched per flag, not as a substring of the whole list."""
    rows = _row("/dev/pts/0", 203, 70, 1791826822, flags="attached,not-ignore-sized")

    clients = _clients(frame, rows, monkeypatch)

    assert clients[0]["controller"] is True


def test_a_failed_tmux_call_reports_nothing_even_if_it_printed(frame, monkeypatch):
    """Could-not-observe is not a client list.

    tmux can fail AND still have written to stdout — a partial listing, or an error
    on the same stream. Parsing that would invent clients that may not exist, so the
    return code decides, not the emptiness of the output.
    """
    stale = _row("/dev/pts/9", 203, 70, 1791826822)
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Result(stale, 1))

    assert frame.attached_clients() == []


def test_a_single_client_is_not_a_finding(frame, monkeypatch):
    clients = _clients(frame, _row("/dev/pts/0", 203, 70, 1791826822), monkeypatch)

    assert len(clients) == 1
