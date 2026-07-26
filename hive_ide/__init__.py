"""Public package surface for hive-ide."""

from importlib.metadata import PackageNotFoundError, version

PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1

try:
    __version__ = version("hive-ide")
except PackageNotFoundError:
    __version__ = "0.1.0.dev0"

__all__ = ["PROTOCOL_VERSION", "SCHEMA_VERSION", "__version__"]
