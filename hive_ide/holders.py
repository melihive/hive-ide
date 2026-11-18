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
import re
import subprocess
from pathlib import Path


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
                # `-ww` or the command is TRUNCATED to $COLUMNS, and this package
                # runs inside tmux panes, which export it. At 80 columns a
                # driver's argv is cut long before its `--resume <uuid>`, so the
                # holder becomes invisible and the guard fails OPEN — two drivers
                # on one conversation, the permanently blank pane this module
                # exists to prevent. `socket_gc` already learned this (`-axww`);
                # here it was missed until a narrow-pane test reproduced it.
                ["ps", "-ww", "-eo", "pid=,ppid=,command="],
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

    #: A socket name this package would recognise. Matched WHOLE: a name that
    #: only partly matches is not a name we may act on. `hive-ide-old:active`
    #: once yielded `hive-ide-old` under a search, which addresses a DIFFERENT
    #: server — and a clientless one of that name would then have authorized a
    #: signal (cross-model review, 2026-11-17).
    _SOCKET_NAME = re.compile(r"[A-Za-z0-9._-]+")

    @classmethod
    def _socket_from_tmux_argv(cls, command: str) -> str | None:
        """The `-L` name a tmux server was started with, or None if not plainly one.

        Parsed as OPTIONS, not searched for. tmux takes the LAST of a repeated
        option, so `-L a -L b` is server `b`, while a search returns `a` — the
        wrong server, and the whole point of this walk is to decide whether to
        signal something inside it.

        `-S <path>` addresses a socket by path instead. A frame never uses it,
        and mixing the two (`-L a -S /elsewhere`) means the name says nothing
        about where the server actually is, so both cases answer None.
        """
        tokens = command.split()
        name: str | None = None
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token == "-S" or (token.startswith("-S") and len(token) > 2):
                return None
            if token == "-L":
                if index + 1 >= len(tokens):
                    return None
                name, index = tokens[index + 1], index + 2
                continue
            if token.startswith("-L") and len(token) > 2:
                name, index = token[2:], index + 1
                continue
            index += 1
        if name is None or not cls._SOCKET_NAME.fullmatch(name):
            return None
        return name


    @staticmethod
    def argv_of(pid: int) -> list[str] | None:
        """The process's REAL argv, with argument boundaries intact.

        `ps` flattens argv into one space-joined line, which throws away the
        boundaries this decision rests on: a prompt is a single argument, but
        flattened it reads as a row of tokens, and any two of them can look like
        a flag and its value. `/proc/<pid>/cmdline` is NUL-separated, so a
        prompt stays one element and cannot select anything.

        None where it cannot be read — a platform without /proc, or a process
        already gone — and on this path None protects: no argv, no signal.
        """
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        if not raw.strip(b"\0"):
            return None
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]

    #: Options that name a conversation BY THEMSELVES, so an argv carrying one is
    #: never held to the id it also carries. `--continue`/`-c` resume the most
    #: recent conversation and `--last` the most recent session, leaving that id a
    #: PROMPT; `--fork-session` starts a new conversation from the one it resumes,
    #: leaving that id a SOURCE. Either way the process holds something this argv
    #: does not name.
    #:
    #: The bundled drivers generate none of them, but `resume_argv` is
    #: configuration: a declarative driver given `["claude", "--continue"]` with
    #: the `conversation_id` strategy generates `claude --continue <id>`, and then
    #: our own record would vouch for a process resuming something else entirely
    #: (cross-model review, 2026-11-18, rounds 9 and 10).
    #:
    #: This is the ONLY driver-CLI knowledge left on this path, and it can only
    #: ever REFUSE — never authorize. No option's ARITY is consulted anywhere,
    #: which is where all six of the earlier P1s came from. `-c` is in at the cost
    #: of a declarative driver that uses the short flag for something of its own:
    #: that driver loses this repair, which is the safe direction to be wrong in.
    OVERRIDING_SELECTION_OPTIONS = frozenset({
        "--fork-session", "--continue", "-c", "--last",
    })

    @staticmethod
    def _executable_name(token: str) -> str:
        """The comparable name of an argv[0]: `/opt/x/Claude.cmd` -> `claude`."""
        return os.path.basename(token).lower().split(".")[0]

    @classmethod
    def _argv_after_executable(cls, argv: list[str], executable: str) -> list[str] | None:
        """`argv` with its executable removed, or None when it is not ours.

        Two process shapes are the same program. A native install runs the
        command itself, so argv[0] IS the configured executable — compared by
        name, because the process carries whatever path the exec resolved to. An
        npm install is a shebang script, so the kernel starts the INTERPRETER
        with the entrypoint as its first argument (`node
        …/@anthropic-ai/claude-code/cli.js --resume <id>`) and the driver's own
        arguments begin after it.

        Both are supported installs — `_is_driver` already recognises each —
        and judging argv[0] alone left every npm install's stranded holder
        unreleasable, the feature silently inert for a whole install base
        (cross-model review, 2026-11-18, round 9).

        The entrypoint must be argv[1], the one place a shebang can put it: the
        kernel starts the interpreter with the script as its first argument. A
        marker accepted anywhere in argv let an unrelated program carry one in a
        later argument and have everything before it discarded — `node
        /tmp/task.js --resume OTHER …/claude-code/cli.js --resume <id>` then
        matched our launch while node was running `task.js` (cross-model review,
        2026-11-18, round 10). An interpreter invoked with options of its own is
        not this shape and is declined.
        """
        name = cls._executable_name(argv[0])
        if name == cls._executable_name(executable):
            return argv[1:]
        if name not in cls.INTERPRETERS or len(argv) < 2:
            return None
        entrypoint = argv[1].lower()
        if not any(marker in entrypoint for marker in cls.ENTRYPOINT_MARKERS):
            return None
        return argv[2:]

    @classmethod
    def selects_exactly(
        cls, argv: list[str], reference: str, *, launched_as: list[str]
    ) -> bool:
        """Is `reference` the conversation this argv actually SELECTS?

        Answered from THIS package's own record of what it launched, not by
        parsing a driver CLI's flags. `launched_as` is the exact argv
        `Frame.agent_launch_argv` builds for the session being repaired, which
        is also the argv the launcher runs — one source, so the thing compared
        against cannot drift from the thing started.

        That inversion is the lesson of three cross-model review rounds. The old
        answer reverse-engineered two CLIs whose flag semantics move with their
        releases, and review found SIX separate P1s in it, each a fresh way to
        misread an argv on the path that sends SIGTERM: a boolean flag promoting
        a prompt into the session slot; a named session defeating the shape test
        that replaced it; `--continue` read as value-bearing when it takes no id;
        `--fork-session` treated as holding its source; `--last` leaving the
        positional a prompt; a repeated `--resume` believed on the FIRST match
        where the CLI takes the last. Six in one function is the shape of a wrong
        abstraction rather than six mistakes: the function was deducing a fact
        this package already knows, having built the command line itself.

        So identity with our own launch is the proof. Four options are still
        named, in `OVERRIDING_SELECTION_OPTIONS`, and only ever to REFUSE: each
        names a conversation by itself, so agreement with our launch would prove
        the wrong thing. No option's ARITY is consulted anywhere, which is where
        every one of the six went wrong. Every one of those six shapes now
        fails for the same reason instead of six delicate ones — it is not what
        we launched — and a seventh shape nobody has thought of fails with them.

        What the question costs is reach. A driver started by hand, a session
        renamed since its driver started, or a hive-ide old enough to generate a
        different command line no longer qualifies, and the holder is left alone
        with the stalemate visible rather than silently signalled. That is the
        safe direction, and it keeps the shape found in the field:
        `claude --resume <id> --name <NAME>` is what this package generates today
        and what the stranded driver of 2026-11-17 was generated from.

        `launched_as` must name the conversation for the comparison to mean
        anything, and `selects_exactly` is the one place that decides it. A
        driver that resumes by workspace instead of by id — the
        `workspace_continue` strategy, `agy -c` — carries no reference in its
        argv at all, so no argv can prove WHICH conversation it holds, and this
        answers False rather than guessing.
        """
        if not argv or not reference or reference not in (launched_as or []):
            return False
        if any(
            token.partition("=")[0] in cls.OVERRIDING_SELECTION_OPTIONS
            for token in argv
        ):
            # This argv names a conversation some other way, so agreement with
            # our launch would prove the wrong thing. Refused rather than
            # trusted, because `resume_argv` is configuration and can generate
            # exactly this. Checked on the observed argv, which is the one that
            # has to match: a launch carrying such an option can only be matched
            # by a process carrying it too.
            return False
        rest = cls._argv_after_executable(argv, launched_as[0])
        if rest is None:
            return False
        expected = launched_as[1:]
        if rest == expected:
            return True
        # ONE trailing token is allowed, and only one: a launch prompt. The
        # launcher appends exactly one positional for it
        # (`Frame._argv_with_handoff_prompt`) and the record drops the prompt as
        # soon as that launch has delivered it (`_clear_consumed_handoff`), so a
        # session started with `--prompt`, or resumed through a handoff, runs an
        # argv one token longer than its record can render. Refusing that would
        # make this path inert for exactly those sessions. A positional cannot
        # change which conversation either driver selects; a token starting with
        # `-` could, so it is refused — and a real launch prompt can never start
        # with one (`launch_prompt_problem`).
        tail = rest[len(expected):]
        return (
            rest[:len(expected)] == expected
            and len(tail) == 1
            and not tail[0].startswith("-")
        )

    @classmethod
    def server_of(
        cls, pid: int, rows: list[tuple[int, int, str]] | None = None
    ) -> tuple[str | None, int | None]:
        """`(socket, pid)` of the tmux server that ultimately owns `pid`.

        Walks PARENTS, not children: a driver sits under its `sh -c` wrapper,
        which sits under the pane, which sits under the server. That chain is the
        only thing tying a process to the frame it belongs to — ppid alone says
        nothing, which is why `orphaned` (ppid 1) misses a driver stranded in a
        retired server entirely.

        The server's PID comes back with the name because a name alone cannot be
        verified: the same `-L` name in a different socket directory is a
        different server. Whoever probes it has to confirm it answered.
        """
        rows = cls._ps() if rows is None else rows
        parents = {pid_: ppid for pid_, ppid, _command in rows}
        commands = {pid_: command for pid_, _ppid, command in rows}
        seen: set[int] = set()
        current: int | None = pid
        while current and current > 1 and current not in seen:
            seen.add(current)
            command = commands.get(current) or ""
            head = command.split(None, 1)[0] if command else ""
            if head.rsplit("/", 1)[-1] == "tmux":
                return cls._socket_from_tmux_argv(command), current
            current = parents.get(current)
        return None, None

    @classmethod
    def stranded(
        cls,
        reference: str,
        *,
        launched_as: list[str],
        live_socket: str | None,
        has_clients,
        live_server_pid: int | None = None,
        argv_of=None,
        rows: list[tuple[int, int, str]] | None = None,
        exclude_tree: int | None = None,
    ) -> list[dict[str, object]]:
        """Holders NOTHING can reach: a retired frame, no client, not the live one.

        The shape found in the field on 2026-11-17. A frame had been rebuilt onto the
        current socket while the pre-2.0 server kept running with the session's
        driver alive inside it for 31 days. It held the conversation, so every
        relaunch hit the holder guard and every repair rebuilt a frame whose
        driver could never start — a stalemate no amount of repairing resolves.

        `orphaned` does not describe it: the driver's ppid is its own `sh -c`
        wrapper, so it reads as perfectly parented. What makes it provably
        abandoned is the three together:

        1. it lives under a tmux server that is NOT the live frame's,
        2. that server has no client attached, so no human can see or type at it,
        3. the socket is one this package hands out, so the server is ours.

        A fourth condition is the holder's own: it must be the driver this
        package launched for `reference`, judged by identity with `launched_as`
        rather than by reading its flags — see `selects_exactly`. With no such
        record there is nothing to compare against and nothing is stranded.

        `has_clients(socket) -> bool | None` answers (2). None means the question
        could not be answered, and an unanswered question PROTECTS: only a
        positive "no clients" makes a candidate. A backgrounded host is excluded
        for the same reason it is excluded from the launch guard — it is held on
        purpose, and the remedy for it is `attach`, never a signal.
        """
        from .socket_gc import SocketReaper

        rows = cls._ps() if rows is None else rows
        # The host AND everything under it, exactly as the launch guard does. The
        # host spawns the real driver as a child that carries no marker of its
        # own, so exempting only the host leaves that child looking stranded —
        # and here the consequence is not a refused launch but a SIGTERM to a
        # session the user backgrounded on purpose (cross-model review,
        # 2026-11-17).
        backgrounded = cls.background_tree(reference, rows=rows)
        found: list[dict[str, object]] = []
        for holder in cls.holders(reference, exclude_tree=exclude_tree, rows=rows):
            if holder.get("background") or int(holder["pid"]) in backgrounded:
                continue
            argv = (argv_of or cls.argv_of)(int(holder["pid"]))
            if argv is None or not cls.selects_exactly(
                argv, reference, launched_as=launched_as
            ):
                # No readable argv is not evidence, and a process that is not the
                # one we launched is not ours to judge. Either way: not something
                # to signal.
                continue
            socket_name, server_pid = cls.server_of(int(holder["pid"]), rows)
            if not socket_name or server_pid is None:
                continue
            if live_server_pid is not None:
                # Identity, not spelling. The same `-L` name under a different
                # `TMUX_TMPDIR` is a DIFFERENT server, so comparing names alone
                # would hide a genuinely stranded holder behind a coincidence.
                if server_pid == live_server_pid:
                    continue
            elif socket_name == live_socket:
                continue        # no identity to compare: fall back to the name
            if not SocketReaper.ours(socket_name):
                continue        # someone else's tmux: never ours to touch
            if has_clients(socket_name, server_pid) is not False:
                continue        # True is in use, None is unknown; both protect
            found.append({**holder, "socket": socket_name, "server_pid": server_pid})
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
        name = cls._executable_name(tokens[0])
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
