"""Version/status file written per backup/prune run.

`<KEDGE_STATE_DIR>/<stack>.version` (default `/var/lib/kedge`), key=value,
one key per line. backup.sh (`write_version_file`) writes the identical format.

    kedge=<version>      installed dist-info version (= pyproject.toml), else "unknown"
    stack=<name>         basename of STACK_DIR
    run=backup|prune
    started=<ISO-8601 UTC>
    finished=<ISO-8601 UTC>
    result=ok|failed
    exit=<code>          only when result=failed
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import click

from kedge import __version__
from kedge.config import Config

DEFAULT_STATE_DIR = "/var/lib/kedge"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_dir() -> Path:
    return Path(os.environ.get("KEDGE_STATE_DIR") or DEFAULT_STATE_DIR)


def kedge_version() -> str:
    # __version__ comes from the installed dist-info, i.e. pyproject.toml's
    # version at install time. "0.0.0+source" = no dist-info -> unknown.
    return "unknown" if __version__.endswith("+source") else __version__


def write_version_file(cfg: Config, run: str, started: str, finished: str, exit_code: int) -> None:
    """Atomically write the status file. Never raises: failure -> warning on stderr."""
    stack = cfg.stack_dir.name
    lines = [
        f"kedge={kedge_version()}",
        f"stack={stack}",
        f"run={run}",
        f"started={started}",
        f"finished={finished}",
        f"result={'ok' if exit_code == 0 else 'failed'}",
    ]
    if exit_code != 0:
        lines.append(f"exit={exit_code}")
    target = state_dir() / f"{stack}.version"
    tmp_name = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{stack}.version.")
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, target)
        tmp_name = None
    except OSError as exc:
        click.echo(f"wrn  could not write version file {target}: {exc}", err=True)
    finally:
        if tmp_name:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


@contextlib.contextmanager
def record_run(cfg: Config, run: str):
    """Write the status file after the wrapped run; the exception/exit code passes through."""
    started = _now()
    code = 0
    try:
        yield
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        raise
    except BaseException:
        code = 1
        raise
    finally:
        write_version_file(cfg, run, started, _now(), code)
