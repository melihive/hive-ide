"""The read-only cards keep their top when the screen is too short for them.

`info` prints the session card and the shortcut map. Both are READ-ONLY — any key
closes them — so there is nothing to scroll with, and a clipped card cannot be
recovered by the reader. What can be done is refuse to lose the TOP, which is
where the title and the first rows are, and say plainly that the rest was cut.

Reported from a handheld as a modal that "hides first item": the content was
drawn with no height budget at all, so the overflow scrolled off the top.
"""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

import hive_ide.info as info
from hive_ide.store import StateStore


def _shown(rows: int, store: StateStore) -> list[str]:
    """What `info.main` ACTUALLY prints at this height.

    The first version of this helper rebuilt the `fit` call itself, which tested
    `modal_io.fit` and not info's use of it — if info stopped calling fit, it
    would have stayed green. Drive the real entry point instead.
    """
    buffer = io.StringIO()
    os.environ["LINES"] = str(rows)
    try:
        with redirect_stdout(buffer), patch.object(info, "wait_any_key"):
            info.main(
                [
                    "--kind",
                    "keys",
                    "--state-home",
                    str(store.home),
                    "--workspace-key",
                    store.workspace_key,
                    "--session-id",
                    "unused-for-the-keys-card",
                ]
            )
    finally:
        os.environ.pop("LINES", None)
    out = [l for l in buffer.getvalue().split("\n") if l.strip()]
    return out


@pytest.fixture
def store(tmp_path) -> StateStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return StateStore(tmp_path / "state", workspace)


def test_the_whole_card_shows_when_there_is_room(store):
    full = info._keys(store.read_path(store.config_snapshot_path()) or {})

    shown = _shown(60, store)

    assert len(shown) == len(full), "a tall screen should clip nothing"
    assert not any("more" in l for l in shown), "nothing was cut, so say nothing"


def test_a_short_screen_keeps_the_top_and_admits_the_cut(store):
    shown = _shown(12, store)

    assert len(shown) <= 12, f"drew {len(shown)} lines into 12 rows"
    assert "shortcuts" in shown[0] or "─" in shown[0], (
        f"the top of the card was lost: first line was {shown[0]!r}"
    )
    assert any("more" in l for l in shown), (
        "a clipped card showed no marker, so it reads as the whole card"
    )


def test_room_is_left_for_the_close_prompt(store):
    """`wait_any_key` prints its own prompt AFTER this.

    Filling every row and then printing the prompt scrolls the terminal by one —
    which puts the title off the top again, by a different route.
    """
    rows = 12
    shown = _shown(rows, store)

    from hive_ide import modal_io

    assert len(shown) <= rows - 2, (
        f"{len(shown)} lines in {rows} rows leaves nothing for the prompt "
        f"(LINES={os.environ.get('LINES')!r}, "
        f"rows_available(2)={modal_io.rows_available(2)}) — "
        f"first line {shown[0][:40]!r}"
    )
