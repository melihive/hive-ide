"""One session, one client.

`open` used to run `tmux attach` with no `-d`, so every invocation left ANOTHER
client attached and nothing ever reaped them. Found 2026-10-13 across the fleet:
two clients on most frames, three on one, the oldest idle for 6.4 days.

That is not untidiness. tmux is configured `window-size latest`, so the window
sizes itself to whichever client was active most recently — a stale 126x33 beside
a live 203x70 makes the window flip geometry and reflow every pane whenever focus
moves between them, which is what people reported as sessions freezing.
"""
from __future__ import annotations

import pytest

from hive_ide.frame import Frame


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def test_attach_detaches_any_client_already_on_the_session(tmp_path, monkeypatch):
    """Without -d a second `open` adds a client instead of taking the session over."""
    from hive_ide.store import StateStore

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    frame = Frame(store, socket="hive-ide-test-attach")

    execed: list[list[str]] = []
    monkeypatch.setattr(
        "hive_ide.frame.os.execvp",
        lambda file, argv: execed.append(list(argv)),
    )
    monkeypatch.setattr(Frame, "require_tmux", lambda self: None)
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "ensure", lambda self, record: False)
    monkeypatch.setattr(Frame, "bind_keys", lambda self: None)
    monkeypatch.setattr(Frame, "windows", lambda self: {"s1": "@1"})
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Result("@1"))
    monkeypatch.setattr(
        store, "list", lambda collection: [{"id": "s1", "name": "ONE"}]
    )

    frame.open()

    assert execed, "open() never attached"
    argv = execed[0]
    assert "attach" in argv
    assert "-d" in argv, (
        "attach without -d leaves the previous client attached; two clients at "
        "different sizes make the window reflow every pane"
    )
    assert argv.index("-d") < argv.index("-t"), "-d must apply to this attach"


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
    rows = "/dev/pts/38\t126\t33\t1791813620\n/dev/pts/0\t203\t70\t1791826822\n"

    clients = _clients(frame, rows, monkeypatch)

    assert [c["client"] for c in clients] == ["/dev/pts/0", "/dev/pts/38"]
    assert [c["size"] for c in clients] == ["203x70", "126x33"]


def test_a_failed_tmux_call_reports_nothing_even_if_it_printed(frame, monkeypatch):
    """Could-not-observe is not a client list.

    tmux can fail AND still have written to stdout — a partial listing, or an error
    on the same stream. Parsing that would invent clients that may not exist, so the
    return code decides, not the emptiness of the output.
    """
    stale = "/dev/pts/9\t203\t70\t1791826822\n"
    monkeypatch.setattr(Frame, "tmux", lambda self, args, **kw: _Result(stale, 1))

    assert frame.attached_clients() == []


def test_a_single_client_is_not_a_finding(frame, monkeypatch):
    clients = _clients(frame, "/dev/pts/0\t203\t70\t1791826822\n", monkeypatch)

    assert len(clients) == 1
