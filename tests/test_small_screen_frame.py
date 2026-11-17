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


def _ladder(frame: Frame, store: StateStore, window: str, width: int, height: int) -> None:
    """Resize, then run the relayout ladder — exactly what `client-resized` does.

    Tests resize through this, never with a bare `resize-window`: the ladder is
    what records the frame's mobile state, so a test that skipped it would be
    asking the hook to read a flag nobody wrote.
    """
    frame.tmux(["resize-window", "-t", window, "-x", str(width), "-y", str(height)])
    subprocess.run(
        [sys.executable, "-m", "hive_ide.relayout",
         "--tmux-socket", frame.socket,
         "--state-home", str(store.home),
         "--workspace-key", store.workspace_key,
         "--client-width", str(width), "--client-height", str(height),
         "--window-id", window],
        check=True, capture_output=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def _zoomed(frame: Frame, window: str) -> str:
    return frame.tmux(
        ["display-message", "-p", "-t", window, "#{window_zoomed_flag}"]
    ).stdout.strip()


def _ask(socket: str, fmt: str) -> str:
    return subprocess.run(
        ["tmux", "-L", socket, "display-message", "-p", fmt],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_tmux_reads_the_mobile_flag_and_not_a_comparison():
    """What the shipped predicate resolves to, asked of real tmux.

    Also pins the two forms that were tried and rejected, because the reason they
    were rejected is a fact about tmux, not about this package:

      * `#{<:49,120}` is FALSE — tmux compares those as text. That was the defect:
        on a 49-column phone every mobile branch took the desktop arm.
      * `#{e|<:}` is correct here but needs tmux 3.2, and an older tmux renders it
        literally, which reads as TRUE — a desktop would zoom on every focus
        change. Debian 11 still ships 3.1c.

    An option read has neither problem, and tmux evaluates it synchronously.
    """
    socket = f"hive-ide-test-{uuid.uuid4().hex[:8]}"
    _bare_tmux(socket, *PHONE)
    try:
        fmt = IdeLayout.tmux_mobile_flag_format()
        assert _ask(socket, fmt) == "0", "an UNSET flag must read as not-mobile"
        for width in (20, 49, 80, 119, 120, 121, 203):
            subprocess.run(
                ["tmux", "-L", socket, "set-option", "-w", "-t", "0",
                 IdeLayout.MOBILE_OPTION, IdeLayout.mobile_flag_value(width)],
                check=True, capture_output=True,
            )
            expected = "1" if IdeLayout.is_mobile(width) else "0"
            assert _ask(socket, fmt) == expected, f"flag wrong for width {width}"

        subprocess.run(
            ["tmux", "-L", socket, "resize-window", "-t", "0", "-x", "49", "-y", "40"],
            check=True, capture_output=True,
        )
        retired = "#{<:#{window_width}," + str(IdeLayout.mobile_threshold()) + "}"
        assert _ask(socket, retired) == "0", (
            "tmux now compares `#{<:}` numerically — the history in "
            "`IdeLayout.tmux_mobile_flag_format` needs revisiting"
        )
        assert IdeLayout.is_mobile(49), "49 columns must be mobile, or this test is moot"
    finally:
        subprocess.run(["tmux", "-L", socket, "kill-server"], capture_output=True)


def test_the_ladder_records_the_frames_mobile_state(tmp_path):
    """The flag is only as good as the thing that writes it: the ladder, every pass."""
    frame, store = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        for width, height in ((49, 44), (119, 40), (120, 40), (203, 68)):
            _ladder(frame, store, window, width, height)
            got = frame.tmux(
                ["display-message", "-p", "-t", window,
                 "#{" + IdeLayout.MOBILE_OPTION + "}"]
            ).stdout.strip()
            assert got == IdeLayout.mobile_flag_value(width), (
                f"at {width} columns the frame believes mobile={got!r}"
            )
    finally:
        frame.tmux(["kill-server"])


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
        "a tmux TEXT comparison is back; use IdeLayout.tmux_mobile_flag_format:"
        "\n  " + "\n  ".join(offenders)
    )


def test_focus_keeps_a_phone_sized_frame_zoomed(tmp_path):
    """Any focus change on a mobile frame leaves the focused pane owning the window.

    Before: one plain `select-pane` — a tap, `ide_nav`, a driver launch, a modal
    handing focus back — dropped the zoom, and nothing put it back, so the phone
    sat in sidebar 1 / agent 27 / plan 19 for the rest of the session.
    """
    frame, store = _frame(tmp_path)
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        _ladder(frame, store, window, *PHONE)
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
        _ladder(frame, store, window, *DESKTOP)
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
    frame, store = _frame(tmp_path)
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
            _ladder(frame, store, window, width, height)
            fmt_w = "#{?" + IdeLayout.tmux_mobile_flag_format() + ",96%,52%}"
            fmt_h = "#{?" + IdeLayout.tmux_mobile_flag_format() + ",92%,42%}"
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
    frame, store = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        frame.bind_keys()
        window = sorted(frame.windows().values())[0]
        agent = frame.tmux(
            ["display-message", "-p", "-t", f"{window}.1", "#{pane_id}"]
        ).stdout.strip()

        _ladder(frame, store, window, 100, 40)
        frame.tmux(["select-pane", "-t", agent])
        zoomed = lambda: frame.tmux(  # noqa: E731
            ["display-message", "-p", "-t", window, "#{window_zoomed_flag}"]
        ).stdout.strip()
        assert zoomed() == "1", "the hook did not zoom on a 100-column window"

        frame.tmux(["resize-pane", "-Z", "-t", agent])
        assert zoomed() == "0", "the manual unzoom did not take"

        # Exactly what the bound key runs.
        key = Frame.select_pane_keeping_mobile_zoom("1", unzoom_on_desktop=True)
        frame.tmux(key)
        assert zoomed() == "1", "the pane's own key left a mobile window unzoomed"

        # Pressing it again must not toggle the zoom back off.
        frame.tmux(key)
        assert zoomed() == "1", "a second press toggled the zoom off"

        # DESKTOP, the other half of the same key: it must not zoom, and — the
        # behaviour the first version of this change dropped — pressing it on an
        # already-active, already-zoomed pane must UNZOOM, because on a wide
        # screen that key is also how you get the other columns back.
        _ladder(frame, store, window, *DESKTOP)
        assert zoomed() == "0", "the ladder left a desktop window zoomed"
        frame.tmux(key)
        assert zoomed() == "0", "a desktop window was zoomed by a pane key"
        frame.tmux(["resize-pane", "-Z", "-t", agent])
        assert zoomed() == "1"
        frame.tmux(key)
        assert zoomed() == "0", (
            "the pane key no longer unzooms a desktop window — pressing it on the "
            "pane you are already in leaves the other columns hidden"
        )

        # The sidebar key never had the desktop unzoom arm and still must not —
        # tested where it shows: on the sidebar itself, zoomed. (Moving to a
        # DIFFERENT pane unzooms in tmux regardless of what we bind, so that case
        # would say nothing about this binding.)
        sidebar = frame.tmux(
            ["display-message", "-p", "-t", f"{window}.0", "#{pane_id}"]
        ).stdout.strip()
        frame.tmux(["select-pane", "-t", sidebar])
        frame.tmux(["resize-pane", "-Z", "-t", sidebar])
        assert zoomed() == "1"
        frame.tmux(Frame.select_pane_keeping_mobile_zoom("0"))
        assert zoomed() == "1", "the sidebar key started unzooming desktop windows"
    finally:
        frame.tmux(["kill-server"])


def test_the_mobile_zoom_command_is_idempotent(tmp_path):
    """Applied twice it must still be zoomed — a bare `-Z` would flip it back.

    The hook and the pane keys both issue this, and two overlapping selections can
    both pass the shell's width test before either resizes; the inner format check
    is evaluated by tmux immediately before the resize, so the second one is a
    no-op instead of an unzoom.
    """
    frame, store = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        _ladder(frame, store, window, 100, 40)
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

        _ladder(frame, store, window, *DESKTOP)
        if zoomed() == "1":
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


def test_installing_bindings_seeds_the_mobile_flag_on_existing_windows(tmp_path):
    """Upgrading a RUNNING frame has to fix it, not just claim to.

    A tmux server keeps the hooks and options it was given, so the upgrade path is
    `bind_keys` against a live frame. Until it wrote the flag, the new hook and
    the new keys read an option nobody had set — which reads as "not mobile", the
    three crushed columns — and `activate` relays out only when the controller
    changes, so upgrading from the same client reported success and changed
    nothing the user could see.
    """
    frame, store = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        # A frame as an older package left it: narrow, with no flag.
        frame.tmux(["resize-window", "-t", window, "-x", str(PHONE[0]), "-y", str(PHONE[1])])
        frame.tmux(["set-option", "-w", "-t", window, "-u", IdeLayout.MOBILE_OPTION])
        assert frame.tmux(
            ["display-message", "-p", "-t", window, "#{" + IdeLayout.MOBILE_OPTION + "}"]
        ).stdout.strip() == "", "the flag was not actually cleared"

        frame.bind_keys()

        assert frame.tmux(
            ["display-message", "-p", "-t", window, "#{" + IdeLayout.MOBILE_OPTION + "}"]
        ).stdout.strip() == "1", "installing the bindings left the frame believing it is wide"

        # And the behaviour that depends on it works immediately, with no relayout.
        pane = frame.tmux(
            ["display-message", "-p", "-t", f"{window}.2", "#{pane_id}"]
        ).stdout.strip()
        frame.tmux(["select-pane", "-t", pane])
        assert _zoomed(frame, window) == "1", (
            "an upgraded frame still needs a resize before focus zooms"
        )
    finally:
        frame.tmux(["kill-server"])


def test_the_ladder_does_not_undo_the_zoom_the_hook_just_applied(tmp_path):
    """Relayout's own zoom must re-read the flag, because the hook moves it.

    The ladder re-homes the zoom onto the pane it believes is focused: it unzooms,
    selects, then zooms. With an `after-select-pane` hook in the server, that
    middle step zooms by itself, and the ladder's unconditional toggle turned it
    straight back off — the phone ending in exactly the squeezed layout both
    pieces exist to prevent.
    """
    frame, store = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        _ladder(frame, store, window, *PHONE)
        panes = [
            line.split()[0]
            for line in frame.tmux(
                ["list-panes", "-t", window, "-F", "#{pane_id}"]
            ).stdout.splitlines()
        ]
        for pane in panes:
            frame.tmux(["select-pane", "-t", pane])
            assert _zoomed(frame, window) == "1"
            _ladder(frame, store, window, *PHONE)
            assert _zoomed(frame, window) == "1", (
                f"the ladder unzoomed the frame with {pane} focused"
            )
            active = frame.tmux(
                ["display-message", "-p", "-t", window, "#{pane_id}"]
            ).stdout.strip()
            assert active == pane, f"the ladder moved focus from {pane} to {active}"
    finally:
        frame.tmux(["kill-server"])


def test_relayouts_zoom_survives_the_hook_zooming_first(tmp_path):
    """`_set_zoom` must end zoomed even when its own `select-pane` zoomed already.

    This is the branch the ladder takes when the pane it wants zoomed is not the
    one focused — reachable whenever focus moves after the ladder reads it, which
    two clients on one frame can do. It unzooms, selects, and zooms; the hook
    zooms on that selection, so the last step has to re-read the flag instead of
    toggling. Driven directly, because going through the ladder lands on the
    early return where the target IS the active pane and proves nothing: a blind
    toggle survived that test (mutation-checked, 2026-11-17).
    """
    from hive_ide.relayout import IdeRelayout

    frame, store = _frame(tmp_path, names=("ALPHA",))
    try:
        frame.open(no_attach=True)
        window = sorted(frame.windows().values())[0]
        _ladder(frame, store, window, *PHONE)
        panes = [
            line.split()[0]
            for line in frame.tmux(
                ["list-panes", "-t", window, "-F", "#{pane_id}"]
            ).stdout.splitlines()
        ]
        wanted, elsewhere = panes[1], panes[2]

        frame.tmux(["select-pane", "-t", elsewhere])
        assert _zoomed(frame, window) == "1", "the hook did not zoom the frame"

        # want=True, already zoomed, and the target is NOT the active pane.
        IdeRelayout._set_zoom(frame.socket, window, True, wanted)

        assert _zoomed(frame, window) == "1", (
            "relayout unzoomed a mobile frame while moving the zoom"
        )
        active = frame.tmux(
            ["display-message", "-p", "-t", window, "#{pane_id}"]
        ).stdout.strip()
        assert active == wanted, f"the zoom landed on {active}, not {wanted}"
    finally:
        frame.tmux(["kill-server"])
