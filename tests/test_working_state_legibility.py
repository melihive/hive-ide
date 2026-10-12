"""A long turn must not render as an idle session.

`hook.py` writes `state: "working"` ONCE, at the turn boundary, and does not write
again until the turn ends. So the status record ages at exactly the rate the agent
is busy. The sidebar used to blank the dot once that record passed
`STALE_WORKING_SECONDS`, which meant any turn longer than 15 minutes — a test suite,
a slow deploy, a hung command — rendered with no status glyph at all: the same thing
an idle session shows, and the same thing a session whose agent has died shows.

Three states, not two: working-and-fresh, working-but-not-seen-recently, and
genuinely-no-status. The middle one is "I cannot confirm", and reporting it as the
blank that means "idle" is the failure this covers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hive_ide.sidebar import IdeSidebar
from hive_ide.state_compat import StateIO


def _at(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def status(monkeypatch):
    """Drive `_status_dot` from a synthetic status record."""
    cell = {}

    monkeypatch.setattr(StateIO, "read_error", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(
        StateIO,
        "read_session_status",
        staticmethod(lambda *a, **k: cell.get("record")),
    )
    return cell


def test_a_fresh_working_turn_shows_the_working_colour(status):
    status["record"] = {"state": "working", "ts": _at(30)}

    glyph, colour = IdeSidebar._status_dot("/state", {"id": "s"})

    assert glyph
    assert colour == IdeSidebar.STATUS_DOT["working"][1]


def test_a_long_turn_still_shows_a_working_glyph(status):
    """25 minutes into a test run the agent is working, not idle."""
    status["record"] = {
        "state": "working",
        "ts": _at(IdeSidebar.STALE_WORKING_SECONDS + 600),
    }

    glyph, colour = IdeSidebar._status_dot("/state", {"id": "s"})

    assert glyph, (
        "a turn past the staleness ceiling rendered no glyph at all — identical to "
        "an idle session, which is what made a busy agent look frozen"
    )
    assert colour == IdeSidebar.STALE_WORKING_COLOR


def test_the_long_turn_is_visibly_distinct_from_a_fresh_one(status):
    """Still legible as 'not first-hand news' — the two must not be the same mark."""
    status["record"] = {"state": "working", "ts": _at(5)}
    fresh = IdeSidebar._status_dot("/state", {"id": "s"})
    status["record"] = {
        "state": "working",
        "ts": _at(IdeSidebar.STALE_WORKING_SECONDS + 1),
    }
    aged = IdeSidebar._status_dot("/state", {"id": "s"})

    assert fresh[0] == aged[0], "the glyph should be the same state"
    assert fresh[1] != aged[1], "the colour should say how fresh the claim is"


def test_no_status_record_is_still_blank(status):
    """The genuinely-unknown case keeps rendering nothing — that part was right."""
    status["record"] = None

    assert IdeSidebar._status_dot("/state", {"id": "s"}) == ("", "")


def test_a_missing_timestamp_counts_as_not_recently_seen(status):
    """`_age_seconds(None)` is infinity: unknown age is not 'just now'."""
    status["record"] = {"state": "working"}

    glyph, colour = IdeSidebar._status_dot("/state", {"id": "s"})

    assert glyph
    assert colour == IdeSidebar.STALE_WORKING_COLOR
