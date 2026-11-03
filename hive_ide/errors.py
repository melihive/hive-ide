"""Public error types."""


class HiveIdeError(Exception):
    """Base class for expected, user-facing failures."""


class UsageError(HiveIdeError):
    """The command or selected resource is invalid."""


class HeldConversationError(HiveIdeError):
    """A process outside the session's own agent pane holds its conversation, so
    no driver may start on it.

    Its own type because the right response differs by caller. A respawn or wake
    the user asked for must refuse and say why. A frame `open` must not: the
    collision belongs to ONE session, and treating it as fatal cost a fleet
    machine its IDE for three days (2026-10-30 to 11-02) because every session on
    it was an agent session, so the frame had no window it was allowed to build.
    """


class StateError(HiveIdeError):
    """Persisted state cannot be read or written safely."""


class SchemaVersionError(StateError):
    """A persisted document uses an incompatible schema."""


class CommandOutcome(HiveIdeError):
    """A command's structured non-success result: `payload` goes to stdout as
    JSON and `exit_code` is the process status (2 for a refusal, 1 for a
    failure). For callers that parse the result rather than read stderr."""

    def __init__(self, payload: dict, exit_code: int):
        self.payload = payload
        self.exit_code = exit_code
        super().__init__(str(payload.get("reason") or "failed"))
