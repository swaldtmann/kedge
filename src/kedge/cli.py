"""kedge CLI — click-based entry point.

Command set mirrors backup.sh 1:1 (Phase 1 goal: drop-in replacement).
Each command is a stub until its own KEDGE-W-001 sub-task lands the real
logic (discover -> #2, backup/init/list/check/prune -> #3).
"""

from __future__ import annotations

import click

from kedge import __version__


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
def discover(as_json: bool) -> None:
    """Dry-run: show what would be backed up."""
    raise NotImplementedError("kedge discover: not yet ported (KEDGE-W-001 #2)")


@main.command(name="list")
def list_snapshots() -> None:
    """List snapshots (alias: snapshots)."""
    raise NotImplementedError("kedge list: not yet ported (KEDGE-W-001 #3)")


main.add_command(list_snapshots, name="snapshots")


if __name__ == "__main__":
    main()
