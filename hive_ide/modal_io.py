"""Small terminal input helpers shared by read-only popups."""

from __future__ import annotations

import os
import sys

try:
    import termios
    import tty
except ImportError:  # pragma: no cover - non-posix fallback
    termios = None
    tty = None


def wait_any_key(prompt: str = "(press any key to close)") -> None:
    """Wait for one keypress without requiring Enter."""
    if not sys.stdin.isatty():
        return
    print(f"\n  {prompt}", end="", flush=True)
    fd = sys.stdin.fileno()
    if not (termios and tty):
        try:
            sys.stdin.read(1)
        except OSError:
            pass
        return
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        os.read(fd, 1)
    except OSError:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
