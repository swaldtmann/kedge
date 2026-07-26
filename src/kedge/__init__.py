"""kedge — generic encrypted backup for Docker Compose stacks."""

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

# Single source of truth is pyproject.toml's version → dist-info →
# importlib.metadata. A hardcoded __version__ here drifted out of sync at the
# v0.5.0 release (stayed "0.4.0.dev0", stamped the wrong kedge_version into
# every meta.json). Fallback only for a source tree with no installed dist-info.
try:
    __version__ = version("kedge")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0+source"
