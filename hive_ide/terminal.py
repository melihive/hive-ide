"""Terminal-type resolution for the processes that drive tmux.

tmux loads terminfo for the terminal it is about to serve and REFUSES TO RUN AT
ALL when ``$TERM`` has no usable entry, reporting::

    missing or unsuitable terminal: <TERM>

So one hostile value in the environment does not degrade a single session — it
takes down every session in the frame at once, and the only message the user
sees blames the sessions for it.

That is not hypothetical. macOS ``login`` stamps every ssh ("network") tty with
``TERM=network``, taken from the tty type in ``/etc/ttys``, and there is no
``network`` terminfo entry behind it. Every remote ``hive-ide open`` onto a Mac
therefore failed with all of its sessions "failed", reading as an IDE fault
rather than a terminal one. Reproduced 2026-11-09: ``TERM=network tmux
new-session -d`` exits 1, the same command under ``TERM=xterm-256color`` exits 0.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

# Tried in order, AFTER the terminal the caller actually has. 256-colour first
# so a capable terminal is never downgraded further than it has to be.
FALLBACKS: tuple[str, ...] = ("xterm-256color", "screen-256color", "xterm", "vt100")

# tmux's refusal is "missing OR UNSUITABLE", and the second half carries weight:
# `dumb` and `unknown` HAVE terminfo entries and are still refused, because they
# cannot address the cursor. An existence-only check would cheerfully select
# `dumb` — precisely what a non-interactive environment hands us — and then fail
# anyway, one layer further from the cause.
REQUIRED_CAPABILITY = "cup"


def can_probe() -> bool:
    """True when this host can answer whether a terminal is usable at all.

    `is_usable` reports False both for "this terminal is unusable" and for "I
    have no way to find out", which is safe for choosing a TERM — the caller
    leaves the environment alone either way — but is NOT safe for reporting a
    fault. A host with no `tput` would otherwise be told its terminal is broken
    forever. Absence, error and an explicit negative are three different states.
    """
    return shutil.which("tput") is not None


@lru_cache(maxsize=None)
def is_usable(term: str | None) -> bool:
    """True when tmux can drive `term` on this host.

    Asks `tput`, in a FRESH process per candidate, and caches the verdict — at
    most one fork when the inherited terminal already works, which is the normal
    case, and `open` makes far more tmux calls than that.

    It deliberately does NOT use `curses` in-process, which looks cheaper and
    is wrong: once `setupterm` has succeeded for one terminal, calling it again
    with an UNKNOWN one does not raise, leaves `cur_term` pointing at the
    previous terminal, and `tigetstr` then answers from that stale entry — so a
    bogus TERM is reported usable, depending only on what was probed before it.
    A check whose answer depends on call order is worse than no check at all.
    """
    if not term:
        return False
    if shutil.which("tput") is None:
        # No way to ask. Claim no knowledge rather than guessing: `resolve` then
        # leaves the environment exactly as it found it.
        return False
    try:
        # `cup` is the capability tmux cannot work without, and asking for it
        # covers both halves of tmux's own refusal: an unknown terminal fails
        # the lookup, and a known-but-unsuitable one (`dumb`) has no `cup`.
        probe = subprocess.run(
            ["tput", "-T", term, "cup", "0", "0"],
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and bool(probe.stdout)


def resolve(term: str | None = None) -> str | None:
    """A terminal type tmux will accept here, or None when nothing qualifies.

    The caller's own terminal is tried FIRST and kept whenever it works: its
    capabilities are the ones the user actually has, and swapping a generic
    entry in for a working one loses colour, keys and mouse for no reason.
    """
    inherited = os.environ.get("TERM") if term is None else term
    for candidate in (inherited, *FALLBACKS):
        if is_usable(candidate):
            return candidate
    return None


def normalize_environment(env: dict[str, str] | None = None) -> dict[str, str] | None:
    """Replace an unusable ``$TERM`` in `env` (default ``os.environ``), in place.

    Returns ``{"from": ..., "to": ...}`` when something changed and None when it
    did not, so a caller can report what it did instead of silently rewriting
    the environment under the user.
    """
    target = os.environ if env is None else env
    current = target.get("TERM")
    if is_usable(current):
        return None
    replacement = resolve(current)
    if replacement is None or replacement == current:
        # Nothing on this host is usable. Leave the environment ALONE and let
        # tmux report its own refusal: substituting a value we already know is
        # broken would only move the failure somewhere less informative.
        return None
    target["TERM"] = replacement
    return {"from": current or "", "to": replacement}
