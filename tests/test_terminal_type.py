"""A terminal tmux cannot drive must not cost the whole frame.

Found on a Mac on 2026-11-09. Every remote `hive-ide open` reported ALL of its
sessions "failed", each with tmux's raw `missing or unsuitable terminal:
network`, and `hive-ide verify` answered `ok: true`. macOS `login` stamps every
ssh ("network") tty with `TERM=network`, taken from the tty type in `/etc/ttys`,
and no terminfo entry exists for it — so tmux refused before a single window
could be built, and the one message the user saw blamed the sessions.

Three things had to line up, and each gets a test here:

  - nothing resolved `$TERM` to something tmux accepts before invoking it;
  - the failure was reported once per session, so it read as a session fault;
  - `verify` inspected the frame thoroughly and never the terminal.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid

import pytest

from hive_ide import cli, terminal
from hive_ide.errors import HiveIdeError, UsageError
from hive_ide.frame import Frame
from hive_ide.hooks import HookInstaller
from hive_ide.store import StateStore

KNOWN_GOOD = "xterm-256color"
UNKNOWN = "zzz-no-such-terminal"


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The verdict is cached per process; these tests change the answer."""
    terminal.is_usable.cache_clear()
    yield
    terminal.is_usable.cache_clear()


def _entry_exists(term: str) -> bool:
    """True when terminfo HAS this entry, whatever its capabilities.

    `infocmp`, not `tput <cap>`: asking for a capability conflates "no entry"
    with "entry without that capability", which is the very distinction these
    tests are about — and `dumb` lacks `clear` as well as `cup`, so a tput-based
    premise check skipped the `dumb` test instead of running it.
    """
    if shutil.which("infocmp") is None:
        return False
    return subprocess.run(["infocmp", term], capture_output=True).returncode == 0


needs_tput = pytest.mark.skipif(
    shutil.which("tput") is None, reason="tput is unavailable"
)
needs_good_term = pytest.mark.skipif(
    shutil.which("tput") is None or not _entry_exists(KNOWN_GOOD),
    reason=f"{KNOWN_GOOD} is not in this host's terminfo",
)


# -- what counts as usable --------------------------------------------------


@needs_tput
def test_an_unknown_terminal_is_not_usable():
    assert terminal.is_usable(UNKNOWN) is False


@needs_tput
def test_dumb_has_an_entry_and_is_still_not_usable():
    """tmux's refusal is "missing OR UNSUITABLE" — existence is not the test.

    `dumb` is what a non-interactive environment hands us, so an existence-only
    check would select the one value guaranteed to fail.
    """
    if not _entry_exists("dumb"):
        pytest.skip("this host has no `dumb` terminfo entry")
    assert terminal.is_usable("dumb") is False


@needs_good_term
def test_a_bogus_terminal_is_rejected_even_right_after_a_good_one():
    """The verdict must not depend on what was probed before it.

    This pins the reason the probe forks instead of using `curses` in-process:
    once `setupterm` has succeeded for one terminal, calling it again with an
    UNKNOWN one does not raise, leaves `cur_term` on the previous terminal, and
    `tigetstr` answers from that stale entry — so the bogus value came back
    USABLE, purely because a good one had been checked first.
    """
    assert terminal.is_usable(KNOWN_GOOD) is True
    assert terminal.is_usable(UNKNOWN) is False


# -- resolution -------------------------------------------------------------


@needs_good_term
def test_resolve_keeps_the_callers_own_terminal():
    """Its capabilities are the ones the user actually has."""
    assert terminal.resolve(KNOWN_GOOD) == KNOWN_GOOD


@needs_good_term
def test_resolve_falls_back_when_the_inherited_term_is_unusable():
    assert terminal.resolve("network") in terminal.FALLBACKS


def test_resolve_gives_up_rather_than_inventing_one(monkeypatch):
    """Unknown is not a verdict: with nothing usable, it returns None."""
    monkeypatch.setattr(terminal, "is_usable", lambda term: False)
    assert terminal.resolve("network") is None


@needs_good_term
def test_normalize_rewrites_an_unusable_term_and_reports_the_change():
    env = {"TERM": "network"}
    changed = terminal.normalize_environment(env)
    assert changed == {"from": "network", "to": env["TERM"]}
    assert terminal.is_usable(env["TERM"])


@needs_good_term
def test_normalize_leaves_a_usable_term_alone():
    env = {"TERM": KNOWN_GOOD}
    assert terminal.normalize_environment(env) is None
    assert env["TERM"] == KNOWN_GOOD


def test_normalize_leaves_the_environment_alone_when_nothing_qualifies(monkeypatch):
    """Substituting a value we know is also broken only moves the error."""
    monkeypatch.setattr(terminal, "is_usable", lambda term: False)
    env = {"TERM": "network"}
    assert terminal.normalize_environment(env) is None
    assert env == {"TERM": "network"}


# -- the wiring that actually prevents the outage ---------------------------


@needs_good_term
def test_the_cli_settles_term_before_anything_can_reach_tmux(monkeypatch, capsys):
    """One normalization at entry covers every tmux this process spawns.

    Including the `execvp` attach, which replaces the process and so cannot be
    fixed up afterwards.
    """
    monkeypatch.setenv("TERM", "network")
    assert cli.main(["version"]) == 0
    capsys.readouterr()
    assert terminal.is_usable(os.environ["TERM"]), os.environ["TERM"]


# -- the two reports that pointed the wrong way -----------------------------


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is unavailable")
def test_open_blames_the_terminal_not_the_sessions(tmp_path, monkeypatch):
    """The fatal message names the environment, once — not N sessions."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = StateStore(tmp_path / "state", workspace)
    from hive_ide.drivers import bundled_drivers

    driver = bundled_drivers()["term"]
    for name in ("ALPHA", "BETA"):
        store.create_session(
            name=name,
            working_dir=store.workspace_key,
            source={
                "kind": "stable",
                "interpreter": os.sys.executable,
                "version": __import__("hive_ide").__version__,
            },
            driver=driver.resolve(
                name=name,
                working_dir=str(store.workspace_key),
                conversation_reference=None,
            ),
        )

    # Exactly what tmux says when it will not serve the terminal.
    def _refuse(self, record):
        raise HiveIdeError("missing or unsuitable terminal: network")

    monkeypatch.setattr(Frame, "ensure", _refuse)
    monkeypatch.setenv("TERM", "network")
    frame = Frame(store, socket=f"hive-ide-test-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        with pytest.raises(UsageError) as caught:
            frame.open(no_attach=True)
    finally:
        frame.tmux(["kill-server"])

    message = str(caught.value)
    assert "TERM=network" in message
    assert "terminal, not the sessions" in message
    # The old wording sent every reader to the session store instead.
    assert "2 session(s) failed" not in message


@needs_tput
def test_verify_reports_an_unusable_terminal_and_fails_on_it(tmp_path, monkeypatch, capsys):
    """`verify` answered ok:true through a three-day outage; it must not.

    The hook check is silenced because it reports on the HARNESS this suite runs
    under, not on anything under test: left alone it holds `ok` false by itself,
    and the assertion below would then pass even with the terminal left out of
    the verdict entirely — which is exactly what the mutation run caught.
    """
    monkeypatch.setattr(terminal, "is_usable", lambda term: False)
    monkeypatch.setattr(HookInstaller, "verify", lambda self: [])
    monkeypatch.chdir(tmp_path)
    cli.main(["--state-home", str(tmp_path / "state"), "verify"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["terminal_findings"], payload
    assert "missing or unsuitable terminal" in payload["terminal_findings"][0]
    # The terminal is now the ONLY thing wrong, so `ok` can only be false
    # because the verdict actually counts it.
    assert payload["ok"] is False
    assert payload["hook_findings"] == []


@needs_tput
def test_verify_stays_quiet_when_it_cannot_tell(tmp_path, monkeypatch, capsys):
    """No `tput` means "unknown", which is not the same as "broken".

    `is_usable` answers False in both cases, so without the `can_probe` guard a
    minimal host would be told its terminal was unusable on every single run —
    a permanent false outage. The inert outcome is no finding, stated in the
    payload so the gap is visible rather than silent.
    """
    monkeypatch.setattr(terminal, "can_probe", lambda: False)
    monkeypatch.setattr(terminal, "is_usable", lambda term: False)
    monkeypatch.setattr(HookInstaller, "verify", lambda self: [])
    monkeypatch.chdir(tmp_path)
    cli.main(["--state-home", str(tmp_path / "state"), "verify"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["terminal_findings"] == []
    assert payload["terminal_probe"] == "unavailable"
    assert payload["ok"] is True


@needs_good_term
def test_verify_names_the_terminal_this_host_does_have(tmp_path, monkeypatch, capsys):
    """Never tell a reader "no fallback is installed" when one is.

    Reaching `verify` with an unusable TERM while a usable type exists means
    something got in without passing through `main` — a direct Python API
    caller. Claiming nothing is available would simply be false, and it sends
    them off to install terminfo they already have. `normalize_environment` is
    stubbed out here to stand in for that entry path.
    """
    monkeypatch.setattr(terminal, "normalize_environment", lambda env=None: None)
    monkeypatch.setattr(terminal, "is_usable", lambda term: term != "network")
    monkeypatch.setattr(HookInstaller, "verify", lambda self: [])
    monkeypatch.setenv("TERM", "network")
    monkeypatch.chdir(tmp_path)
    cli.main(["--state-home", str(tmp_path / "state"), "verify"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["terminal_findings"], payload
    finding = payload["terminal_findings"][0]
    assert "This host does have" in finding
    assert "No fallback is installed" not in finding
