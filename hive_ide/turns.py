"""Turn sequencing in hook state, and the idle proof derived from it.

Hook state alone says "waiting" for several different things: a turn that ended
(`Stop`), a permission prompt or idle reminder in the middle of one
(`Notification`), a fresh conversation (`SessionStart`). Typing into Codex while
it works interrupts its turn, so "waiting" is not enough to type into it. This
module keeps a per-session turn counter in the status document so a caller can
ask a narrower question: did the most recent turn END, cleanly?

- `UserPromptSubmit` opens a turn (counter + 1).
- `Stop` records the turn it closes, and whether it is part of a stop-hook
  continuation (`stop_hook_active`) — in which case it is treated as blocked.
- Every other event (`Notification`, `SessionStart`, …) changes nothing but
  `last_event`: none of them is proof the agent is idle.

`idle_proven` is True only when the latest `Stop` closes the latest turn, was
not blocked, and the status still says waiting. Anything missing, malformed,
reset or from another driver is NOT proof — unknown routes to "not idle".

Limitation: a `Stop` that ANOTHER hook blocks (decision "block") is invisible at
its own arrival; hooks run side by side and none sees the others' verdicts. The
agent then keeps working with no new `UserPromptSubmit`, so the counter alone
would call it idle. The next `Stop` in that chain carries `stop_hook_active`
(treated as blocked), and callers that type into a pane must also check the pane
itself (see `nudge.py`) — the counter is necessary, never sufficient.
"""

from __future__ import annotations

from typing import Any


class TurnSequence:
    PROMPT = "UserPromptSubmit"
    STOP = "Stop"

    @staticmethod
    def event_name(payload: dict[str, Any], requested_state: str) -> str | None:
        """The hook event this payload reports, or None when it cannot be told.

        Claude and Codex both send `hook_event_name`. Without it, only `working`
        is unambiguous (only `UserPromptSubmit` maps to it); a bare `waiting`
        could be `Stop`, `Notification` or `SessionStart` and stays unknown.
        """
        name = payload.get("hook_event_name") if isinstance(payload, dict) else None
        if isinstance(name, str) and name:
            return name
        if requested_state == "working":
            return TurnSequence.PROMPT
        return None

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
        return False

    @staticmethod
    def carried(previous: dict[str, Any] | None, driver: str) -> dict[str, Any]:
        turns = (previous or {}).get("turns")
        if (
            isinstance(turns, dict)
            and turns.get("driver") == driver
            and isinstance(turns.get("turn"), int)
            and not isinstance(turns.get("turn"), bool)
            and turns["turn"] >= 0
        ):
            stopped = turns.get("stopped_turn")
            return {
                "driver": driver,
                "turn": turns["turn"],
                "stopped_turn": stopped
                if isinstance(stopped, int) and not isinstance(stopped, bool)
                else None,
                "stop_blocked": turns.get("stop_blocked") is True,
                "last_event": turns.get("last_event")
                if isinstance(turns.get("last_event"), str)
                else None,
            }
        return {
            "driver": driver,
            "turn": 0,
            "stopped_turn": None,
            "stop_blocked": False,
            "last_event": None,
        }

    @staticmethod
    def advance(
        previous: dict[str, Any] | None,
        *,
        driver: str,
        event: str | None,
        payload: dict[str, Any],
        observed_at: str,
    ) -> dict[str, Any]:
        turns = TurnSequence.carried(previous, driver)
        if event == TurnSequence.PROMPT:
            turns["turn"] += 1
            turns["stop_blocked"] = False
        elif event == TurnSequence.STOP:
            turns["stopped_turn"] = turns["turn"]
            turns["stop_blocked"] = TurnSequence._truthy(
                payload.get("stop_hook_active") if isinstance(payload, dict) else None
            )
        turns["last_event"] = event
        turns["observed_at"] = observed_at
        return turns

    @staticmethod
    def idle_proven(status: dict[str, Any] | None) -> bool:
        if not isinstance(status, dict) or status.get("state") != "waiting":
            return False
        turns = status.get("turns")
        if not isinstance(turns, dict) or turns.get("driver") != status.get("driver"):
            return False
        turn = turns.get("turn")
        stopped = turns.get("stopped_turn")
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            return False
        if not isinstance(stopped, int) or isinstance(stopped, bool):
            return False
        return stopped == turn and turns.get("stop_blocked") is False
