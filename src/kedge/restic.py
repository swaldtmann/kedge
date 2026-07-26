"""restic wrapper — thin subprocess layer around backup.sh's restic calls
(init/backup/list/check/prune, lines 751-1006)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from kedge.config import Config
from kedge.errors import KedgeError


def _env(cfg: Config) -> dict:
    env = os.environ.copy()
    env["RESTIC_REPOSITORY"] = cfg.restic_repository
    if cfg.restic_password:
        env["RESTIC_PASSWORD"] = cfg.restic_password
    if cfg.restic_password_file:
        env["RESTIC_PASSWORD_FILE"] = cfg.restic_password_file
    return env


def repo_initialized(cfg: Config) -> bool:
    result = subprocess.run(
        ["restic", "snapshots", "--latest", "1"],
        env=_env(cfg), capture_output=True, check=False,
    )
    return result.returncode == 0


def init(cfg: Config) -> None:
    result = subprocess.run(["restic", "init"], env=_env(cfg))
    if result.returncode != 0:
        raise KedgeError("restic init failed")


def backup(cfg: Config, paths: list, excludes: list[str], tags: list[str], host: str) -> None:
    cmd = ["restic", "backup", *[str(p) for p in paths]]
    for excl in excludes:
        cmd += ["--exclude", excl]
    for tag in tags:
        cmd += ["--tag", tag]
    cmd += ["--host", host]
    result = subprocess.run(cmd, env=_env(cfg))
    if result.returncode != 0:
        raise KedgeError("restic backup failed")


def restore(cfg: Config, snapshot_id: str, target: Path) -> None:
    result = subprocess.run(
        ["restic", "restore", snapshot_id, "--target", str(target), "--tag", "kedge"],
        env=_env(cfg),
    )
    if result.returncode != 0:
        raise KedgeError(f"restic restore failed (snapshot: {snapshot_id})")


def latest_snapshot_short_id(cfg: Config) -> str:
    result = subprocess.run(
        ["restic", "snapshots", "--latest", "1", "--json"],
        env=_env(cfg), capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return "unknown"
    try:
        data = json.loads(result.stdout)
        return data[0]["short_id"] if data else "unknown"
    except (ValueError, KeyError, IndexError, TypeError):
        return "unknown"


def print_latest_snapshot(cfg: Config) -> None:
    subprocess.run(["restic", "snapshots", "--latest", "1"], env=_env(cfg), check=False)


def list_snapshots(cfg: Config) -> None:
    subprocess.run(["restic", "snapshots", "--tag", "kedge"], env=_env(cfg), check=False)


def check(cfg: Config) -> None:
    result = subprocess.run(["restic", "check"], env=_env(cfg))
    if result.returncode != 0:
        raise KedgeError("restic check failed — repository integrity issue")


def prune(cfg: Config, keep_daily: int, keep_weekly: int, keep_monthly: int) -> None:
    # --group-by tags (EWH-W-135): restic's default group-by is host,paths.
    # Tags are stable across runs ("kedge" + "stack:<name>"), grouping by
    # them keeps forget effective even if the staging path ever drifts.
    result = subprocess.run(
        [
            "restic", "forget",
            "--keep-daily", str(keep_daily),
            "--keep-weekly", str(keep_weekly),
            "--keep-monthly", str(keep_monthly),
            "--tag", "kedge",
            "--group-by", "tags",
            "--prune",
        ],
        env=_env(cfg),
    )
    if result.returncode != 0:
        raise KedgeError("restic prune failed")


def stats_size_formatted(cfg: Config) -> str:
    """backup.sh:963-967. Modern restic's `stats --json` has no
    total_size_formatted field (only a raw total_size byte count) — the
    shell version's fallback to parsing plain `restic stats`' "Total Size"
    line is load-bearing, not dead code (confirmed live: restic 0.19.0
    always needs it)."""
    result = subprocess.run(
        ["restic", "stats", "--json"], env=_env(cfg), capture_output=True, text=True, check=False,
    )
    try:
        data = json.loads(result.stdout)
        formatted = data.get("total_size_formatted")
        if formatted:
            return formatted
    except (ValueError, AttributeError):
        pass

    plain = subprocess.run(["restic", "stats"], env=_env(cfg), capture_output=True, text=True, check=False)
    for line in plain.stdout.splitlines():
        if "Total Size" in line:
            parts = line.split()
            if len(parts) >= 2:
                return " ".join(parts[-2:])
    return "unknown"
