"""The chat pane's border names the session's relay agent.

The pane title was the session name alone, so nothing on screen said which relay
agent a window belongs to — and relay names are how the sessions are addressed in
conversation ("ask Wayan"), so the mapping lived only in `relay agents` output.

Two properties matter and pull against each other. Most sessions DO have a relay
label and should show it; nine of fifteen live ones do NOT, and for those the
suffix must vanish entirely rather than render as empty brackets. And the label is
written by relay registration rather than typed here, so it is sanitised: the
border is drawn through `pane-border-format`, where `#` opens a format or style
sequence.
"""
from __future__ import annotations

import pytest

from hive_ide.frame import Frame
from hive_ide.store import StateStore


@pytest.fixture
def store(tmp_path):
    workspace = tmp_path / "example-repo"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def _record(name="HIDE", labels=None):
    record = {"id": "s1", "name": name, "working_dir": "/tmp", "plan": None}
    if labels is not None:
        record["labels"] = labels
    return record


def test_the_chat_title_carries_the_relay_name(store):
    titles = Frame(store, socket="sock")._pane_titles(_record(labels={"relay": "Lenka"}))

    assert titles["agent"] == "HIDE (Lenka)"


def test_a_session_without_a_relay_label_shows_the_name_alone(store):
    """The common case — not empty brackets."""
    frame = Frame(store, socket="sock")

    assert frame._pane_titles(_record())["agent"] == "HIDE"
    assert frame._pane_titles(_record(labels={}))["agent"] == "HIDE"
    assert frame._pane_titles(_record(labels={"relay": ""}))["agent"] == "HIDE"


@pytest.mark.parametrize("labels", [None, {}, {"relay": None}, {"relay": 7}, "notadict"])
def test_a_missing_or_malformed_label_never_breaks_the_title(store, labels):
    """The title is drawn on every window build, so a malformed label must degrade
    to the plain name rather than raise and take the build down with it."""
    titles = Frame(store, socket="sock")._pane_titles(_record(labels=labels))

    assert titles["agent"] == "HIDE"


def test_only_the_chat_pane_is_annotated(store):
    """Explicitly asked for: the sidebar keeps the workspace name and the plan pane
    keeps the plan title. The relay name belongs on the chat pane only."""
    titles = Frame(store, socket="sock")._pane_titles(_record(labels={"relay": "Lenka"}))

    assert titles["sidebar"] == "example-repo", titles
    assert "Lenka" not in titles["sidebar"]
    assert "Lenka" not in titles["plan"]


def test_a_hash_in_the_label_cannot_reach_the_border_format(store):
    """`pane-border-format` renders `#{@hive_ide_title}`, and `#` opens a format or
    style sequence there — so a `#` arriving from relay registration would distort
    the border rather than print."""
    titles = Frame(store, socket="sock")._pane_titles(
        _record(labels={"relay": "#[fg=red]Lenka"})
    )

    assert "#" not in titles["agent"], titles["agent"]
    assert titles["agent"] == "HIDE ([fg=red]Lenka)"


def test_a_long_label_is_bounded(store):
    """The chat pane's border is narrow; a long name must not crowd out the session
    name it exists to annotate."""
    titles = Frame(store, socket="sock")._pane_titles(_record(labels={"relay": "L" * 80}))

    assert titles["agent"] == "HIDE (" + "L" * 24 + ")"


def test_whitespace_in_the_label_is_collapsed(store):
    titles = Frame(store, socket="sock")._pane_titles(
        _record(labels={"relay": "  Lenka\n\tTwo  "})
    )

    assert titles["agent"] == "HIDE (Lenka Two)"


# ── the label must reach the border without a rebuild ─────────────────────────


def _label_args(store, value):
    import argparse

    return argparse.Namespace(
        workspace_key=str(store.workspace_key),
        state_home=str(store.home),
        config=str(store.home / "config.json"),
        session_id="s1",
        key="relay",
        value=value,
        clear=False,
    )


def test_setting_the_label_retitles_the_panes(store, monkeypatch):
    """Relay registration sets this label long after the window was built, so
    without retitling here the name appears only on the next rebuild — which reads
    as the feature not working at all."""
    from hive_ide import cli

    record = store.create_session(
        name="HIDE",
        working_dir=str(store.workspace_key),
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver={"id": "term", "launch_argv": ["/bin/sh"], "resume": {"reference": None}},
    )
    monkeypatch.setattr(cli, "_session", lambda *a, **kw: record)
    seen: list = []
    monkeypatch.setattr(
        Frame, "retitle_panes", lambda self, rec: seen.append(rec["id"]) or True
    )
    args = _label_args(store, "Lenka")
    args.session_id = record["id"]

    result = cli.cmd_label(args)

    assert result["labels"]["relay"] == "Lenka"
    assert seen == [record["id"]], "the panes must be retitled in the same call"
    assert result["retitled"] is True


def test_a_label_is_recorded_even_when_no_frame_can_be_retitled(store, monkeypatch):
    """A label is state, not a drawing: recording it must not depend on a frame
    being up, or labelling a sleeping session would fail."""
    from hive_ide import cli
    from hive_ide.errors import HiveIdeError

    record = store.create_session(
        name="HIDE",
        working_dir=str(store.workspace_key),
        source={"kind": "stable", "interpreter": "/usr/bin/python3", "version": "0"},
        driver={"id": "term", "launch_argv": ["/bin/sh"], "resume": {"reference": None}},
    )

    def explode(self, rec):
        raise HiveIdeError("no server")

    monkeypatch.setattr(Frame, "retitle_panes", explode)
    args = _label_args(store, "Lenka")
    args.session_id = record["id"]

    result = cli.cmd_label(args)

    assert result["labels"]["relay"] == "Lenka"
    assert result["retitled"] is False


# ── the two cross-model review findings ───────────────────────────────────────


class _Reply:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _framed(store, monkeypatch, reply):
    """A Frame whose window and panes exist and whose tmux answers with `reply`."""
    frame = Frame(store, socket="sock")
    calls: list = []

    def fake_tmux(self, args, **kwargs):
        calls.append((list(args), kwargs))
        return reply(args) if callable(reply) else reply

    monkeypatch.setattr(Frame, "tmux", fake_tmux)
    monkeypatch.setattr(Frame, "windows", lambda self: {"s1": "@7"})
    monkeypatch.setattr(
        Frame,
        "_order_role_panes",
        lambda self, target: {"sidebar": "%1", "agent": "%2", "plan": "%3"},
    )
    return frame, calls


def test_a_failed_title_write_is_not_reported_as_retitled(store, monkeypatch):
    """Review finding 2. The write results were discarded, so a wedged server that
    changed nothing still produced `retitled: true`. This drives the REAL retitle
    path — the earlier test monkeypatched it away and so could not see this."""
    frame, _calls = _framed(store, monkeypatch, _Reply("", returncode=1))

    assert frame.retitle_panes(_record(labels={"relay": "Lenka"})) is False


def test_a_successful_retitle_is_reported(store, monkeypatch):
    """The inverse, so the check above cannot become "always False"."""
    def reply(args):
        # The probe reports a stale title, so a retitle is due; writes succeed.
        if args[0] == "display-message":
            return _Reply("OLD TITLE")
        return _Reply("")

    frame, _calls = _framed(store, monkeypatch, reply)

    assert frame.retitle_panes(_record(labels={"relay": "Lenka"})) is True


def test_nothing_to_do_is_not_reported_as_a_retitle(store, monkeypatch):
    """When every pane already shows its title there is nothing to write, and the
    callers that log "retitled panes" must not claim an action."""
    titles = Frame(store, socket="sock")._pane_titles(_record(labels={"relay": "Lenka"}))

    def reply(args):
        if args[0] == "display-message":
            pane = args[args.index("-t") + 1]
            role = {"%1": "sidebar", "%2": "agent", "%3": "plan"}[pane]
            return _Reply(titles[role])
        return _Reply("")

    frame, calls = _framed(store, monkeypatch, reply)

    assert frame.retitle_panes(_record(labels={"relay": "Lenka"})) is False
    assert not any(a[0] == "select-pane" for a, _kw in calls), "no writes were needed"


def test_every_retitle_call_to_tmux_is_bounded(store, monkeypatch):
    """Review finding 1. `cmd_label` reaches this path holding the workspace
    mutation lock and tmux has no timeout of its own, so an unresponsive server
    would hang labelling and block every other command waiting on that lock — and
    an exception handler cannot recover from a call that never returns."""
    frame, calls = _framed(store, monkeypatch, _Reply("OLD"))

    frame.retitle_panes(_record(labels={"relay": "Lenka"}))

    assert calls, "it must talk to tmux"
    unbounded = [args for args, kwargs in calls if not kwargs.get("timeout")]
    assert unbounded == [], unbounded
