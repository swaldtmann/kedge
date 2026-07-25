"""kedge error types."""


class KedgeError(Exception):
    """User-facing failure — mirrors backup.sh's die() (message + exit 1)."""
