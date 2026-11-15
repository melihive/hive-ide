"""The height tmux is actually asked for, not the height we meant to ask for.

`_content_rows` is pinned elsewhere, but a hint nothing reads changes nothing:
the defect reported from a handheld was the `-h` on the `display-popup` command
line — a PERCENTAGE of a short screen, which minus tmux's own popup border came
out under what the modal draws, so the overflow scrolled the title off the top.

These drive `popup.main` with tmux faked and assert on the argv it builds, which
is the only place that mistake is observable. Two mutants survived the first
harness run here: dropping the content-sizing line entirely, and dropping the
`+2` that pays for the border.
"""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from hive_ide import popup
from hive_ide.popup import MIN_POPUP_ROWS, _content_rows
from hive_ide.store import StateStore


def _session(tmp_path) -> tuple[StateStore, dict]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    record = store.create_session(
        name="ALPHA",
        working_dir=workspace,
        source={},
        driver={"id": "claude"},
    )
    return store, record


def _opened(store: StateStore, record: dict, client: str) -> tuple[list[str], int]:
    """Run `popup.main` with tmux faked; return the `display-popup` argv.

    `client` is what `display-message -p` reports as "<width> <height>".
    """
    seen: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        if "display-message" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=client, stderr="")
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    with patch.object(popup.subprocess, "run", side_effect=fake_run):
        rc = popup.main(
            [
                "--kind",
                "options",
                "--state-home",
                str(store.home),
                "--workspace-key",
                store.workspace_key,
                "--session-id",
                record["id"],
            ]
        )
    assert len(seen) == 1, f"expected one display-popup, got {seen}"
    return seen[0], rc


def _height(argv: list[str]) -> str:
    return argv[argv.index("-h") + 1]


def test_the_popup_is_sized_in_rows_from_the_content_not_a_percentage(tmp_path):
    """`-h` must be a row count derived from the modal, not `58%`/`92%`.

    A percentage is the defect itself: it describes the screen, never the thing
    being drawn, so on a short screen it is always a guess that can land under
    what the modal needs.
    """
    store, record = _session(tmp_path)
    argv, rc = _opened(store, record, client="80 40")

    height = _height(argv)
    assert "%" not in height, f"popup still sized by percentage: -h {height}"
    assert height.isdigit(), f"-h is not a row count: {height!r}"
    assert rc == 0


def test_the_requested_height_pays_for_tmuxs_popup_border(tmp_path):
    """Exactly two rows more than the content, or the border eats two rows of it.

    tmux draws the popup's border INSIDE the `-h` it is given, so asking for
    exactly the content's height delivers two rows fewer than the modal draws —
    and what disappears is the top.
    """
    store, record = _session(tmp_path)
    args = type("A", (), {
        "state_home": str(store.home),
        "workspace_key": store.workspace_key,
        "session_id": record["id"],
    })()
    needed = _content_rows("options", args)
    assert needed is not None and needed > MIN_POPUP_ROWS

    argv, _ = _opened(store, record, client="80 40")

    assert _height(argv) == str(needed + 2), (
        f"asked tmux for {_height(argv)} rows to draw {needed} rows of content "
        "plus a border"
    )


def test_a_screen_shorter_than_the_content_is_never_overrun(tmp_path):
    """The clamp: never taller than the client, never below the usable floor."""
    store, record = _session(tmp_path)

    argv, _ = _opened(store, record, client="80 9")
    assert _height(argv) == "9", f"popup asked for {_height(argv)} rows on a 9-row client"

    argv, _ = _opened(store, record, client="80 2")
    assert int(_height(argv)) == MIN_POPUP_ROWS, (
        f"popup shrank to {_height(argv)}, below the usable floor"
    )


def test_the_height_covers_lines_the_terminal_will_wrap(tmp_path):
    """Found by a cross-model review: logical lines are not terminal rows.

    tmux takes a percentage of the client for the width and draws the border
    inside it, so on some client widths the modal's widest line — its footer —
    is one cell too wide and wraps onto a second row. Counting logical lines
    then asks for one row too few, the content overruns, and the title is what
    leaves. EVERY client from 60 to 78 columns wraps today, plus exactly 120 —
    the whole narrow-terminal range, which is the screen this was reported from.
    """
    from hive_ide import modal_io
    from hive_ide.optionsmodal import IdeOptionsModal

    store, record = _session(tmp_path)
    lines, _ = IdeOptionsModal._lines(record, store.workspace_key, 0, rows=10_000)
    widest = max(modal_io.printable_width(line) for line in lines)

    for client_width in (60, 70, 78, 120):
        pct = 96 if client_width < 120 else 62
        interior = int(client_width * pct / 100) - 2
        argv, _ = _opened(store, record, client=f"{client_width} 60")
        asked = int(_height(argv))

        physical = modal_io.wrapped_rows(lines, interior)
        assert physical > len(lines), (
            f"width {client_width} was expected to wrap (interior {interior}, "
            f"widest line {widest}) but did not — pick a width that does"
        )
        assert asked >= physical + 2, (
            f"client {client_width}: asked tmux for {asked} rows, but the "
            f"content occupies {physical} rows plus a 2-row border"
        )


def test_a_width_that_does_not_wrap_is_not_padded(tmp_path):
    """The wrap allowance must not become a blanket two extra rows."""
    from hive_ide import modal_io
    from hive_ide.optionsmodal import IdeOptionsModal

    store, record = _session(tmp_path)
    lines, _ = IdeOptionsModal._lines(record, store.workspace_key, 0, rows=10_000)

    for client_width, pct in ((80, 96), (200, 62)):
        argv, _ = _opened(store, record, client=f"{client_width} 60")
        interior = int(client_width * pct / 100) - 2
        assert modal_io.wrapped_rows(lines, interior) == len(lines), (
            f"{client_width} cells was expected to wrap nothing"
        )
        assert int(_height(argv)) == len(lines) + 2
