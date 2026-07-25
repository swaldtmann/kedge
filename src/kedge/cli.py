"""kedge CLI — click-based entry point.

Command set mirrors backup.sh 1:1 (Phase 1 goal: drop-in replacement).
Each command is a stub until its own KEDGE-W-001 sub-task lands the real
logic (discover -> #2, backup/init/list/check/prune -> #3).
"""

from __future__ import annotations

import functools
import json

import click

from kedge import __version__
from kedge.config import Config
from kedge.discovery import build_discover_report, compose_config, format_discover_report
from kedge.errors import KedgeError
from kedge.prereqs import check_prereqs


def _handle_kedge_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KedgeError as exc:
            click.echo(f"ERR  {exc}", err=True)
            raise SystemExit(1) from None

    return wrapper


@click.group()
@click.version_option(__version__, prog_name="kedge")
def main() -> None:
    """Generic encrypted backup for Docker Compose stacks."""


@main.command()
def init() -> None:
    """Initialize the restic repository."""
    raise NotImplementedError("kedge init: not yet ported (KEDGE-W-001 #3)")


@main.command()
def backup() -> None:
    """Run a full backup (discover + dump + collect + restic)."""
    raise NotImplementedError("kedge backup: not yet ported (KEDGE-W-001 #3)")


@main.command()
@click.argument("snapshot", required=False)
def restore(snapshot: str | None) -> None:
    """Restore from a snapshot (delegates to restore.sh for now)."""
    raise NotImplementedError("kedge restore: Phase 2 (KEDGE-W-001 follow-up)")


@main.command()
def check() -> None:
    """Verify repository integrity."""
    raise NotImplementedError("kedge check: not yet ported (KEDGE-W-001 #3)")


@main.command()
def prune() -> None:
    """Remove old snapshots per retention policy."""
    raise NotImplementedError("kedge prune: not yet ported (KEDGE-W-001 #3)")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable JSON output.")
@_handle_kedge_errors
def discover(as_json: bool) -> None:
    """Dry-run: show what would be backed up."""
    cfg = Config.from_env()
    prereqs = check_prereqs(cfg)
    config = compose_config(cfg.stack_dir, prereqs.compose_cmd)
    report = build_discover_report(
        stack_dir=cfg.stack_dir,
        compose_config_dict=config,
        exclude_volumes=cfg.exclude_volumes,
        exclude_mounts=cfg.exclude_mounts,
        system_paths=cfg.system_paths,
        system_paths_exclude=cfg.system_paths_exclude,
    )
    if as_json:
        click.echo(json.dumps(report, indent=2))
    else:
        click.echo(format_discover_report(report))


@main.command(name="list")
def list_snapshots() -> None:
    """List snapshots (alias: snapshots)."""
    raise NotImplementedError("kedge list: not yet ported (KEDGE-W-001 #3)")


main.add_command(list_snapshots, name="snapshots")


if __name__ == "__main__":
    main()
