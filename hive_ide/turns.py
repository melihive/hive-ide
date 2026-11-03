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
not blocked, and the status still says waiting (or `idle`, which is what a
viewed `waiting` becomes). It is always recomputed from `turns`, never
persisted, so another writer changing `state` can never leave a stale proof. Anything missing, malformed,
reset or from another driver is NOT proof — unknown routes to "not idle".

A Codex turn that ends in an ERROR (an API 400, a usage limit) runs no hook at
all: codex has no failure hook event and skips `Stop`, so the status written by
`UserPromptSubmit` would say `working` forever and every caller holding work
for an idle agent would hold it indefinitely. `UserPromptSubmit` therefore
records codex's `turn_id`, and `settle` closes that turn on read when codex's
own rollout records its end (`task_complete`, which codex also writes after an
error, or `turn_aborted`). The stored document is never rewritten by a reader:
the next hook event replaces it as usual.

Limitation: a `Stop` that ANOTHER hook blocks (decision "block") is invisible at
its own arrival; hooks run side by side and none sees the others' verdicts. The
agent then keeps working with no new `UserPromptSubmit`, so the counter alone
would call it idle. The next `Stop` in that chain carries `stop_hook_active`
(treated as blocked). The typing gates (`nudge.py`, used by `driver-rename`)
accept that residual window rather than read the screen, which proved
unreliable across pane widths.
"""

from __future__ import annotations

from typing import Any, Callable


class TurnSequence:
    PROMPT = "UserPromptSubmit"
    STOP = "Stop"
    NOTIFICATION = "Notification"
    HARMLESS_NOTIFICATIONS = frozenset({"idle_prompt"})
    # `seen.py` acks a viewed `waiting` session to `idle`; both mean "not working".
    RESTING_STATES = frozenset({"waiting", "idle"})
    # Not a hook event: the `last_event` of a turn `settle` closed from the
    # driver's own record because no `Stop` ever arrived for it.
    TURN_ENDED = "TurnEnded"
    TURN_ID_MAX = 128

    @staticmethod
    def _turn_id(value: Any) -> str | None:
        if isinstance(value, str) and 0 < len(value) <= TurnSequence.TURN_ID_MAX:
            return value
        return None

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
                "turn_id": TurnSequence._turn_id(turns.get("turn_id")),
                "stopped_turn": stopped
                if isinstance(stopped, int) and not isinstance(stopped, bool)
                else None,
                "stop_blocked": turns.get("stop_blocked") is True,
                "last_event": turns.get("last_event")
                if isinstance(turns.get("last_event"), str)
                else None,
                "notification_type": turns.get("notification_type")
                if isinstance(turns.get("notification_type"), str)
                else None,
            }
        return {
            "driver": driver,
            "turn": 0,
            "turn_id": None,
            "stopped_turn": None,
            "stop_blocked": False,
            "last_event": None,
            "notification_type": None,
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
            turns["turn_id"] = TurnSequence._turn_id(
                payload.get("turn_id") if isinstance(payload, dict) else None
            )
            turns["stop_blocked"] = False
        elif event == TurnSequence.STOP:
            turns["stopped_turn"] = turns["turn"]
            turns["stop_blocked"] = TurnSequence._truthy(
                payload.get("stop_hook_active") if isinstance(payload, dict) else None
            )
        turns["last_event"] = event
        kind = payload.get("notification_type") if isinstance(payload, dict) else None
        turns["notification_type"] = (
            kind[:40] if event == TurnSequence.NOTIFICATION and isinstance(kind, str) else None
        )
        turns["observed_at"] = observed_at
        return turns

    @staticmethod
    def observed(status: dict[str, Any] | None, driver: str) -> bool:
        """Has any hook of `driver` reported for this session?

        False before the first hook runs (e.g. while Claude shows its
        folder-trust prompt) and for state left by another driver.
        """
        turns = (status or {}).get("turns")
        return isinstance(turns, dict) and turns.get("driver") == driver

    @staticmethod
    def attention_pending(status: dict[str, Any] | None) -> bool:
        """Is the agent's latest hook event a Notification that may be a dialog?

        Claude raises `Notification` for permission prompts, plan approval,
        elicitation and more; Enter on any of them picks the highlighted option.
        Only the idle reminder is known to be harmless, so every other type —
        including a missing one — counts as pending until the next event.
        """
        turns = (status or {}).get("turns")
        if not isinstance(turns, dict) or turns.get("last_event") != TurnSequence.NOTIFICATION:
            return False
        return turns.get("notification_type") not in TurnSequence.HARMLESS_NOTIFICATIONS

    @staticmethod
    def idle_proven(status: dict[str, Any] | None) -> bool:
        if not isinstance(status, dict) or status.get("state") not in TurnSequence.RESTING_STATES:
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

    @staticmethod
    def settle(
        status: dict[str, Any] | None,
        turn_ended: Callable[[str, str], bool | None] | None = None,
    ) -> dict[str, Any] | None:
        """`status` with a Codex turn that ended without a `Stop` closed.

        Applies only to a `working` Codex status whose latest event is the
        `UserPromptSubmit` that opened turn `turns.turn_id`. When codex's rollout
        records that turn's end (`turn_ended` returns True), the result reads as
        if a clean `Stop` had closed it: `waiting`, `stopped_turn == turn`, and
        `last_event` `TurnEnded`. Anything else — another driver, no turn id, a
        later event, a rollout that cannot be read (None) — returns `status`
        unchanged: unknown is never proof the turn ended.

        Pure apart from the probe; the caller's document is not mutated.
        """
        if not isinstance(status, dict) or status.get("state") != "working":
            return status
        if status.get("driver") != "codex":
            return status
        turns = status.get("turns")
        if not isinstance(turns, dict) or turns.get("driver") != "codex":
            return status
        if turns.get("last_event") != TurnSequence.PROMPT:
            return status
        turn = turns.get("turn")
        if not isinstance(turn, int) or isinstance(turn, bool) or turn < 1:
            return status
        turn_id = TurnSequence._turn_id(turns.get("turn_id"))
        reference = status.get("conversation_reference")
        if turn_id is None or not isinstance(reference, str) or not reference:
            return status
        if turn_ended is None:
            from .drivers import ConversationProbe

            turn_ended = ConversationProbe.codex_turn_ended
        try:
            ended = turn_ended(reference, turn_id)
        except Exception:  # noqa: BLE001 — a probe failure is "unknown", not a verdict
            return status
        if ended is not True:
            return status
        return {
            **status,
            "state": "waiting",
            "settled_by": "codex_rollout",
            "turns": {
                **turns,
                "stopped_turn": turn,
                "stop_blocked": False,
                "last_event": TurnSequence.TURN_ENDED,
            },
        }
