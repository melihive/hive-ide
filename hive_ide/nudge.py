"""Type one caller-supplied constant line into a session's agent pane.

`nudge` is a generic "wake the agent with a fixed line" capability. The package
never builds the line: the caller passes a constant, and it must match a strict
safe pattern (printable ASCII, at most 200 chars, no control characters, and no
leading `/` slash command, `!` shell escape, `#` memory write or `-` that tmux
could read as a flag). The text is also passed after `--`, so tmux never parses
it as options. Integrations decide WHEN to nudge and WHAT the line says; this
module only decides whether typing into the pane right now is safe.

Two gates, shared by `nudge` and `Frame.driver_rename`. Neither reads the
screen: the user resizes and hides panes constantly (a pane can be one column
wide, or its window shown on no client), and every width variant of a screen
check needed another regex fix and still refused panes that were safe. Safety is
decided from what the process table, tmux and the agent's own hooks report.

**Pane gate** (every driver, never bypassed by `force`): the agent pane is
observable, alive, not a shell left behind after the agent exited (a typed line
+ Enter there would run as a command), and not in a tmux mode (copy-mode would
interpret the keys). tmux names the pane's process-group leader, and hive-ide
runs every driver as a child of an `sh -c` wrapper, so a shell there is only
"no agent" when it is not that wrapper or the wrapper has no non-shell child; a
process table that cannot be read is refused.

**Driver gate** — hook state (`turns.TurnSequence`) is authoritative:

- **Claude** queues text typed while it works, so an open turn and an ended one
  are both allowed. Enter on a permission / plan-approval / question dialog picks
  the highlighted option, so Claude is refused (`claude_dialog_open`) while its
  latest hook event is a Notification other than the idle reminder. A session
  with no hook state at all is refused (`state_unknown`): the folder-trust prompt
  is shown before any hook runs, and nothing else would reveal it.
- **Codex** treats typing while it works as interrupting its turn. Without
  `force` it is typed only on positive proof of idle: the latest `Stop` closed
  the latest turn and was not a stop-hook continuation. `force` skips only that
  check, and only an explicit caller passes it.
- Any other driver is unsupported.

**Human input** (`nudge` only; `driver_rename` skips it because the human who
just asked for the rename would always trip it). tmux exposes no per-PANE input
time, only `#{client_activity}` per attached CLIENT plus that client's current
pane, so the check is: any client whose current pane is the target pane and
whose last input is < 60 s old refuses (`human_recent`). When the clients cannot
be listed the answer is unknown, and unknown does NOT refuse once hook state has
already proved the agent idle or queueing: the input check only guards a human
mid-typing in the focused pane, and blocking on an unreadable client list would
strand every nudge on a tmux hiccup. A forced Codex nudge has no hook proof, so
there unknown still refuses (`human_activity_unknown`).

Known limit: a draft a human typed and then left (focus moved, > 60 s ago) is
not detectable without reading the screen; a nudge then appends to it.

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
    # A literal line followed at once by Enter arrives as one burst, which the
    # Codex TUI treats as a paste and keeps the Enter as a newline in the
    # composer instead of submitting. A short pause makes Enter a keypress.
    ENTER_DELAY_SECONDS = 0.5

    SHELL_COMMANDS = frozenset({"sh", "bash", "fish", "zsh", "dash", "ksh", "tcsh"})
    # `Frame._interactive_command` prefix: every agent pane runs as
    # `sh -c "unset NO_COLOR; <driver>; ...; exec $SHELL"`.
    WRAPPER_PREFIX = "unset NO_COLOR;"

    def __init__(
        self,
        frame: Any,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.frame = frame
        self.clock = clock
        self.sleep = sleep

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

    def _status(self, session_id: str) -> dict[str, Any] | None:
        try:
            return self.frame.store.read("status", session_id)
        except Exception:
            return None

    # -- gates -----------------------------------------------------------

    def pane_gate(self, pane_id: str) -> tuple[bool, str, str | None]:
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
        return True, "pane_ok", None

    def driver_gate(
        self, record: dict[str, Any], *, force: bool
    ) -> tuple[bool, str]:
        """Per-driver rule from hook state, shared by `nudge` and `driver_rename`."""
        driver = (record.get("driver") or {}).get("id")
        status = self._status(record["id"])
        if driver == "claude":
            if not TurnSequence.observed(status, driver):
                return False, "state_unknown"
            if TurnSequence.attention_pending(status):
                return False, "claude_dialog_open"
            if TurnSequence.idle_proven(status):
                return True, "claude_idle"
            return True, "claude_queues_input"
        if driver != "codex":
            return False, "unsupported_driver"
        if force:
            return True, "forced"
        if not TurnSequence.idle_proven(status):
            return False, "codex_not_idle"
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
            "basis": "hooks",
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
            allowed, reason, detail = self.pane_gate(pane_id)
            if not allowed:
                return refuse(reason, detail)
            allowed, reason = self.driver_gate(record, force=force)
            if not allowed:
                return refuse(reason)
            human = self.human_recent(pane_id)
            if human:
                return refuse("human_recent")
            if human is None and reason == "forced":
                # unknown is not "no" when nothing else proved it safe
                return refuse("human_activity_unknown")
            sent = self._tmux(["send-keys", "-l", "-t", pane_id, "--", text])
            if sent.returncode != 0:
                return refuse("send_failed", (sent.stderr or "").strip() or None)
            self.sleep(self.ENTER_DELAY_SECONDS)
            entered = self._tmux(["send-keys", "-t", pane_id, "Enter"])
            if entered.returncode != 0:
                return refuse("enter_failed", (entered.stderr or "").strip() or None)
        result["sent"] = True
        result["reason"] = reason
        result["pane_id"] = pane_id
        return result
