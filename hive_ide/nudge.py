"""Typing-safety gates for `driver-rename`, the one remaining typed input.

hive-ide never types a message into an agent pane. Relay delivery is native
(Codex `codex queue`, Claude's asyncRewake waiter), and a session that needs a
first instruction gets it at launch: `create --prompt=<TEXT>` passes it to the
driver as its initial prompt argument, so nothing is typed. The old `nudge`
command, which typed a caller's line plus Enter into a pane, is gone.

What remains is `driver-rename`: the human asks for it from the options modal,
and it types `/rename <name>` into their own agent pane. These gates decide
whether that is safe. Neither reads the screen: the user resizes and hides panes
constantly, so safety is decided from what the process table, tmux and the
agent's own hooks report.

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
  the latest turn and was not a stop-hook continuation — or, for a turn that
  ended in an error (codex runs no hook then), codex's own rollout records the
  end of the turn the last prompt opened (`TurnSequence.settle`). `force` skips
  only that check, and only an explicit caller passes it.
- Any other driver is unsupported.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Callable

from .turns import TurnSequence


class AgentNudge:
    TMUX_TIMEOUT = 3.0
    # A literal line followed at once by Enter arrives as one burst, which the
    # Codex TUI treats as a paste and keeps the Enter as a newline in the
    # composer instead of submitting. A short pause makes Enter a keypress
    # (`Frame.driver_rename`).
    ENTER_DELAY_SECONDS = 0.5

    SHELL_COMMANDS = frozenset({"sh", "bash", "fish", "zsh", "dash", "ksh", "tcsh"})
    # `Frame._interactive_command` prefix: every agent pane runs as
    # `sh -c "unset NO_COLOR; <driver>; ...; exec $SHELL"`.
    WRAPPER_PREFIX = "unset NO_COLOR;"

    def __init__(
        self,
        frame: Any,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.frame = frame
        self.sleep = sleep

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

    def _status(self, session_id: str) -> dict[str, Any] | None:
        try:
            # A codex turn that ended in an error left `working` (no Stop hook).
            return TurnSequence.settle(self.frame.store.read("status", session_id))
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
        """Per-driver rule from hook state (`driver_rename`)."""
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
