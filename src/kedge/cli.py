"""kedge CLI — click-based entry point.

Command set mirrors backup.sh/restore.sh/verify.sh 1:1 (drop-in replacement
goal). `kedge restore` is the KEDGE-W-003 (Phase 2) port of restore.sh.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

import click

from kedge import __version__
from kedge.commands import cmd_backup, cmd_check, cmd_init, cmd_list, cmd_prune
from kedge.config import Config
from kedge.discovery import build_discover_report, compose_config, format_discover_report
from kedge.errors import KedgeError
from kedge.prereqs import check_prereqs
from kedge.restore import cmd_restore
from kedge.verify import VerifyConfig, cmd_burn, cmd_verify

DEFAULT_RESTORE_TARGET = "/opt/stack"


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
@_handle_kedge_errors
def init() -> None:
    """Initialize the restic repository."""
    cmd_init(Config.from_env())


@main.command()
@_handle_kedge_errors
def backup() -> None:
    """Run a full backup (discover + dump + collect + restic)."""
    cmd_backup(Config.from_env())


@main.command()
@click.argument("snapshot", required=False, default="latest")
@click.option("--verify", "verify_only", is_flag=True,
              help="Restore files only, don't start the stack. Docker volumes are always "
                   "restored under an isolated *_restoretest name.")
@click.option("--force-live", "force_live", is_flag=True,
              help="Only relevant for a real (non --verify) restore: skip the safety check "
                   "that refuses to overwrite a volume already mounted by a running container.")
@_handle_kedge_errors
def restore(snapshot: str, verify_only: bool, force_live: bool) -> None:
    """Restore a snapshot (bare-metal Docker Compose restore)."""
    restore_target = Path(os.environ.get("RESTORE_TARGET") or DEFAULT_RESTORE_TARGET)
    cmd_restore(Config.from_env(), restore_target, snapshot, verify_only, force_live)


@main.command()
@_handle_kedge_errors
def check() -> None:
    """Verify repository integrity."""
    cmd_check(Config.from_env())


@main.command()
@_handle_kedge_errors
def prune() -> None:
    """Remove old snapshots per retention policy."""
    cmd_prune(Config.from_env())


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
@_handle_kedge_errors
def list_snapshots() -> None:
    """List snapshots (alias: snapshots)."""
    cmd_list(Config.from_env())


main.add_command(list_snapshots, name="snapshots")


@main.command()
@click.argument("snapshot", required=False, default="latest")
@click.option("--keep", "keep_box", is_flag=True, help="Don't burn the box after verification (for debugging).")
@_handle_kedge_errors
def verify(snapshot: str, keep_box: bool) -> None:
    """Restore a snapshot onto a fresh Hetzner Cloud box and run health checks."""
    ok = cmd_verify(Config.from_env(), VerifyConfig.from_env(), snapshot, keep_box)
    if not ok:
        raise SystemExit(1)


@main.command()
@_handle_kedge_errors
def burn() -> None:
    """Burn leftover verify boxes."""
    cmd_burn(VerifyConfig.from_env())


if __name__ == "__main__":
    main()
