"""Command orchestration — one function per CLI command, called from cli.py.
Ports backup.sh's cmd_init/cmd_backup/cmd_list/cmd_check/cmd_prune
(backup.sh:751-1006).
"""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from kedge import log, restic
from kedge.checksums import compute_backup_checksums, compute_dump_checksums
from kedge.collect import collect_stack_files, collect_volumes, write_metadata
from kedge.config import Config
from kedge.discovery import check_hot_safety, compose_config
from kedge.docker_stack import start_stack, stop_stack
from kedge.engines import checkpoint_wal_paths
from kedge.errors import KedgeError
from kedge.hooks import run_pre_hooks
from kedge.lifecycle_hooks import HookContext, ping_healthcheck, run_hook
from kedge.prereqs import check_prereqs
from kedge.system import hostname


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _filter_shadowing_excludes(backup_paths: list, excludes: list[str]) -> list[str]:
    """Drop SYSTEM_PATHS_EXCLUDE entries that would also match an explicit
    backup path (staging dir, direct volume paths). restic applies --exclude
    globally to the whole invocation, not scoped to SYSTEM_PATHS — an exclude
    meant only to keep the broad SYSTEM_PATHS scan from re-descending into
    paths handled elsewhere would otherwise silently empty those deliberate
    backup targets too (KEDGE-W-007: "/var/lib/docker/volumes" in
    SYSTEM_PATHS_EXCLUDE shadowed every direct Docker volume backup path)."""
    paths_str = [str(p) for p in backup_paths]
    kept = []
    for excl in excludes:
        shadows = any(bp == excl or bp.startswith(excl.rstrip("/") + "/") for bp in paths_str)
        if shadows:
            log.warn(f"SYSTEM_PATHS_EXCLUDE entry '{excl}' overlaps an explicit backup path — skipping this exclude so real data isn't dropped")
            continue
        kept.append(excl)
    return kept


def cmd_init(cfg: Config) -> None:
    check_prereqs(cfg)
    log.info(f"Initializing restic repository: {cfg.restic_repository}")
    restic.init(cfg)
    log.ok("Repository initialized")


def cmd_list(cfg: Config) -> None:
    check_prereqs(cfg)
    restic.list_snapshots(cfg)


def cmd_check(cfg: Config) -> None:
    check_prereqs(cfg)
    log.info("Checking repository integrity...")
    restic.check(cfg)
    log.ok("Repository OK")


def cmd_prune(cfg: Config) -> None:
    check_prereqs(cfg)
    log.info(f"Pruning old snapshots (keep: {cfg.keep_daily}d {cfg.keep_weekly}w {cfg.keep_monthly}m)...")
    restic.prune(cfg, cfg.keep_daily, cfg.keep_weekly, cfg.keep_monthly)
    log.ok("Prune complete")


def cmd_backup(cfg: Config) -> None:
    prereqs = check_prereqs(cfg)

    if not restic.repo_initialized(cfg):
        raise KedgeError("Restic repo not initialized. Run: kedge init")

    log.info("=== Backup started ===")
    log.info(f"Stack: {cfg.stack_dir}")
    log.info(f"Target: {cfg.restic_repository}")
    if not cfg.backup_stop_stack:
        log.info("Mode: HOT BACKUP (stack stays running)")

    start_time = time.monotonic()

    # Stable staging path (#18 in the original repo) so restic finds the
    # parent snapshot for incremental scans — a random per-run path would
    # make restic re-walk every file on each invocation.
    cfg.staging_base.mkdir(parents=True, exist_ok=True)
    staging_dir = cfg.staging_base / cfg.stack_dir.name
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    was_running = False
    try:
        config = compose_config(cfg.stack_dir, prereqs.compose_cmd)

        if not cfg.backup_stop_stack:
            all_safe, _ = check_hot_safety(config)
            if not all_safe:
                log.warn("Proceeding with hot backup despite unsafe services — data may be inconsistent")

        run_hook(cfg.backup_pre_hook, "pre-hook", HookContext())

        log.info("--- Phase 1: Database dumps ---")
        run_pre_hooks(config, cfg.stack_dir, prereqs.compose_cmd, staging_dir / "dumps")

        if cfg.sqlite_wal_checkpoint_paths:
            # KEDGE-W-004: SQLite (e.g. prod-poki) has no container image to
            # auto-discover a dump hook for -- its bind-mount already gets
            # tarred like any other external mount (collect.py), this just
            # makes the plain .db file in that mount self-consistent first.
            log.info("--- Phase 1b: SQLite WAL checkpoints ---")
            checkpoint_wal_paths(cfg.sqlite_wal_checkpoint_paths)

        log.info("--- Phase 2: Volume collection ---")
        was_running = stop_stack(cfg.stack_dir, prereqs.compose_cmd, cfg.backup_stop_stack)
        volume_backup_paths = collect_volumes(config, staging_dir / "volumes", cfg.exclude_volumes)

        log.info("--- Phase 3: Stack files ---")
        collect_stack_files(cfg.stack_dir, config, staging_dir, cfg.exclude_mounts)

        checksums = {
            "volumes": compute_backup_checksums(config, staging_dir / "volumes", cfg.exclude_volumes),
            "dumps": compute_dump_checksums(staging_dir / "dumps"),
        }
        write_metadata(cfg.stack_dir, config, prereqs.compose_cmd, staging_dir, checksums=checksums)

        log.info("--- Phase 5: Restic backup ---")
        hostname_str = hostname()
        backup_paths: list[Path | str] = [staging_dir, *volume_backup_paths]
        for sp in cfg.system_paths:
            if Path(sp).exists():
                backup_paths.append(sp)
            else:
                log.warn(f"SYSTEM_PATHS entry not found, skipping: {sp}")

        restic_excludes = _filter_shadowing_excludes(backup_paths, cfg.system_paths_exclude)
        size = restic.backup(
            cfg, backup_paths, restic_excludes,
            tags=["kedge", f"stack:{cfg.stack_dir.name}"], host=hostname_str,
        )

        start_stack(cfg.stack_dir, prereqs.compose_cmd, was_running)
        was_running = False  # started above — don't double-start in the except/finally paths
    except Exception as exc:
        if was_running:
            start_stack(cfg.stack_dir, prereqs.compose_cmd, was_running)
        fail_ctx = HookContext(
            hostname=hostname(), stack=cfg.stack_dir.name,
            timestamp=_utc_timestamp(), error=str(exc),
        )
        run_hook(cfg.backup_fail_hook, "fail-hook", fail_ctx)
        ping_healthcheck(cfg.backup_healthcheck_url, "fail", fail_ctx)
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    duration = int(time.monotonic() - start_time)
    snapshot = restic.latest_snapshot_short_id(cfg)

    log.ok(f"=== Backup complete ({duration}s) ===")
    restic.print_latest_snapshot(cfg)

    ok_ctx = HookContext(
        duration=str(duration), size=size, snapshot=snapshot,
        hostname=hostname(), stack=cfg.stack_dir.name, timestamp=_utc_timestamp(),
    )
    run_hook(cfg.backup_post_hook, "post-hook", ok_ctx)
    ping_healthcheck(cfg.backup_healthcheck_url, "ok", ok_ctx)
