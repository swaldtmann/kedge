"""Restore — port of restore.sh:106-433 (cmd_restore). Bare-metal restore:
stack files, external bind mounts, Docker volumes (direct + tar fallback),
DB dump import. Live-volume guard (CW-W-243): --verify never restores into
a real, potentially-live volume name, even on the same host the backup was
taken from.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import click

from kedge import log, restic
from kedge.checksums import verify_restore_checksums
from kedge.config import Config
from kedge.discovery import compose_config, detect_db_type, discover_services
from kedge.engines import ENGINES
from kedge.errors import KedgeError
from kedge.prereqs import detect_compose_cmd

STACK_COMPOSE_FILES = (
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "docker-compose.override.yml", "docker-compose.override.yaml",
)
ENV_FILES = (".env", ".env.local", ".env.production")


def _check_restore_prereqs(cfg: Config) -> list[str]:
    """restore.sh:62-90. Deliberately no compose-file-exists check — unlike
    backup, restore's target directory starts out empty."""
    missing = [tool for tool in ("docker", "restic") if shutil.which(tool) is None]
    if missing:
        raise KedgeError(f"Missing required tools: {' '.join(missing)}")
    compose_cmd = detect_compose_cmd()
    if not cfg.restic_repository:
        raise KedgeError("RESTIC_REPOSITORY not set")
    if not cfg.restic_password and not cfg.restic_password_file:
        raise KedgeError("RESTIC_PASSWORD or RESTIC_PASSWORD_FILE not set")
    return compose_cmd


def _find_backup_root(staging_dir: Path) -> Path:
    """restore.sh:140-148 — restic preserves the full staging path; find
    meta.json under either the stable staging layout (*/staging/<stack>/)
    or legacy mktemp paths (*/kedge-staging.XXXXXX), for compat with old
    snapshots (#18)."""
    candidates = sorted(
        p for p in staging_dir.rglob("meta.json")
        if "/staging/" in p.as_posix() or "/kedge-staging" in p.as_posix()
    )
    if not candidates:
        raise KedgeError("No meta.json found in snapshot — is this a kedge snapshot?")
    return candidates[0].parent


def _restore_stack_files(backup_root: Path, restore_target: Path) -> None:
    """restore.sh:163-191."""
    restore_target.mkdir(parents=True, exist_ok=True)

    stack_content = backup_root / "stack-dir"
    if stack_content.is_dir():
        subprocess.run(["rsync", "-a", f"{stack_content}/", f"{restore_target}/"], check=True)
        log.ok(f"Stack files restored to {restore_target}")

    for name in (*STACK_COMPOSE_FILES, *ENV_FILES):
        src = backup_root / name
        if src.is_file():
            shutil.copy2(src, restore_target / name)
            log.ok(f"  Restored: {name}")


def _restore_external_mounts(backup_root: Path) -> None:
    """restore.sh:193-210."""
    ext_dir = backup_root / "external-mounts"
    if not ext_dir.is_dir():
        log.info("No external bind mounts to restore")
        return
    for archive in sorted(ext_dir.glob("*.tar.gz")):
        mount_name = archive.name[: -len(".tar.gz")]
        mount_path = Path("/" + mount_name.replace("_", "/"))
        log.info(f"Restoring external mount: {mount_path}")
        mount_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["tar", "xzf", str(archive), "-C", str(mount_path)], check=True)
        log.ok(f"  {mount_path} restored")


def _project_name(restore_target: Path) -> str:
    """restore.sh:226-228 — fallback derivation when meta.json's
    vol_mapping doesn't know the real volume name."""
    import re

    return re.sub(r"[^a-z0-9]", "", restore_target.name.lower())


def _volume_exists(vol_name: str) -> bool:
    result = subprocess.run(["docker", "volume", "inspect", vol_name], capture_output=True, check=False)
    return result.returncode == 0


def _volume_mounted_by_running_container(vol_name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-q", "--filter", f"volume={vol_name}"],
        capture_output=True, text=True, check=False,
    )
    return bool(result.stdout.strip())


def _volume_mountpoint(vol_name: str) -> Path:
    subprocess.run(["docker", "volume", "create", vol_name], capture_output=True, check=False)
    result = subprocess.run(
        ["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", vol_name],
        capture_output=True, text=True, check=False,
    )
    return Path(result.stdout.strip())


def _restore_volumes(
    staging_dir: Path, backup_root: Path, meta: dict, restore_target: Path,
    verify_only: bool, force_live: bool,
) -> dict[str, Path]:
    """restore.sh:212-296. Returns {vol_name: restored_mountpoint} — used
    for the checksum-verify step below."""
    vol_mapping: dict[str, str] = meta.get("volume_mapping") or {}
    vol_paths: dict[str, str] = meta.get("volume_paths") or {}
    restored: dict[str, Path] = {}

    for vol_name, real_vol_name in vol_mapping.items():
        real_vol_name = real_vol_name or f"{_project_name(restore_target)}_{vol_name}"

        if verify_only:
            # CW-W-243: --verify must never write into the real, potentially-live
            # volume — always restore under an isolated name instead.
            restore_vol_name = f"{real_vol_name}_restoretest"
        else:
            restore_vol_name = real_vol_name
            if _volume_exists(real_vol_name) and _volume_mounted_by_running_container(real_vol_name):
                if not force_live:
                    raise KedgeError(
                        f"Volume '{real_vol_name}' already exists AND is mounted by a running "
                        f"container — this restore target looks like the live backup source "
                        f"host. Refusing to overwrite live data. Re-run with --force-live if "
                        f"this is really intended."
                    )
                log.warn(f"  --force-live set: overwriting '{real_vol_name}' while mounted by a running container")

        new_vol_path = _volume_mountpoint(restore_vol_name)

        orig_vol_path = vol_paths.get(vol_name)
        restored_vol_dir = None
        if orig_vol_path:
            matches = sorted(
                p for p in staging_dir.rglob("*") if p.is_dir() and p.as_posix().endswith(orig_vol_path)
            )
            restored_vol_dir = matches[0] if matches else None

        tar_path = backup_root / "volumes" / f"{vol_name}.tar.gz"
        if restored_vol_dir is not None:
            log.info(f"Restoring volume: {vol_name} -> {restore_vol_name} [direct]")
            subprocess.run(["rsync", "-a", "--delete", f"{restored_vol_dir}/", f"{new_vol_path}/"], check=True)
            log.ok(f"  {restore_vol_name} restored [direct]")
        elif tar_path.is_file():
            log.info(f"Restoring volume: {vol_name} -> {restore_vol_name} [tar]")
            subprocess.run(
                [
                    "docker", "run", "--rm",
                    "-v", f"{restore_vol_name}:/data",
                    "-v", f"{tar_path.parent}:/backup:ro",
                    "alpine", "sh", "-c",
                    f"rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/{tar_path.name} -C /data",
                ],
                check=True,
            )
            log.ok(f"  {restore_vol_name} restored [tar]")
        else:
            log.warn(f"  No data found for volume {vol_name} — skipping")
            continue

        restored[vol_name] = new_vol_path

    if restored:
        log.ok(f"{len(restored)} volume(s) restored")
    else:
        log.info("No volumes to restore")

    return restored


# KEDGE-W-004: per-engine import implementations (_import_postgres et al.)
# moved to kedge.engines, alongside their matching dump() -- one place per
# engine instead of a hand-synced copy here. Derived straight from the
# registry so a new engine's dump_suffix/import_ automatically participates,
# no edit needed in this module.
_DUMP_IMPORTERS = tuple(
    (engine.dump_suffix, engine.import_) for engine in ENGINES if engine.dump_suffix and engine.import_
)


def _import_dumps(compose_cmd: list[str], restore_target: Path, dumps_dir: Path) -> None:
    """restore.sh:309-408 — start DB services first, wait, import dumps."""
    if not dumps_dir.is_dir() or not any(dumps_dir.iterdir()):
        return

    config = compose_config(restore_target, compose_cmd)
    db_services = {svc for svc, image in discover_services(config) if detect_db_type(image)}
    if not db_services:
        return

    log.info("Starting database containers for dump import...")
    subprocess.run([*compose_cmd, "up", "-d", *sorted(db_services)], cwd=restore_target, check=True)
    log.info("Waiting for databases to be ready...")
    time.sleep(15)

    for dump_path in sorted(dumps_dir.iterdir()):
        for suffix, importer in _DUMP_IMPORTERS:
            if dump_path.name.endswith(suffix):
                svc = dump_path.name[: -len(suffix)]
                log.info(f"Importing dump: {dump_path.name} -> {svc}")
                importer(compose_cmd, restore_target, dump_path, svc)
                break


def cmd_restore(
    cfg: Config, restore_target: Path, snapshot_id: str = "latest",
    verify_only: bool = False, force_live: bool = False,
) -> None:
    compose_cmd = _check_restore_prereqs(cfg)
    restore_target = Path(restore_target)

    log.info("=== Restore started ===")
    log.info(f"Repository: {cfg.restic_repository}")
    log.info(f"Snapshot: {snapshot_id}")
    log.info(f"Target: {restore_target}")

    staging_dir = Path(tempfile.mkdtemp(prefix="kedge-restore."))
    try:
        log.info("--- Phase 1: Restic restore ---")
        restic.restore(cfg, snapshot_id, staging_dir)

        backup_root = _find_backup_root(staging_dir)
        log.ok(f"Backup data found at: {backup_root}")

        meta = json.loads((backup_root / "meta.json").read_text())
        log.info(f"Original stack was at: {meta.get('stack_dir')}")
        click.echo("")
        click.echo("--- Backup metadata ---")
        click.echo(json.dumps(meta, indent=2))
        click.echo("")

        log.info("--- Phase 2: Restore stack files ---")
        _restore_stack_files(backup_root, restore_target)

        log.info("--- Phase 3: Restore external bind mounts ---")
        _restore_external_mounts(backup_root)

        log.info("--- Phase 4: Restore Docker volumes ---")
        restored_volumes = _restore_volumes(staging_dir, backup_root, meta, restore_target, verify_only, force_live)

        checksums = meta.get("checksums")
        if checksums and (checksums.get("volumes") or checksums.get("dumps")):
            log.info("--- Checksum verification ---")
            mismatches = verify_restore_checksums(checksums, restored_volumes, backup_root / "dumps")
            if mismatches:
                raise KedgeError("Restore checksum verification FAILED: " + "; ".join(mismatches))
            log.ok("All checksums match — restored data is byte-identical to the source")
        else:
            log.info("No checksum manifest in this snapshot — skipping integrity verification")

        if verify_only:
            log.ok("=== Verify complete — stack NOT started ===")
            log.info(f"Files restored to: {restore_target}")
            if restored_volumes:
                log.info("Docker volumes restored under isolated *_restoretest names — no live volume was touched.")
            log.info(f"To start: cd {restore_target} && {' '.join(compose_cmd)} up -d")
            return

        log.info("--- Phase 5: Start stack ---")
        _import_dumps(compose_cmd, restore_target, backup_root / "dumps")

        log.info("Starting full stack...")
        subprocess.run([*compose_cmd, "up", "-d"], cwd=restore_target, check=True)
        time.sleep(5)
        click.echo("")
        click.echo("--- Container status ---")
        subprocess.run([*compose_cmd, "ps"], cwd=restore_target, check=False)
        click.echo("")

        log.ok("=== Restore complete ===")
        log.info(f"Stack running at: {restore_target}")
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
