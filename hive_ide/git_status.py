"""Read-only Git checkout status for sidebar decoration."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitCheckoutStatus:
    """The observable state of a linked Git checkout."""

    state: str


def inspect_linked_checkout(working_dir: str | Path) -> GitCheckoutStatus | None:
    """Return status for a linked checkout, or None for an ordinary directory/repo."""
    directory = Path(working_dir).expanduser()
    if not directory.is_dir():
        return GitCheckoutStatus("missing")

    try:
        identity = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "rev-parse",
                "--absolute-git-dir",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except FileNotFoundError:
        return None
    except (OSError, subprocess.SubprocessError):
        return GitCheckoutStatus("unknown")
    if identity.returncode != 0:
        return None

    lines = identity.stdout.splitlines()
    if len(lines) != 2:
        return GitCheckoutStatus("unknown")
    git_dir = Path(lines[0]).expanduser().resolve()
    common_dir = Path(lines[1]).expanduser()
    if not common_dir.is_absolute():
        common_dir = directory / common_dir
    if git_dir == common_dir.resolve():
        return None

    try:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return GitCheckoutStatus("unknown")
    if status.returncode != 0:
        return GitCheckoutStatus("unknown")
    if status.stdout:
        return GitCheckoutStatus("live")

    candidates: list[str] = []
    try:
        remote_head = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "symbolic-ref",
                "--quiet",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return GitCheckoutStatus("unknown")
    if remote_head.returncode == 0 and remote_head.stdout.strip():
        candidates.append(remote_head.stdout.strip())
    candidates.extend(("refs/heads/main", "refs/heads/master"))

    for reference in candidates:
        try:
            ahead = subprocess.run(
                [
                    "git",
                    "-C",
                    str(directory),
                    "rev-list",
                    "--count",
                    f"{reference}..HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if ahead.returncode == 0 and ahead.stdout.strip().isdigit():
            return GitCheckoutStatus(
                "shipped" if int(ahead.stdout.strip()) == 0 else "live"
            )
    return GitCheckoutStatus("live")
