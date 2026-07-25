"""Volume collection, stack-file collection, metadata — port of backup.sh
lines 502-696 (collect_volumes, collect_stack_files, write_metadata)."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from kedge import __version__, log
from kedge.discovery import (
    discover_bind_mounts,
    discover_volumes,
    is_excluded_mount,
    is_excluded_volume,
    resolve_volume_name,
    resolve_volume_path,
)
from kedge.system import hostname

# Schema of meta.json / snapshot layout — bump only on a breaking on-disk
# format change (backup.sh:58-62).
BACKUP_FORMAT_VERSION = "1.0.0"

STACK_COMPOSE_FILES = (
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "docker-compose.override.yml", "docker-compose.override.yaml",
)
ENV_FILES = (".env", ".env.local", ".env.production")


def _du(path: Path) -> str:
    flag = "-sh" if path.is_dir() else "-h"
    result = subprocess.run(["du", flag, str(path)], capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return "unknown"
    return result.stdout.split()[0]


def collect_volumes(config: dict, vol_map_dir: Path, exclude_volumes: list[str]) -> list[str]:
    """backup.sh:509-557. Returns the list of directly-mountable host paths
    for restic to back up (tar-fallback volumes are staged into vol_map_dir
    instead and don't need a separate restic path)."""
    vol_map_dir.mkdir(parents=True, exist_ok=True)
    backup_paths: list[str] = []
    count = 0

    for vol_name in discover_volumes(config):
        if is_excluded_volume(vol_name, exclude_volumes):
            log.info(f"Skipping excluded volume: {vol_name}")
            continue

        real_vol = resolve_volume_name(vol_name)
        if not real_vol:
            log.warn(f"Volume '{vol_name}' not found in Docker — skipping")
            continue

        vol_path = resolve_volume_path(real_vol)
        if not vol_path or not Path(vol_path).is_dir():
            log.warn(f"Volume '{real_vol}' mountpoint not accessible — falling back to tar export")
            tar_path = vol_map_dir / f"{vol_name}.tar.gz"
            subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{real_vol}:/data:ro",
                    "-v", f"{vol_map_dir}:/backup",
                    "alpine", "tar", "czf", f"/backup/{vol_name}.tar.gz", "-C", "/data", ".",
                ],
                check=False,
            )
            log.ok(f"  {real_vol} -> {vol_name}.tar.gz ({_du(tar_path)}) [tar fallback]")
        else:
            backup_paths.append(vol_path)
            log.ok(f"  {real_vol} -> {vol_path} ({_du(Path(vol_path))}) [direct]")

        count += 1

    log.ok(f"{count} volume(s) collected")
    return backup_paths


def collect_stack_files(stack_dir: Path, config: dict, target_dir: Path, exclude_mounts: list[str]) -> None:
    """backup.sh:563-645."""
    target_dir.mkdir(parents=True, exist_ok=True)

    for name in (*STACK_COMPOSE_FILES, *ENV_FILES):
        src = stack_dir / name
        if src.is_file():
            shutil.copy2(src, target_dir / name)

    external_mounts: list[str] = []
    for mount_src in discover_bind_mounts(config):
        if mount_src.startswith("/"):
            abs_mount = mount_src
        else:
            abs_mount = str((stack_dir / mount_src).resolve())

        if is_excluded_mount(abs_mount, exclude_mounts):
            log.info(f"Skipping excluded mount: {abs_mount}")
            continue

        stack_dir_str = str(stack_dir)
        if abs_mount == stack_dir_str or abs_mount.startswith(stack_dir_str + "/"):
            continue  # inside stack dir — captured by the rsync below
        external_mounts.append(abs_mount)

    stack_copy_target = target_dir / "stack-dir"
    subprocess.run(
        [
            "rsync", "-a", "--relative",
            "--exclude=.git", "--exclude=__pycache__", "--exclude=node_modules",
            "--exclude=.venv", "--exclude=*.pyc",
            f"{stack_dir}/./", f"{stack_copy_target}/",
        ],
        check=True,
    )

    if external_mounts:
        ext_dir = target_dir / "external-mounts"
        ext_dir.mkdir(parents=True, exist_ok=True)
        for mount in external_mounts:
            mount_path = Path(mount)
            if mount_path.is_socket():
                log.warn(f"Skipping socket: {mount}")
                continue
            if not mount_path.exists():
                log.warn(f"External bind mount not found: {mount}")
                continue
            mount_name = mount.strip("/").replace("/", "_")
            log.info(f"Backing up external bind mount: {mount}")
            if mount_path.is_dir():
                subprocess.run(
                    ["tar", "czf", str(ext_dir / f"{mount_name}.tar.gz"),
                     "-C", str(mount_path.parent), mount_path.name],
                    check=True,
                )
            else:
                shutil.copy2(mount_path, ext_dir / mount_path.name)

    log.ok("Stack files collected")


def write_metadata(stack_dir: Path, config: dict, compose_cmd: list[str], target: Path) -> None:
    """backup.sh:651-696."""
    hostname_str = hostname()

    ps_result = subprocess.run(
        [*compose_cmd, "ps", "--format", "json"],
        cwd=stack_dir, capture_output=True, text=True, check=False,
    )
    containers = []
    if ps_result.returncode == 0:
        for line in ps_result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            containers.append({"name": item.get("Name"), "image": item.get("Image"), "state": item.get("State")})

    vol_map: dict[str, str] = {}
    vol_paths: dict[str, str] = {}
    for vol_name in discover_volumes(config):
        real_vol = resolve_volume_name(vol_name)
        if real_vol:
            vol_map[vol_name] = real_vol
            vol_path = resolve_volume_path(real_vol)
            if vol_path:
                vol_paths[vol_name] = vol_path

    docker_version_result = subprocess.run(["docker", "--version"], capture_output=True, text=True, check=False)
    docker_version = docker_version_result.stdout.strip() if docker_version_result.returncode == 0 else ""

    metadata = {
        "format_version": BACKUP_FORMAT_VERSION,
        "kedge_version": __version__,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hostname": hostname_str,
        "stack_dir": str(stack_dir),
        "compose_cmd": " ".join(compose_cmd),
        "containers": containers,
        "volume_mapping": vol_map,
        "volume_paths": vol_paths,
        "docker_version": docker_version,
    }
    (target / "meta.json").write_text(json.dumps(metadata, indent=4) + "\n")
    log.ok("Metadata written")
