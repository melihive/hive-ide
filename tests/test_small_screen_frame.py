"""The frame on a phone-sized client: the predicate, the zoom, and the popup.

Reported from a phone over SSH at 49 columns: the frame "always ends in the 3
column layout, even when I focus the sidebar", and the session options modal
"does not fit nicely and does not show all session options". Both were ONE
defect — `#{<:X,Y}` compares as TEXT in tmux, so `#{<:49,120}` is false and all
three mobile predicates were dead on anything phone-sized. Width 49 took the
desktop branch: a 52%-wide popup that drew 2 of 10 actions, and pane keys whose
zoom-preserving arm never ran.

These tests ask REAL tmux, because the bug was in what tmux does with a string,
and no amount of Python asserting on our own strings would have seen it.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hive_ide import __version__  # noqa: E402
from hive_ide.drivers import bundled_drivers  # noqa: E402
from hive_ide.frame import Frame  # noqa: E402
from hive_ide.layout import IdeLayout  # noqa: E402
from hive_ide.store import StateStore  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")

#: 49x44 is the client in the report (a phone terminal over SSH); 203 is the
#: desktop beside it. 119/120 straddle the threshold itself.
PHONE = (49, 44)
DESKTOP = (203, 68)


def _source() -> dict:
    return {"kind": "stable", "interpreter": sys.executable, "version": __version__}


def _frame(tmp_path, names=("ALPHA", "BETA")) -> tuple[Frame, StateStore]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    store = StateStore(tmp_path / "state", workspace)
    driver = bundled_drivers()["term"]
    for name in names:
        store.create_session(
            name=name,
            working_dir=workspace,
            source=_source(),
            driver=driver.resolve(
                name=name, working_dir=str(workspace), conversation_reference=None
            ),
        )
    socket = f"hive-ide-test-{uuid.uuid4().hex[:8]}"
    return Frame(store, socket=socket), store


def _bare_tmux(socket: str, width: int, height: int) -> None:
    subprocess.run(
        ["tmux", "-L", socket, "new-session", "-d", "-x", str(width), "-y", str(height),
         "sh -c 'while :; do sleep 30; done'"],
        check=True, capture_output=True,
    )


def _ask(socket: str, fmt: str) -> str:
    return subprocess.run(
        ["tmux", "-L", socket, "display-message", "-p", fmt],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_the_mobile_predicate_is_numeric_not_textual_in_real_tmux():
    """Both shipped forms must agree with `is_mobile` at every width — tmux's answer, not ours.

    The retired form is exercised too, and at the width from the report: a test
    that only checked the new one would pass against the old one as well on a
    120-column terminal, which is how this survived.
    """
    socket = f"hive-ide-test-{uuid.uuid4().hex[:8]}"
    _bare_tmux(socket, *PHONE)
    try:
        for width in (20, 49, 80, 119, 120, 121, 203):
            subprocess.run(
                ["tmux", "-L", socket, "resize-window", "-t", "0", "-x", str(width), "-y", "40"],
                check=True, capture_output=True,
            )
            expected = "1" if IdeLayout.is_mobile(width) else "0"
            fmt = IdeLayout.tmux_mobile_format("#{window_width}")
            assert _ask(socket, fmt) == expected, f"format form wrong at {width}"

            shell = subprocess.run(
                ["tmux", "-L", socket, "if-shell", IdeLayout.shell_mobile_test(),
                 "display-message -p MOBILE", "display-message -p WIDE"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            assert shell == ("MOBILE" if IdeLayout.is_mobile(width) else "WIDE"), (
                f"shell form wrong at {width}: {shell!r}"
            )

        # The defect itself, pinned: the text comparison calls a 49-column phone
        # "not mobile", which is why it took the desktop branch everywhere.
        subprocess.run(
            ["tmux", "-L", socket, "resize-window", "-t", "0", "-x", "49", "-y", "40"],
            check=True, capture_output=True,
        )
        retired = "#{<:#{window_width}," + str(IdeLayout.mobile_threshold()) + "}"
        assert _ask(socket, retired) == "0", (
            "tmux now compares `#{<:}` numerically — re-check whether the helpers "
            "are still needed before changing them"
        )
        assert IdeLayout.is_mobile(49), "49 columns must be mobile, or this test is moot"
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def test_no_mobile_predicate_compares_width_as_text():
    """The gate: `#{<:` against the threshold anywhere in the package is the bug returning."""
    package = Path(__file__).resolve().parents[1] / "hive_ide"
    files = sorted(package.glob("*.py"))
    assert len(files) > 10, "the package was not found — this gate would scan nothing"
    offenders = [
        f"{path.name}:{lineno}: {line.strip()}"
        for path in files
        # layout.py NAMES the retired form in the docstring that explains it.
        if path.name != "layout.py"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "{<:" in line or "{>:" in line
    ]
    assert not offenders, (
        "a tmux TEXT comparison is back; use IdeLayout.tmux_mobile_format or "
        "shell_mobile_test:\n  " + "\n  ".join(offenders)
    )


def test_focus_keeps_a_phone_sized_frame_zoomed(tmp_path):
    """Any focus change on a mobile frame leaves the focused pane owning the window.

    Before: one plain `select-pane` — a tap, `ide_nav`, a driver launch, a modal
    handing focus back — dropped the zoom, and nothing put it back, so the phone
    sat in sidebar 1 / agent 27 / plan 19 for the rest of the session.
    """
    frame, _ = _frame(tmp_path)
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        frame.tmux(["resize-window", "-t", window, "-x", str(PHONE[0]), "-y", str(PHONE[1])])
        panes = [
            line.split()[0]
            for line in frame.tmux(
                ["list-panes", "-t", window, "-F", "#{pane_id}"]
            ).stdout.splitlines()
        ]
        assert len(panes) == 3, panes

        for pane in panes + list(reversed(panes)):
            frame.tmux(["select-pane", "-t", pane])
            zoomed = frame.tmux(
                ["display-message", "-p", "-t", window, "#{window_zoomed_flag}"]
            ).stdout.strip()
            width = frame.tmux(
                ["display-message", "-p", "-t", pane, "#{pane_width}"]
            ).stdout.strip()
            assert zoomed == "1", f"focus on {pane} left the window unzoomed"
            assert int(width) == PHONE[0], (
                f"{pane} is {width} columns, not the whole {PHONE[0]}-column window"
            )

        # A desktop window gets its three columns back. The hook never unzooms —
        # on a wide screen a zoom is the user's own `prefix z` and undoing it on
        # every focus change would be its own bug — so the LADDER owns that arm,
        # exactly as `client-resized` drives it when a phone detaches.
        frame.tmux(["resize-window", "-t", window, "-x", str(DESKTOP[0]), "-y", str(DESKTOP[1])])
        subprocess.run(
            [sys.executable, "-m", "hive_ide.relayout",
             "--tmux-socket", frame.socket,
             "--state-home", str(frame.store.home),
             "--workspace-key", frame.store.workspace_key,
             "--client-width", str(DESKTOP[0]), "--client-height", str(DESKTOP[1]),
             "--window-id", window],
            check=True, capture_output=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert frame.tmux(
            ["display-message", "-p", "-t", window, "#{window_zoomed_flag}"]
        ).stdout.strip() == "0", "the ladder left a 203-column window zoomed"
        for pane in panes:
            frame.tmux(["select-pane", "-t", pane])
            zoomed = frame.tmux(
                ["display-message", "-p", "-t", window, "#{window_zoomed_flag}"]
            ).stdout.strip()
            assert zoomed == "0", f"focus on {pane} zoomed a {DESKTOP[0]}-column window"
    finally:
        frame.tmux(["kill-server"])


def test_the_popup_opens_at_the_size_the_client_can_show(tmp_path):
    """The bound popup's own width/height formats, expanded by tmux at both sizes.

    Asserting the literal string would have passed throughout the defect; the
    question is what tmux RESOLVES it to on a 49-column client.
    """
    frame, _ = _frame(tmp_path)
    try:
        frame.open(no_attach=True)
        frame.bind_keys()
        keys = frame.tmux(["list-keys"]).stdout
        popup = next(
            (line for line in keys.splitlines() if "display-popup" in line and "96%" in line),
            "",
        )
        assert popup, "no popup binding carries a mobile width any more"
        window = sorted(frame.windows().values())[0]

        for (width, height), expected_w, expected_h in (
            (PHONE, "96%", "92%"), (DESKTOP, "52%", "42%")
        ):
            frame.tmux(["resize-window", "-t", window, "-x", str(width), "-y", str(height)])
            # The binding reads the CLIENT; with no client attached the window is
            # the same number, so the format is asked about the window instead.
            fmt_w = "#{?" + IdeLayout.tmux_mobile_format("#{window_width}") + ",96%,52%}"
            fmt_h = "#{?" + IdeLayout.tmux_mobile_format("#{window_width}") + ",92%,42%}"
            got_w = frame.tmux(["display-message", "-p", "-t", window, fmt_w]).stdout.strip()
            got_h = frame.tmux(["display-message", "-p", "-t", window, fmt_h]).stdout.strip()
            assert (got_w, got_h) == (expected_w, expected_h), (
                f"at {width}x{height} the popup would be {got_w}x{got_h}"
            )
    finally:
        frame.tmux(["kill-server"])


def test_a_panes_own_key_restores_the_zoom_a_manual_unzoom_gave_up(tmp_path):
    """tmux fires no `after-select-pane` for the pane that is already active.

    So on a mobile window the hook alone could not answer `prefix z` followed by
    the key for the pane you are already in: you stayed in three crushed columns
    having just asked, in the most direct way there is, for that pane. The key
    carries the zoom itself, idempotently, for exactly this case.
    """
    frame, _ = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        frame.bind_keys()
        window = sorted(frame.windows().values())[0]
        agent = frame.tmux(
            ["display-message", "-p", "-t", f"{window}.1", "#{pane_id}"]
        ).stdout.strip()

        frame.tmux(["resize-window", "-t", window, "-x", "100", "-y", "40"])
        frame.tmux(["select-pane", "-t", agent])
        zoomed = lambda: frame.tmux(  # noqa: E731
            ["display-message", "-p", "-t", window, "#{window_zoomed_flag}"]
        ).stdout.strip()
        assert zoomed() == "1", "the hook did not zoom on a 100-column window"

        frame.tmux(["resize-pane", "-Z", "-t", agent])
        assert zoomed() == "0", "the manual unzoom did not take"

        # Exactly what the bound key runs.
        frame.tmux(Frame.select_pane_keeping_mobile_zoom("1"))
        assert zoomed() == "1", "the pane's own key left a mobile window unzoomed"

        # Pressing it again must not toggle the zoom back off.
        frame.tmux(Frame.select_pane_keeping_mobile_zoom("1"))
        assert zoomed() == "1", "a second press toggled the zoom off"

        # On a desktop window the same key must leave zoom alone, in both states.
        frame.tmux(["resize-window", "-t", window, "-x", "200", "-y", "50"])
        frame.tmux(["resize-pane", "-Z", "-t", agent])  # back to unzoomed
        assert zoomed() == "0"
        frame.tmux(Frame.select_pane_keeping_mobile_zoom("1"))
        assert zoomed() == "0", "a 200-column window was zoomed by a pane key"
    finally:
        frame.tmux(["kill-server"])


def test_the_mobile_zoom_command_is_idempotent(tmp_path):
    """Applied twice it must still be zoomed — a bare `-Z` would flip it back.

    The hook and the pane keys both issue this, and two overlapping selections can
    both pass the shell's width test before either resizes; the inner format check
    is evaluated by tmux immediately before the resize, so the second one is a
    no-op instead of an unzoom.
    """
    frame, _ = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        frame.tmux(["resize-window", "-t", window, "-x", "100", "-y", "40"])
        pane = frame.tmux(
            ["display-message", "-p", "-t", f"{window}.1", "#{pane_id}"]
        ).stdout.strip()
        frame.tmux(["select-pane", "-t", pane])
        zoomed = lambda: frame.tmux(  # noqa: E731
            ["display-message", "-p", "-t", window, "#{window_zoomed_flag}"]
        ).stdout.strip()
        if zoomed() == "1":
            frame.tmux(["resize-pane", "-Z", "-t", pane])
        assert zoomed() == "0"

        command = Frame.zoom_when_mobile()
        for applied in range(1, 4):
            frame.tmux(command.split(" ", 1)[0:1] + _if_shell_args(command))
            assert zoomed() == "1", f"flipped off after {applied} applications"

        frame.tmux(["resize-window", "-t", window, "-x", "200", "-y", "50"])
        frame.tmux(["resize-pane", "-Z", "-t", pane])
        assert zoomed() == "0"
        frame.tmux(command.split(" ", 1)[0:1] + _if_shell_args(command))
        assert zoomed() == "0", "a 200-column window was zoomed"
    finally:
        frame.tmux(["kill-server"])


def _if_shell_args(command: str) -> list[str]:
    """`if-shell 'a' 'b'` -> ["a", "b"], so the command can be run as tmux argv."""
    import shlex

    return shlex.split(command)[1:]
