"""Env-var configuration — mirrors the "Config (overridable via env)" block
in backup.sh (lines 68-82). Kept minimal for Phase 1 skeleton needs
(discovery); restic/hook-specific fields land with their own sub-tasks
(#3, #5) so this dataclass grows incrementally instead of guessing ahead.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _split_env(name: str) -> list[str]:
    value = os.environ.get(name, "")
    return value.split() if value else []


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
        )
