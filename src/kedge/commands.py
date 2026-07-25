"""Command orchestration — one function per CLI command, called from cli.py.
Ports backup.sh's cmd_init/cmd_backup/cmd_list/cmd_check/cmd_prune
(backup.sh:751-1006).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from kedge import log, restic
from kedge.collect import collect_stack_files, collect_volumes, write_metadata
from kedge.config import Config
from kedge.discovery import check_hot_safety, compose_config
from kedge.docker_stack import start_stack, stop_stack
from kedge.errors import KedgeError
from kedge.hooks import run_pre_hooks
from kedge.prereqs import check_prereqs
from kedge.system import hostname


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

    config = compose_config(cfg.stack_dir, prereqs.compose_cmd)

    if not cfg.backup_stop_stack:
        all_safe, _ = check_hot_safety(config)
        if not all_safe:
            log.warn("Proceeding with hot backup despite unsafe services — data may be inconsistent")

    was_running = False
    try:
        log.info("--- Phase 1: Database dumps ---")
        run_pre_hooks(config, cfg.stack_dir, prereqs.compose_cmd, staging_dir / "dumps")

        log.info("--- Phase 2: Volume collection ---")
        was_running = stop_stack(cfg.stack_dir, prereqs.compose_cmd, cfg.backup_stop_stack)
        volume_backup_paths = collect_volumes(config, staging_dir / "volumes", cfg.exclude_volumes)

        log.info("--- Phase 3: Stack files ---")
        collect_stack_files(cfg.stack_dir, config, staging_dir, cfg.exclude_mounts)

        write_metadata(cfg.stack_dir, config, prereqs.compose_cmd, staging_dir)

        log.info("--- Phase 5: Restic backup ---")
        hostname_str = hostname()
        backup_paths: list[Path | str] = [staging_dir, *volume_backup_paths]
        for sp in cfg.system_paths:
            if Path(sp).exists():
                backup_paths.append(sp)
            else:
                log.warn(f"SYSTEM_PATHS entry not found, skipping: {sp}")

        restic.backup(
            cfg, backup_paths, cfg.system_paths_exclude,
            tags=["kedge", f"stack:{cfg.stack_dir.name}"], host=hostname_str,
        )

        start_stack(cfg.stack_dir, prereqs.compose_cmd, was_running)
        was_running = False  # started above — don't double-start in finally
    finally:
        if was_running:
            start_stack(cfg.stack_dir, prereqs.compose_cmd, was_running)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)

    duration = int(time.monotonic() - start_time)
    log.ok(f"=== Backup complete ({duration}s) ===")
    restic.print_latest_snapshot(cfg)
