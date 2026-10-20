"""Type one caller-supplied constant line into a session's agent pane.

`nudge` is a generic "wake the agent with a fixed line" capability. The package
never builds the line: the caller passes a constant, and it must match a strict
safe pattern (printable ASCII, at most 200 chars, no control characters, no
leading slash-command / shell-escape / memory character). Integrations decide
WHEN to nudge and WHAT the line says; this module only decides whether typing
into the pane right now is safe.

Per driver:

- **Claude** queues text typed while it works, so a nudge is allowed at any time.
- **Codex** treats typing while it works as interrupting its turn. Without
  `force` it is typed only on positive proof of idle: the hook turn sequence
  says the latest `Stop` closed the latest turn (`turns.TurnSequence`), AND the
  pane's bottom rows show Codex's empty-composer signature with no working
  indicator or dialog. Anything else — working, unknown, unrecognised screen —
  is refused. `force` types regardless and accepts that it may interrupt.
- Any other driver is unsupported.

For every driver, regardless of `force`:

- the pane must be the live agent (not a shell left behind after the agent
  exited — a typed line + Enter there would run as a command), not in a tmux
  mode (copy-mode would interpret the keys), and the session not asleep;
- a nudge is refused when a human keystroke in that pane in the last 60 s is
  detectable. tmux exposes no per-PANE input time; it exposes
  `#{client_activity}`, the last input time of each attached CLIENT, and each
  client's current pane. So the check is: any client whose current pane is the
  target pane and whose last input is < 60 s old. It cannot see a human who
  typed a draft into the pane and then moved focus elsewhere; for Codex the
  empty-composer signature covers that case, for Claude it is uncovered.
  If the clients cannot be listed, the answer is unknown and the nudge is
  refused.

There is an unavoidable window between these checks and the keystrokes; the
workspace mutation lock is held across both so a hook write cannot interleave,
but the agent itself is not locked.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from .turns import TurnSequence


class AgentNudge:
    SUPPORTED_DRIVERS = ("claude", "codex")
    TEXT_MAX = 200
    SAFE_TEXT_RE = re.compile(r"[\x20-\x7e]{1,200}")
    # A first character that turns the line into something other than a message:
    # a slash command, a shell escape (`!` in both Claude and Codex), a memory
    # write (`#` in Claude).
    UNSAFE_LEADING = ("/", "!", "#")
    HUMAN_QUIET_SECONDS = 60

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
    COMPOSER_ROWS = 8
    BUSY_ROWS = 15
    SHELL_COMMANDS = frozenset({"sh", "bash", "fish", "zsh", "dash", "ksh", "tcsh"})

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
            return "must not start with /, ! or #"
        return None

    # -- pane observations ----------------------------------------------

    def _tmux(self, args: list[str]) -> Any:
        return self.frame.tmux(args)

    def pane_facts(self, pane_id: str) -> dict[str, Any] | None:
        result = self._tmux(
            [
                "display-message",
                "-p",
                "-t",
                pane_id,
                "#{pane_in_mode}\t#{pane_current_command}\t#{pane_dead}",
            ]
        )
        if result.returncode != 0:
            return None
        in_mode, _, rest = result.stdout.strip("\n").partition("\t")
        command, _, dead = rest.partition("\t")
        return {
            "in_mode": in_mode.strip() not in {"", "0"},
            "command": command.strip(),
            "dead": dead.strip() == "1",
        }

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

    def pane_lines(self, pane_id: str) -> list[str] | None:
        result = self._tmux(["capture-pane", "-p", "-t", pane_id])
        if result.returncode != 0:
            return None
        return result.stdout.splitlines()

    @classmethod
    def codex_prompt_signature(cls, lines: list[str] | None) -> bool:
        """Do the pane's bottom rows show an idle, empty Codex composer?

        Conservative by construction: no lines, no placeholder near the bottom,
        or any working/dialog marker in the bottom rows → False.
        """
        if not lines:
            return False
        rows = [line.rstrip() for line in lines if line.strip()]
        if not rows:
            return False
        busy = "\n".join(rows[-cls.BUSY_ROWS:]).casefold()
        if any(marker in busy for marker in cls.CODEX_BUSY_MARKERS):
            return False
        return any(cls.CODEX_COMPOSER_RE.match(row) for row in rows[-cls.COMPOSER_ROWS:])

    def _status(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self.frame.store.read("status", session_id)
        except Exception:
            return None

    # -- gates -----------------------------------------------------------

    def driver_gate(
        self, record: dict[str, Any], pane_id: str, *, force: bool
    ) -> tuple[bool, str]:
        """Per-driver rule shared by `nudge` and `driver_rename`."""
        driver = (record.get("driver") or {}).get("id")
        if driver == "claude":
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
            facts = self.pane_facts(pane_id)
            if facts is None:
                return refuse("pane_unobservable")
            if facts["dead"] or facts["command"].rsplit("/", 1)[-1] in self.SHELL_COMMANDS:
                return refuse("agent_not_running", facts["command"] or None)
            if facts["in_mode"]:
                return refuse("pane_in_mode")
            human = self.human_recent(pane_id)
            if human is None:
                return refuse("human_activity_unknown")
            if human:
                return refuse("human_active")
            allowed, reason = self.driver_gate(record, pane_id, force=force)
            if not allowed:
                return refuse(reason)
            sent = self._tmux(["send-keys", "-l", "-t", pane_id, text])
            if sent.returncode != 0:
                return refuse("send_failed", (sent.stderr or "").strip() or None)
            entered = self._tmux(["send-keys", "-t", pane_id, "Enter"])
            if entered.returncode != 0:
                return refuse("enter_failed", (entered.stderr or "").strip() or None)
        result["sent"] = True
        result["reason"] = reason
        result["pane_id"] = pane_id
        return result
