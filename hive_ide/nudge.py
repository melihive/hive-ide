"""Type one caller-supplied constant line into a session's agent pane.

`nudge` is a generic "wake the agent with a fixed line" capability. The package
never builds the line: the caller passes a constant, and it must match a strict
safe pattern (printable ASCII, at most 200 chars, no control characters, and no
leading `/` slash command, `!` shell escape, `#` memory write or `-` that tmux
could read as a flag). The text is also passed after `--`, so tmux never parses
it as options. Integrations decide WHEN to nudge and WHAT the line says; this
module only decides whether typing into the pane right now is safe.

Two gates, shared by `nudge` and `Frame.driver_rename`:

**Pane gate** (every driver, never bypassed by `force`): the agent pane is
observable, alive, not a shell left behind after the agent exited (a typed line
+ Enter there would run as a command), and not in a tmux mode (copy-mode would
interpret the keys). tmux names the pane's process-group leader, and hive-ide
runs every driver as a child of an `sh -c` wrapper, so a shell there is only
"no agent" when it is not that wrapper or the wrapper has no non-shell child; a
process table that cannot be read is refused. `nudge` additionally refuses when a human keystroke in that
pane in the last 60 s is detectable; `driver_rename` skips that check because
the human who just asked for the rename would always trip it.

tmux exposes no per-PANE input time. It exposes `#{client_activity}`, the last
input time of each attached CLIENT, and each client's current pane. So the check
is: any client whose current pane is the target pane and whose last input is
< 60 s old (attaching also stamps it). It cannot see a human who typed a draft
and then moved focus elsewhere — the composer checks below cover that. If the
clients cannot be listed the answer is unknown and the nudge is refused.

**Driver gate**:

- **Claude** queues text typed while it works, so no turn state is required.
  But Enter on a permission / plan-approval / question dialog picks the
  highlighted option, and typed text merges into a human's draft. So Claude is
  refused when its latest hook event is a Notification other than the idle
  reminder, when its bottom rows show a dialog, and unless its composer is
  recognised AND empty. A composer that cannot be recognised is refused.
- **Codex** treats typing while it works as interrupting its turn. Without
  `force` it is typed only on positive proof of idle: the hook turn sequence
  says the latest `Stop` closed the latest turn (`turns.TurnSequence`), AND the
  pane's bottom rows show Codex's empty-composer signature with no working
  indicator or dialog. `force` skips only these two Codex idle checks.
- Any other driver is unsupported.

Screen signatures are English strings from specific CLI versions (Claude Code
2.1, codex-cli 0.160). A reworded UI makes the checks refuse, never send.

There is an unavoidable window between these checks and the keystrokes; the
workspace mutation lock is held across both so a hook write cannot interleave,
but the agent itself is not locked. Every tmux call carries a timeout so a wedged
tmux server cannot hold that lock.
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any, Callable

from .turns import TurnSequence


class AgentNudge:
    SUPPORTED_DRIVERS = ("claude", "codex")
    TEXT_MAX = 200
    SAFE_TEXT_RE = re.compile(r"[\x20-\x7e]{1,200}")
    # A first character that turns the line into something other than a message:
    # a slash command, a shell escape (`!` in both Claude and Codex), a memory
    # write (`#` in Claude), or a tmux flag (`-`).
    UNSAFE_LEADING = ("/", "!", "#", "-")
    HUMAN_QUIET_SECONDS = 60
    TMUX_TIMEOUT = 3.0

    # Codex TUI (verified against codex-cli 0.160 strings): an empty composer
    # shows one of these placeholders after the `›` prompt glyph. Typed text
    # replaces the placeholder, so its presence also proves there is no draft.
    CODEX_PLACEHOLDERS = ("Ask Codex to do anything", "Ask a follow-up question")
    CODEX_COMPOSER_RE = re.compile(
        r"^\s*[│|]?\s*[›❯>▌]?\s*(?:"
        + "|".join(re.escape(text) for text in CODEX_PLACEHOLDERS)
        + r")\s*[│|]?\s*$"
    )
    # Anything that means a turn is running or the TUI is waiting on a decision.
    CODEX_BUSY_MARKERS = (
        "esc to interrupt",
        "to interrupt)",
        "working (",
        "would you like to",
        "press enter to confirm",
        "esc to dismiss",
        "press a number to choose",
        "type to continue",
        "to queue",
        "esc to cancel",
    )
    # Claude Code 2.1 dialogs (permission, edit, plan approval, questions).
    CLAUDE_DIALOG_MARKERS = (
        "do you want to",
        "would you like to proceed",
        "ready to code?",
        "no, keep planning",
        "tell claude what to do differently",
        "tell claude what to change",
        "yes, and don't ask again",
        "yes, and auto-accept",
        "enter to select",
        "to navigate",
        "esc to cancel",
        "esc to close",
    )
    # Claude's composer sits between two horizontal rules (or inside a box) with
    # a `>`/`❯` prompt; an empty one shows nothing after the glyph, or the
    # first-run `Try "…"` hint. Claude Code 2.1 writes the session name into the
    # top rule (`──── DC WEB DEV ─`), so a rule may carry one inset label. A rule
    # is width-independent: a bare rule needs 8 rule chars, a labelled one only
    # rule chars around one label (a 19-column pane leaves `────── AGENT CHAT ─`).
    CLAUDE_RULE_RE = re.compile(
        r"^\s*[╭╰]?(?:[─━]{8,}|[─━]{2,} [^─━]+ [─━]+)[╮╯]?\s*$"
    )
    CLAUDE_PROMPT_RE = re.compile(r"^\s*│?\s*[>❯]\s?(?P<text>.*?)\s*│?\s*$")
    # The hint is drawn DIM (SGR 2) and typed text is not, so with escape codes
    # the attribute decides. This text shape is only the fallback for a capture
    # with no escape codes; a narrow pane truncates the hint with `…`.
    CLAUDE_PLACEHOLDER_RE = re.compile(r'^Try ".*(?:"|…)$')
    # What `capture-pane -e` can emit: SGR (named, so its params can be read),
    # other CSI sequences, and OSC (e.g. hyperlinks). Stripped for every text check.
    ESCAPE_RE = re.compile(
        r"\x1b\[(?P<sgr>[0-9;:]*)m|\x1b\[[0-9;:?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    )
    COMPOSER_ROWS = 8
    BUSY_ROWS = 15
    DIALOG_ROWS = 20
    SHELL_COMMANDS = frozenset({"sh", "bash", "fish", "zsh", "dash", "ksh", "tcsh"})
    # `Frame._interactive_command` prefix: every agent pane runs as
    # `sh -c "unset NO_COLOR; <driver>; ...; exec $SHELL"`.
    WRAPPER_PREFIX = "unset NO_COLOR;"

    def __init__(self, frame: Any, *, clock: Callable[[], float] = time.time):
        self.frame = frame
        self.clock = clock

    # -- text ------------------------------------------------------------

    @classmethod
    def text_problem(cls, text: Any) -> str | None:
        """Why `text` is not a safe nudge line, or None when it is."""
        if not isinstance(text, str) or not text:
            return "empty"
        if len(text) > cls.TEXT_MAX:
            return f"longer than {cls.TEXT_MAX} characters"
        if not cls.SAFE_TEXT_RE.fullmatch(text):
            return "only printable ASCII is allowed (no control characters)"
        if text != text.strip():
            return "leading or trailing whitespace"
        if text.startswith(cls.UNSAFE_LEADING):
            return "must not start with /, !, # or -"
        return None

    # -- pane observations ----------------------------------------------

    def _tmux(self, args: list[str]) -> Any:
        return self.frame.tmux(args, timeout=self.TMUX_TIMEOUT)

    def pane_facts(self, pane_id: str) -> dict[str, Any] | None:
        """In-mode / current command / dead / pid for the pane, or None if unobservable."""
        result = self._tmux(
            [
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{pane_in_mode}\t#{pane_current_command}\t#{pane_dead}\t#{pane_pid}",
            ]
        )
        if result.returncode != 0:
            return None
        fields = result.stdout.rstrip("\n").split("\t")
        if len(fields) != 4:
            return None
        in_mode, command, dead, pid = (field.strip() for field in fields)
        if not in_mode.isdigit() or dead not in {"0", "1"} or not command or not pid.isdigit():
            return None
        return {"in_mode": in_mode != "0", "command": command, "dead": dead == "1", "pid": pid}

    def _ps(self, args: list[str]) -> str | None:
        """`ps` stdout, or None when the process table cannot be read."""
        try:
            result = subprocess.run(
                ["ps", *args], capture_output=True, text=True, timeout=self.TMUX_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    def wrapped_agent_running(self, pane_pid: str) -> bool | None:
        """Is a driver running under the hive-ide `sh -c` wrapper in this pane?

        True only when the pane process is the wrapper AND it has a live
        non-shell child. A login shell (the wrapper `exec`s one once the driver
        exits, and a sleeping session shows one) is False. None when the process
        table cannot be read — never treated as "running".
        """
        args = self._ps(["-p", pane_pid, "-o", "args="])
        if args is None:
            return None
        parts = args.strip().split(None, 2)
        if (
            len(parts) != 3
            or parts[0].rsplit("/", 1)[-1] != "sh"
            or parts[1] != "-c"
            or not parts[2].startswith(self.WRAPPER_PREFIX)
        ):
            return False
        table = self._ps(["-A", "-o", "ppid=,pid=,comm="])
        if table is None:
            return None
        for line in table.splitlines():
            fields = line.split(None, 2)
            if len(fields) != 3 or fields[0] != pane_pid:
                continue
            if fields[2].strip().rsplit("/", 1)[-1].lstrip("-") not in self.SHELL_COMMANDS:
                return True
        return False

    def human_recent(self, pane_id: str) -> bool | None:
        """Did a client focused on `pane_id` send input in the last 60 s?

        None when the clients cannot be observed — never treated as "no".
        """
        result = self._tmux(["list-clients", "-F", "#{client_activity}\t#{pane_id}"])
        if result.returncode != 0:
            return None
        now = self.clock()
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            activity, _, pane = line.partition("\t")
            if pane.strip() != pane_id:
                continue
            if not activity.strip().isdigit():
                return None
            if now - int(activity.strip()) < self.HUMAN_QUIET_SECONDS:
                return True
        return False

    def pane_lines(self, pane_id: str, *, escapes: bool = False) -> list[str] | None:
        """The pane's visible rows; with `escapes`, attributes kept as SGR codes."""
        flags = ["-p", "-e"] if escapes else ["-p"]
        result = self._tmux(["capture-pane", *flags, "-t", pane_id])
        if result.returncode != 0:
            return None
        return result.stdout.splitlines()

    @staticmethod
    def _rows(lines: list[str] | None) -> list[str]:
        return [line.rstrip() for line in (lines or []) if line.strip()]

    @classmethod
    def _plain(cls, line: str) -> str:
        return cls.ESCAPE_RE.sub("", line)

    @classmethod
    def _dim_after_glyph(cls, row: str) -> bool:
        """Is every visible character after the prompt glyph drawn dim (SGR 2)?

        Walks the row tracking the dim attribute. Spaces and a box edge are
        ignored; any other character drawn without dim — or no character at
        all — is False. Extended-colour arguments (`38;5;n`, `38;2;r;g;b`) are
        skipped so their `2`/`5` are never read as attributes.
        """
        dim = seen_glyph = seen_text = False
        position = 0
        for match in [*cls.ESCAPE_RE.finditer(row), None]:
            chunk = row[position:match.start()] if match else row[position:]
            for char in chunk:
                if not seen_glyph:
                    seen_glyph = char in ">❯"
                elif not (char.isspace() or char == "│"):
                    if not dim:
                        return False
                    seen_text = True
            if match is None:
                break
            position = match.end()
            if match.group("sgr") is None:
                continue
            params = (match.group("sgr") or "0").split(";")
            index = 0
            while index < len(params):
                param = params[index].split(":")[0]
                if param in ("", "0", "22"):
                    dim = False
                elif param == "2":
                    dim = True
                elif param in ("38", "48", "58") and ":" not in params[index]:
                    mode = params[index + 1] if index + 1 < len(params) else ""
                    index += {"5": 3, "2": 5}.get(mode, 1)
                    continue
                index += 1
        return seen_text

    @classmethod
    def codex_prompt_signature(cls, lines: list[str] | None) -> bool:
        """Do the pane's bottom rows show an idle, empty Codex composer?

        Conservative by construction: no lines, no placeholder near the bottom,
        or any working/dialog marker in the bottom rows → False.
        """
        rows = cls._rows(lines)
        if not rows:
            return False
        busy = "\n".join(rows[-cls.BUSY_ROWS:]).casefold()
        if any(marker in busy for marker in cls.CODEX_BUSY_MARKERS):
            return False
        return any(cls.CODEX_COMPOSER_RE.match(row) for row in rows[-cls.COMPOSER_ROWS:])

    @classmethod
    def claude_dialog_open(cls, lines: list[str] | None) -> bool:
        plain = [cls._plain(line) for line in (lines or [])]
        bottom = "\n".join(cls._rows(plain)[-cls.DIALOG_ROWS:]).casefold()
        return any(marker in bottom for marker in cls.CLAUDE_DIALOG_MARKERS)

    @classmethod
    def claude_composer(cls, lines: list[str] | None) -> str:
        """`empty`, `draft`, or `unrecognized` for Claude's bottom-most composer.

        The composer is the rows between the LAST two horizontal rules near the
        bottom (transcript echoes of past prompts also start with `>`, so the
        rules are what locate the live input). It is empty only as one prompt
        row with nothing — or only the `Try "…"` hint — after the glyph. When
        the capture carries escape codes the hint is recognised ONLY as a dim
        run after the glyph (typed text is not dim, whatever it says); the
        `Try "…"` text shape is the fallback for a capture without them.
        """
        raw = [line.rstrip() for line in (lines or []) if cls._plain(line).strip()]
        escapes = any("\x1b" in line for line in raw)
        raw = raw[-(cls.COMPOSER_ROWS + 4):]
        rows = [cls._plain(line).rstrip() for line in raw]
        rules = [index for index, row in enumerate(rows) if cls.CLAUDE_RULE_RE.match(row)]
        if len(rules) < 2:
            return "unrecognized"
        top, bottom = rules[-2], rules[-1]
        inside = rows[top + 1:bottom]
        if not inside:
            return "unrecognized"
        prompt = cls.CLAUDE_PROMPT_RE.match(inside[0])
        if prompt is None:
            return "unrecognized"
        text = prompt.group("text")
        if len(inside) != 1:
            return "draft"
        if not text:
            return "empty"
        if escapes:
            hint = cls._dim_after_glyph(raw[top + 1])
        else:
            hint = cls.CLAUDE_PLACEHOLDER_RE.match(text) is not None
        return "empty" if hint else "draft"

    def _status(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self.frame.store.read("status", session_id)
        except Exception:
            return None

    # -- gates -----------------------------------------------------------

    def pane_gate(
        self, pane_id: str, *, check_human: bool
    ) -> tuple[bool, str, str | None]:
        """Driver-independent pane safety; never bypassed by `force`."""
        facts = self.pane_facts(pane_id)
        if facts is None:
            return False, "pane_unobservable", None
        command = facts["command"].rsplit("/", 1)[-1]
        if facts["dead"]:
            return False, "agent_not_running", facts["command"]
        if command in self.SHELL_COMMANDS:
            running = self.wrapped_agent_running(facts["pid"])
            if running is None:
                return False, "pane_unobservable", None
            if not running:
                return False, "agent_not_running", facts["command"]
        if facts["in_mode"]:
            return False, "pane_in_mode", None
        if check_human:
            human = self.human_recent(pane_id)
            if human is None:
                return False, "human_activity_unknown", None
            if human:
                return False, "human_active", None
        return True, "pane_ok", None

    def driver_gate(
        self, record: dict[str, Any], pane_id: str, *, force: bool
    ) -> tuple[bool, str]:
        """Per-driver rule shared by `nudge` and `driver_rename`."""
        driver = (record.get("driver") or {}).get("id")
        if driver == "claude":
            if TurnSequence.attention_pending(self._status(record["id"])):
                return False, "claude_notification_pending"
            lines = self.pane_lines(pane_id, escapes=True)
            if lines is None:
                return False, "pane_unobservable"
            if self.claude_dialog_open(lines):
                return False, "claude_dialog_open"
            composer = self.claude_composer(lines)
            if composer == "draft":
                return False, "claude_draft_present"
            if composer != "empty":
                return False, "claude_composer_not_recognized"
            return True, "claude_queues_input"
        if driver != "codex":
            return False, "unsupported_driver"
        if force:
            return True, "forced"
        if not TurnSequence.idle_proven(self._status(record["id"])):
            return False, "codex_not_idle_proven"
        if not self.codex_prompt_signature(self.pane_lines(pane_id)):
            return False, "codex_prompt_not_recognized"
        return True, "codex_idle_proven"

    def _agent_pane(self, record: dict[str, Any]) -> str | None:
        return (self.frame.role_panes(record["id"]) or {}).get("agent")

    def nudge(self, record: dict[str, Any], text: str, *, force: bool = False) -> dict[str, Any]:
        driver = (record.get("driver") or {}).get("id")
        result: dict[str, Any] = {
            "session_id": record["id"],
            "driver": driver,
            "forced": bool(force),
            "sent": False,
        }

        def refuse(reason: str, detail: str | None = None) -> dict[str, Any]:
            result["reason"] = reason
            if detail:
                result["detail"] = detail
            return result

        problem = self.text_problem(text)
        if problem:
            return refuse("unsafe_text", problem)
        if driver not in self.SUPPORTED_DRIVERS:
            return refuse("unsupported_driver")
        if (record.get("sleep") or {}).get("state") == "sleeping":
            return refuse("agent_sleeping")
        with self.frame.store.mutation_lock():
            pane_id = self._agent_pane(record)
            if not pane_id:
                return refuse("no_agent_pane")
            allowed, reason, detail = self.pane_gate(pane_id, check_human=True)
            if not allowed:
                return refuse(reason, detail)
            allowed, reason = self.driver_gate(record, pane_id, force=force)
            if not allowed:
                return refuse(reason)
            sent = self._tmux(["send-keys", "-l", "-t", pane_id, "--", text])
            if sent.returncode != 0:
                return refuse("send_failed", (sent.stderr or "").strip() or None)
            entered = self._tmux(["send-keys", "-t", pane_id, "Enter"])
            if entered.returncode != 0:
                return refuse("enter_failed", (entered.stderr or "").strip() or None)
        result["sent"] = True
        result["reason"] = reason
        result["pane_id"] = pane_id
        return result
