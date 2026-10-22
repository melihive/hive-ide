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

    #: A reference shorter than this is too weak to match on: a few hex characters
    #: appear in unrelated command lines by chance, and a false positive here
    #: REFUSES a launch the user asked for.
    MIN_REFERENCE_LEN = 8

    #: EXACT executable basenames, matched against argv[0] — never a substring of the
    #: command line. Substring matching looked fine and was wrong: the first live
    #: probe of this module flagged the very `bash -c` running it, because
    #: `~/.claude/` appeared in an unrelated path in its argv. Exact rather than
    #: `startswith` so a `claude-helper` is not mistaken for a driver.
    DRIVER_EXECUTABLES = frozenset({"claude", "codex", "agy"})

    #: Drivers whose processes this module can actually recognise. A diagnostic must
    #: not report "no agent" for a driver it has no way of seeing — so callers gate
    #: on this rather than on "not a terminal session". Declarative and plugin
    #: drivers are deliberately absent.
    KNOWN_DRIVERS = frozenset({"claude", "codex", "antigravity"})

    #: An interpreter is a driver only when its argv names a known entrypoint. An
    #: npm-installed `claude` runs as `node …/@anthropic-ai/claude-code/cli.js`, so
    #: argv[0] alone would miss it — and missing it reports a healthy session as
    #: having no agent, which is the expensive direction.
    INTERPRETERS = frozenset({"node", "nodejs", "bun", "deno", "python", "python3"})
    ENTRYPOINT_MARKERS = ("claude-code/", "@anthropic-ai/claude", "codex/cli", "codex-cli")

    #: The reference must follow one of these in argv. Carrying a conversation id is
    #: NOT holding it: `claude -p "Explain transcript <ref>"` and
    #: `codex exec explain <ref>` both name one while owning nothing, and an agent
    #: investigating an incident is exactly the process that would. The real drivers
    #: select a conversation as `claude --resume <ref>` and
    #: `codex resume -C <dir> <ref>`.
    SELECTION_TOKENS = frozenset({"--resume", "resume", "--continue", "--session-id"})

    #: Argv marks of a driver's BACKGROUND session host. Claude Code runs a
    #: backgrounded session under `claude bg-pty-host`, which is deliberately
    #: detached — the whole point is that it outlives the terminal that started it,
    #: so it is reparented to init and `ppid == 1` is its NORMAL state, not a fault.
    #:
    #: This distinction is why the flag exists. A background host holds the
    #: conversation exactly as an abandoned driver does, so the holder check sees
    #: them identically; but one is live work the user backgrounded on purpose and
    #: the other is litter. Treating the first as the second told the user to kill a
    #: running session (AGENT CHAT, 2026-10-22) and refused to reopen a session that
    #: was never broken — it only needed attaching.
    BACKGROUND_MARKERS = ("bg-pty-host",)

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
        if not reference or len(reference) < cls.MIN_REFERENCE_LEN:
            return []
        rows = cls._ps() if rows is None else rows
        skip = cls.descendants(exclude_tree, rows) if exclude_tree else set()
        skip.add(os.getpid())
        found: list[dict[str, object]] = []
        for pid, ppid, command in rows:
            if pid in skip or reference not in command:
                continue
            if not cls._is_driver(command) or not cls._selects(command, reference):
                continue
            background = cls._is_background_host(command)
            found.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "command": command,
                    # A backgrounded session's host is attachable, not abandoned.
                    "background": background,
                    # ppid 1 with the session gone is the orphan shape: nothing owns
                    # it, nothing will reap it, and it is invisible until something
                    # tries to reuse its conversation. A background host is the one
                    # case where ppid 1 is by design, so it is NOT an orphan — the
                    # remedy for it is `claude attach`, never a kill.
                    "orphaned": ppid == 1 and not background,
                }
            )
        return found

    @classmethod
    def _is_driver(cls, command: str) -> bool:
        """Is this process a driver, judged by argv[0]?

        The basename, lowercased, with any extension dropped, must be EXACTLY one of
        the known driver names — so `claude`, `/opt/x/claude` and `claude.cmd` match
        while `bash`, `python3`, `grep`, `/bin/sh` and `claude-helper` do not,
        however much of a conversation id they happen to be carrying.

        An interpreter counts only when its argv also names a known driver
        entrypoint, which is how an npm-installed `claude` appears.
        """
        tokens = command.split()
        if not tokens:
            return False
        name = os.path.basename(tokens[0]).lower().split(".")[0]
        if name in cls.DRIVER_EXECUTABLES:
            return True
        if name in cls.INTERPRETERS:
            lowered = command.lower()
            return any(marker in lowered for marker in cls.ENTRYPOINT_MARKERS)
        return False

    @classmethod
    def _selects(cls, command: str, reference: str) -> bool:
        """Does this argv SELECT `reference`, rather than merely mention it?

        The reference must appear as its own whitespace-delimited token (not as part
        of a path or a longer string) and the argv must carry a recognised selection
        token. Both halves matter: the first stops a filename matching, the second
        stops a prompt that quotes a conversation id from looking like ownership.
        """
        tokens = command.split()
        if reference not in tokens:
            return False
        return any(token in cls.SELECTION_TOKENS for token in tokens)

    @classmethod
    def _is_background_host(cls, command: str) -> bool:
        """Is this the background host ITSELF, by argv position rather than text?

        A plain substring search reads `claude --resume <ref> "Explain bg-pty-host
        detection"` as a background host — and that is the worse direction here,
        because it makes the guard wave a real second driver through AND makes the
        launch pick `attach` when there is nothing to attach to. This module already
        learned the lesson once, when `~/.claude/` in an unrelated path made an
        ordinary shell look like a driver.

        So the marker has to sit where an invocation puts it: as the subcommand
        straight after argv[0], or as a flag. Both forms appear in the real argv,
        and the flag form is what an interpreter-launched host carries.
        """
        tokens = command.split()
        if len(tokens) < 2:
            return False
        for marker in cls.BACKGROUND_MARKERS:
            if tokens[1] == marker or f"--{marker}" in tokens:
                return True
        return False

    @classmethod
    def background_tree(
        cls, reference: str, rows: list[tuple[int, int, str]] | None = None
    ) -> set[int]:
        """Pids holding `reference` that belong to a background session.

        The host and everything beneath it. The host spawns the real driver as its
        child, and that child is often a recognisable driver in its own right — an
        npm-installed claude runs as `node …/claude-code/cli.js --session-id <ref>`
        — while carrying no marker of its own. Exempting only the host therefore
        left the child refusing the launch: the same lockout, one process down.
        """
        rows = cls._ps() if rows is None else rows
        out: set[int] = set()
        for host in cls.background_hosts(reference, rows=rows):
            pid = int(host["pid"])            # type: ignore[arg-type]
            out.add(pid)
            out |= cls.descendants(pid, rows)
        return out

    @classmethod
    def background_hosts(
        cls, reference: str, rows: list[tuple[int, int, str]] | None = None
    ) -> list[dict[str, object]]:
        """Holders of `reference` that are a driver's background session host.

        A non-empty result means the conversation is live in the background and can
        be reattached, so a launch must ATTACH rather than start a second driver or
        refuse outright. Empty is not evidence of absence — it carries the same
        unreadable-`ps` caveat as `holders`.
        """
        return [h for h in cls.holders(reference, rows=rows) if h["background"]]

    @staticmethod
    def attach_id(reference: str) -> str:
        """The short id `claude attach` takes, derived from a conversation id.

        `claude agents` lists a background session by the first segment of its
        session id, which is what `attach` resolves; the full id is not what the
        listing prints. Anything without a segment boundary is passed through
        unchanged rather than truncated to a guess.
        """
        head = reference.split("-")[0]
        return head or reference

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
