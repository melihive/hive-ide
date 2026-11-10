"""Coming back to a screen puts it back in charge of the frame.

Reported 2026-11-10: a 49x44 client and a 203x68 one, both attached, the narrow one idle for 4.3 hours and still sizing the window. The
window therefore sat at 49 columns -- below the mobile threshold, so the ladder
zoomed it to a single pane -- and returning to the wide screen changed nothing.

Three things had to line up for that to be unrecoverable:

  * the election ranked clients by CREATION, so the narrow screen won for having
    attached second and kept winning for as long as it stayed attached;
  * the only elections ran on `client-attached` and `client-detached`, and coming
    back to a screen that never left fires neither;
  * `client-resized` could not stand in: the wide terminal's own size never
    changed, only which client tmux was listening to.

So the fix is both halves -- rank by the screen in USE, and re-elect when one
gains focus. `test_client_mirroring.py` drives the whole thing on real ptys.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from hive_ide.clients import IdeClients
from hive_ide.frame import Frame
from hive_ide.store import StateStore


class _Result:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


@pytest.fixture
def frame(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Frame(StateStore(tmp_path / "state", workspace), socket="hive-ide-test-f")


def _row(
    name: str,
    size: str,
    activity: int,
    *,
    flags: str = "attached,UTF-8",
    created: int,
    pid: int = 1000,
) -> str:
    width, height = size.split("x")
    return f"{name}\t{width}\t{height}\t{activity}\t{flags}\t{created}\t{pid}\n"


def _clients(
    frame: Frame, rows: str, monkeypatch, *, attention: str = ""
) -> list[dict]:
    """`attached_clients` over a canned client list and attention stamp.

    Dispatched per command rather than one canned answer: `attached_clients`
    reads the attention option first, and answering that with the client list
    would silently make every attention test meaningless.
    """

    def tmux(_self, args, **_kw):
        if args[0] == "show-options":
            return _Result(attention)
        return _Result(rows)

    monkeypatch.setattr(Frame, "tmux", tmux)
    return frame.attached_clients()


def test_the_screen_tmux_named_to_the_hook_outranks_every_other_signal(
    frame, monkeypatch
):
    """Attention replaces activity as the primary key, and has to.

    During the second two screens attach in, activity, creation and focus ALL
    tie — and `client-active` fires exactly once, on the first keystroke, inside
    that second. Ranked without attention that one election picks by pid and
    nothing re-runs, which is the state measured in
    `temp/scripts/probe-focus-recovery.py`: a 49-column screen holding the frame
    while the 203-column one is typed into.
    """
    tie = 1794315923
    rows = _row(
        "/dev/pts/93", "49x44", tie, flags="attached,focused,UTF-8",
        created=tie, pid=1319678,
    ) + _row(
        "/dev/pts/88", "203x68", tie, flags="attached,focused,UTF-8",
        created=tie, pid=1319673,
    )

    assert Frame._intended_controller(_clients(frame, rows, monkeypatch)) == (
        "/dev/pts/93"
    ), "with no attention stamped, the tie is broken by pid — the measured bug"
    assert Frame._intended_controller(
        _clients(frame, rows, monkeypatch, attention="/dev/pts/88")
    ) == "/dev/pts/88"


def test_an_unstamped_attention_option_promotes_nobody(frame, monkeypatch):
    """Empty is not a client name.

    A frame built by an older package has no stamp at all, and `show-options -qv`
    answers an unset option with an empty line. Matching a client against that
    would make the FIRST client the attention client by accident.
    """
    rows = _row("/dev/pts/2", "203x68", 1794312876, created=1794200000) + _row(
        "/dev/pts/48", "49x44", 1794297283, created=1794290000
    )

    clients = _clients(frame, rows, monkeypatch, attention="")

    assert [c["attention"] for c in clients] == [False, False]
    assert Frame._intended_controller(clients) == "/dev/pts/2"


def test_the_screen_in_use_outranks_the_newest_attach(frame, monkeypatch):
    """The reported frame, as tmux reported it.

    Ranked by creation the narrow screen wins for having attached second, and
    nothing it does afterwards -- including four hours of nothing -- can lose it
    the frame.
    """
    rows = _row("/dev/pts/2", "203x68", 1794312876, created=1794200000) + _row(
        "/dev/pts/48", "49x44", 1794297283, created=1794290000
    )

    clients = _clients(frame, rows, monkeypatch)

    assert [c["client"] for c in clients] == ["/dev/pts/2", "/dev/pts/48"]
    assert Frame._intended_controller(clients) == "/dev/pts/2", (
        "the idle narrow screen kept the frame, so the window stays at 49 columns"
    )


def test_a_focused_screen_outranks_one_that_was_typed_in_more_recently(
    frame, monkeypatch
):
    """A screen can be READ without being typed into.

    Activity is the workhorse, but it only moves on input. Pick up a second
    terminal to look at it and the one you last typed in would otherwise keep
    the frame while you stare at a mirror.
    """
    rows = _row(
        "/dev/pts/2", "203x68", 1794300000, flags="attached,focused,UTF-8",
        created=1794200000,
    ) + _row("/dev/pts/48", "49x44", 1794312876, created=1794290000)

    clients = _clients(frame, rows, monkeypatch)

    assert Frame._intended_controller(clients) == "/dev/pts/2"


def test_activity_decides_when_neither_screen_reports_focus(frame, monkeypatch):
    """Focus is not always observable.

    A terminal only reports focus if it sends focus events — an ssh tty or a web
    terminal never will — so the rank cannot depend on it. Both of the clients on
    the reported frame were unfocused.
    """
    rows = _row("/dev/pts/2", "203x68", 1794312876, created=1794200000) + _row(
        "/dev/pts/48", "49x44", 1794297283, created=1794290000
    )

    clients = _clients(frame, rows, monkeypatch)

    assert [c["focused"] for c in clients] == [False, False]
    assert Frame._intended_controller(clients) == "/dev/pts/2"


def test_creation_still_breaks_an_activity_tie(frame, monkeypatch):
    """Whole seconds, so a second screen attaching inside one second ties.

    This is the ordinary `open` path and it must keep working: the client the
    attach just created has to take control, and on a tie activity cannot say so.
    """
    tie = 1794312876
    rows = _row("/dev/pts/48", "49x44", tie, created=tie, pid=400) + _row(
        "/dev/pts/2", "203x68", tie, created=tie + 1, pid=900
    )

    clients = _clients(frame, rows, monkeypatch)

    assert Frame._intended_controller(clients) == "/dev/pts/2"


def test_a_flag_that_merely_contains_focused_is_not_focus(frame, monkeypatch):
    """Matched per flag, not as a substring of the whole list."""
    rows = _row("/dev/pts/0", "203x70", 1794312876, flags="attached,unfocused-ish",
                created=1794312876)

    clients = _clients(frame, rows, monkeypatch)

    assert clients[0]["focused"] is False


def _hook_values(tmp_path, monkeypatch) -> list[list[str]]:
    store = StateStore(tmp_path / "state", tmp_path)
    frame = Frame(store, socket="fixture")
    calls: list[list[str]] = []

    def tmux(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(frame, "tmux", tmux)
    monkeypatch.setattr(
        frame, "_module", lambda module, args: f"module={module} " + " ".join(args)
    )
    frame.bind_keys()
    return calls


def _installed(calls: list[list[str]], hook: str) -> list[str]:
    return [
        args[-1]
        for args in calls
        if args[0] == "set-hook" and hook in args and "-u" not in args
    ]


@pytest.mark.parametrize("hook", ["client-focus-in", "client-active"])
def test_coming_back_to_a_screen_runs_an_election(tmp_path, monkeypatch, hook):
    """Two triggers, because neither alone covers every terminal.

    `client-focus-in` is the direct signal and fires without a keystroke, but
    tmux asks for focus events only from a terminal that reports them -- a web
    terminal or a bare tty never fires it. `client-active` is the one that
    survives there: tmux notifies it when the window's latest client changes,
    which is what typing on another screen does.
    """
    calls = _hook_values(tmp_path, monkeypatch)

    installed = _installed(calls, hook)

    assert len(installed) == 1, f"expected one {hook} hook, got {installed}"
    command = shlex.split(installed[0].split("run-shell -b ", 1)[-1])[0]
    assert "module=clients" in command, f"{hook} does not elect: {command!r}"
    assert "--relayout-on-change" in command


@pytest.mark.parametrize(
    "hook", ["client-focus-in", "client-active", "client-attached"]
)
def test_an_attention_event_stamps_the_client_tmux_named(tmp_path, monkeypatch, hook):
    """The one fact only the hook has.

    An election that starts afterwards cannot work out which screen the event
    was about, and during the second two screens attach in every other ordering
    key ties — so without this the single `client-active` firing lands on that
    tie, picks by pid, and nothing re-runs. `-F` is what expands the format; a
    plain `set-option` would store the literal `#{client_name}`.
    """
    calls = _hook_values(tmp_path, monkeypatch)

    installed = _installed(calls, hook)

    assert installed, f"{hook} is not installed"
    stamp = installed[0].split("run-shell", 1)[0]
    assert stamp.startswith("set-option -F "), (
        f"{hook} does not stamp the attention client: {installed[0]!r}"
    )
    assert Frame.ATTENTION_OPTION in stamp
    assert "#{client_name}" in stamp


def test_a_departing_client_is_not_stamped_as_the_one_in_use(tmp_path, monkeypatch):
    """On detach the hook's client is the one LEAVING.

    Preferring it would name a client that is already gone and leave the
    survivor a mirror, which is the state `client-detached` exists to prevent.
    """
    calls = _hook_values(tmp_path, monkeypatch)

    installed = _installed(calls, "client-detached")

    assert installed, "client-detached is not installed"
    assert "set-option" not in installed[0], (
        f"the departing client is stamped as the one in use: {installed[0]!r}"
    )


@pytest.mark.parametrize("hook", ["client-focus-in", "client-active"])
def test_coming_back_does_not_relayout_unconditionally(tmp_path, monkeypatch, hook):
    """1.0.86, pinned.

    `client-focus-in` ran a full relayout once -- every window, every pane, on
    every alt-tab -- and was removed as a hook storm. Both of these fire faster
    than a client set changes, so the election may lead to a relayout; the hook
    must not contain one.
    """
    calls = _hook_values(tmp_path, monkeypatch)

    installed = _installed(calls, hook)

    assert installed, f"{hook} is not installed"
    command = installed[0].split("run-shell -b ", 1)[-1]
    assert "module=relayout" not in command, (
        f"{hook} relays out unconditionally again: {installed!r}"
    )


def _elect_with(monkeypatch, tmp_path, *, before: str, after: str) -> list[str]:
    """Run the focus hook's election over a controller that moves or does not."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relayouts: list[str] = []
    reads = {"count": 0}

    def fake_attached_clients(_self):
        reads["count"] += 1
        name = before if reads["count"] == 1 else after
        return [{"client": name, "size": "203x68", "controller": True}]

    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(Frame, "attached_clients", fake_attached_clients)
    monkeypatch.setattr(Frame, "elect_controller", lambda self: after)
    monkeypatch.setattr(
        IdeClients, "_relayout", staticmethod(lambda store, socket: relayouts.append(socket))
    )

    IdeClients.elect(
        tmp_path / "state", str(workspace), "sock", relayout_on_change=True
    )
    return relayouts


def test_a_promotion_relays_the_frame_out(tmp_path, monkeypatch):
    """Promoting is not enough on its own.

    tmux preserves a zoom across an automatic resize, so the window the narrow
    controller zoomed for mobile grows to the wide screen's size and stays zoomed
    to one pane. Nothing else restores its columns: the wide terminal never
    resized, so `client-resized` does not fire.
    """
    assert _elect_with(
        monkeypatch, tmp_path, before="/dev/pts/48", after="/dev/pts/2"
    ) == ["sock"]


def test_focus_on_the_screen_already_in_charge_changes_nothing(tmp_path, monkeypatch):
    """The common firing, and the reason the relayout is gated at all."""
    assert (
        _elect_with(monkeypatch, tmp_path, before="/dev/pts/2", after="/dev/pts/2")
        == []
    )


def test_an_election_without_the_flag_never_relays_out(tmp_path, monkeypatch):
    """Attach and detach chain their own relayout in the tmux hook.

    Both would otherwise run twice -- once from the hook, once from here.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    relayouts: list[str] = []
    monkeypatch.setattr(Frame, "exists", lambda self: True)
    monkeypatch.setattr(
        Frame, "attached_clients", lambda self: [
            {"client": "/dev/pts/48", "size": "49x44", "controller": True}
        ]
    )
    monkeypatch.setattr(Frame, "elect_controller", lambda self: "/dev/pts/2")
    monkeypatch.setattr(
        IdeClients, "_relayout", staticmethod(lambda store, socket: relayouts.append(socket))
    )

    IdeClients.elect(tmp_path / "state", str(workspace), "sock")

    assert relayouts == []
