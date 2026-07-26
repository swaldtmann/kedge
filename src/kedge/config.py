"""Env-var configuration — mirrors the "Config (overridable via env)" block
in backup.sh (lines 68-82).

No separate /etc/kedge-backup.env parser: cron sources that file into the
shell environment before invoking the binary (`. /etc/kedge-backup.env &&
kedge backup`), so os.environ already has everything by the time
Config.from_env() runs — same as it does for backup.sh. Nothing to port.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STAGING_BASE = "/var/lib/kedge/staging"


def _split_env(name: str) -> list[str]:
    value = os.environ.get(name, "")
    return value.split() if value else []


def _bool_env(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() == "true"


def _int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


@dataclass
class Config:
    stack_dir: Path
    restic_repository: str = ""
    restic_password: str = ""
    restic_password_file: str = ""
    exclude_volumes: list[str] = field(default_factory=list)
    exclude_mounts: list[str] = field(default_factory=list)
    system_paths: list[str] = field(default_factory=list)
    system_paths_exclude: list[str] = field(default_factory=list)
    sqlite_wal_checkpoint_paths: list[str] = field(default_factory=list)
    backup_stop_stack: bool = True
    keep_daily: int = 7
    keep_weekly: int = 4
    keep_monthly: int = 3
    staging_base: Path = field(default_factory=lambda: Path(DEFAULT_STAGING_BASE))
    backup_pre_hook: str = ""
    backup_post_hook: str = ""
    backup_fail_hook: str = ""
    backup_healthcheck_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        stack_dir = Path(os.environ.get("STACK_DIR") or Path.cwd())
        return cls(
            stack_dir=stack_dir,
            restic_repository=os.environ.get("RESTIC_REPOSITORY", ""),
            restic_password=os.environ.get("RESTIC_PASSWORD", ""),
            restic_password_file=os.environ.get("RESTIC_PASSWORD_FILE", ""),
            exclude_volumes=_split_env("BACKUP_EXCLUDE_VOLUMES"),
            exclude_mounts=_split_env("BACKUP_EXCLUDE_MOUNTS"),
            system_paths=_split_env("SYSTEM_PATHS"),
            system_paths_exclude=_split_env("SYSTEM_PATHS_EXCLUDE"),
            sqlite_wal_checkpoint_paths=_split_env("SQLITE_WAL_CHECKPOINT_PATHS"),
            backup_stop_stack=_bool_env("BACKUP_STOP_STACK", True),
            keep_daily=_int_env("BACKUP_KEEP_DAILY", 7),
            keep_weekly=_int_env("BACKUP_KEEP_WEEKLY", 4),
            keep_monthly=_int_env("BACKUP_KEEP_MONTHLY", 3),
            staging_base=Path(os.environ.get("KEDGE_STAGING_BASE") or DEFAULT_STAGING_BASE),
            backup_pre_hook=os.environ.get("BACKUP_PRE_HOOK", ""),
            backup_post_hook=os.environ.get("BACKUP_POST_HOOK", ""),
            backup_fail_hook=os.environ.get("BACKUP_FAIL_HOOK", ""),
            backup_healthcheck_url=os.environ.get("BACKUP_HEALTHCHECK_URL", ""),
        )
