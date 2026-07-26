"""Public error types."""


class HiveIdeError(Exception):
    """Base class for expected, user-facing failures."""


class UsageError(HiveIdeError):
    """The command or selected resource is invalid."""


class StateError(HiveIdeError):
    """Persisted state cannot be read or written safely."""


class SchemaVersionError(StateError):
    """A persisted document uses an incompatible schema."""
