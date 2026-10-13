#!/usr/bin/env python3
"""Which live processes are holding a driver conversation.

`ConversationGuard` answers whether a conversation EXISTS — gone, archived, or
still there. It cannot answer who is currently using it, and that is the question
that matters before launching an agent on one.

The gap cost a visible freeze on 2026-10-13. An orphaned `claude` (ppid 1, no
controlling tty, 16h40m old) still held its conversation. Re-opening the
frame respawned the agent pane, the fresh `claude --resume` of that same
conversation never painted, and the pane sat on its stale frame looking exactly
like a hung session. Two processes on one conversation is the fault; neither the
conversation probe nor the pane checks noticed, because the conversation really
did exist and the pane really was respawned.

A holder is found by its command line: a driver launched to resume a conversation
carries that conversation's reference in its argv. That is a deliberately narrow
signal — see `holders()` for what is excluded and why.

`ps` rather than `/proc`: this has to work on macOS, where half the fleet lives.
"""
from __future__ import annotations

import os
import subprocess


class ConversationHolders:
    """Live processes holding a conversation reference, by command line."""

    #: A reference shorter than this is too weak to match on: a few hex characters
    #: appear in unrelated command lines by chance, and a false positive here
    #: REFUSES a launch the user asked for.
    MIN_REFERENCE = 8

    #: Only a process whose EXECUTABLE is one of these counts as a holder, matched on
    #: the basename of argv[0] — never on the command line as a whole.
    #:
    #: Substring matching looked fine and was wrong: the first probe of this module
    #: flagged the very `bash -c` running it, because `~/.claude/` appears in an
    #: unrelated path in its argv and the reference was one of its arguments. A grep,
    #: an editor, a diagnostic or a capture script can all name a conversation without
    #: holding it, and a false holder REFUSES a launch the user asked for.
    DRIVER_EXECUTABLES = ("claude", "codex")

    @staticmethod
    def _ps() -> list[tuple[int, int, str]]:
        """(pid, ppid, command) for every visible process, or [] if ps fails.

        [] means "could not observe", and every caller must treat it as such — never
        as "no holders". Refusing to launch on an unobservable system would make the
        IDE unusable; claiming a conversation is free on no evidence is how the
        duplicate above happened. The callers resolve that tension explicitly.
        """
        try:
            result = subprocess.run(
                ["ps", "-eo", "pid=,ppid=,command="],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        rows: list[tuple[int, int, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) < 3:
                continue
            pid, ppid, command = parts
            if not pid.isdigit() or not ppid.isdigit():
                continue
            rows.append((int(pid), int(ppid), command))
        return rows

    @classmethod
    def snapshot(cls) -> list[tuple[int, int, str]]:
        """One read of the process table, for callers checking several sessions.

        Public because `verify` checks every session at once and should not pay for a
        `ps` per session, and should not reach into a private helper to avoid it.
        """
        return cls._ps()

    @classmethod
    def descendants(cls, pid: int, rows: list[tuple[int, int, str]] | None = None) -> set[int]:
        """`pid` and every process beneath it."""
        rows = cls._ps() if rows is None else rows
        children: dict[int, list[int]] = {}
        for child, parent, _command in rows:
            children.setdefault(parent, []).append(child)
        seen: set[int] = set()
        stack = [pid]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(children.get(current, []))
        return seen

    @classmethod
    def holders(
        cls,
        reference: str,
        *,
        exclude_tree: int | None = None,
        rows: list[tuple[int, int, str]] | None = None,
    ) -> list[dict[str, object]]:
        """Live driver processes whose argv carries `reference`.

        `exclude_tree` drops the process tree under one pid — the pane being
        respawned. Its own agent is the process we are deliberately replacing, so
        counting it as a holder would refuse every ordinary relaunch.

        Returns [] for a reference too short to match safely, and [] when `ps` could
        not be read. The second case is indistinguishable from "nothing holds it"
        here on purpose: this returns observations, and the decision about what an
        absence of observation means belongs to the caller that has to act on it.
        """
        if not reference or len(reference) < cls.MIN_REFERENCE:
            return []
        rows = cls._ps() if rows is None else rows
        skip = cls.descendants(exclude_tree, rows) if exclude_tree else set()
        skip.add(os.getpid())
        found: list[dict[str, object]] = []
        for pid, ppid, command in rows:
            if pid in skip or reference not in command:
                continue
            if not cls._is_driver(command):
                continue
            found.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "command": command,
                    # ppid 1 with the session gone is the orphan shape: nothing owns
                    # it, nothing will reap it, and it is invisible until something
                    # tries to reuse its conversation.
                    "orphaned": ppid == 1,
                }
            )
        return found

    @classmethod
    def _is_driver(cls, command: str) -> bool:
        """Is argv[0] one of the driver executables?

        Deliberately strict: the basename, lowercased, with any extension dropped,
        must START one of the known driver names — so `claude`, `/opt/x/claude`,
        `claude.cmd` and `codex-cli` match, while `bash`, `python3`, `grep` and
        `/bin/sh` do not, however much of a conversation id they are carrying.
        """
        head = command.split(None, 1)[0] if command.split() else ""
        name = os.path.basename(head).lower()
        name = name.split(".")[0]
        return any(name.startswith(driver) for driver in cls.DRIVER_EXECUTABLES)

    @classmethod
    def drivers_in_tree(
        cls, pid: int, rows: list[tuple[int, int, str]] | None = None
    ) -> list[dict[str, object]]:
        """Driver processes running under `pid`.

        Used to tell an agent pane that is doing something from one that is just a
        shell. A pane with no driver under it is pixel-identical to a working one:
        the frame is up, the layout is right, and the agent simply is not there.

        The pane's ROOT process counts. An agent pane is spawned as `sh -c '<argv>'`,
        and a shell handed a simple enough command `exec`s it rather than forking — so
        the pane's root pid can BE the driver, with no child at all. Excluding the root
        would then report a perfectly healthy session as having no agent, which is the
        more expensive mistake: a verify that flags working sessions gets ignored.
        """
        rows = cls._ps() if rows is None else rows
        tree = cls.descendants(pid, rows)
        return [
            {"pid": p, "ppid": pp, "command": c}
            for p, pp, c in rows
            if p in tree and cls._is_driver(c)
        ]

    @classmethod
    def observable(cls, rows: list[tuple[int, int, str]] | None = None) -> bool:
        """Whether the process table could be read at all."""
        return bool(cls._ps() if rows is None else rows)
