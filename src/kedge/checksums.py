"""Checksum verify — KEDGE-W-003 #3 (roadmap issue #1), never built in v0.3.x.

Backup side computes a content fingerprint per volume/dump right after
collect_volumes/run_pre_hooks stage the data, stored in meta.json under
"checksums". Restore side recomputes the same fingerprint on the restored
data and diffs it against the manifest — a hard failure (KedgeError) on any
mismatch, no fake-green.

meta.json produced by backup.sh (the shell version) simply has no
"checksums" key — restore treats that as "nothing to verify", not an
error, so cross-version restore (bash backup -> python restore) still
works, just without the extra integrity check.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from kedge.discovery import discover_volumes, is_excluded_volume, resolve_volume_name, resolve_volume_path


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def checksum_directory(path: Path) -> str:
    """Sha256 over a sorted manifest of (relative path, per-file sha256) —
    same digest regardless of which tool staged the tree, as long as the
    bytes and layout match. Regular files only; skips symlinks/sockets/etc,
    which restic itself preserves but which don't carry byte content to
    hash."""
    path = Path(path)
    entries = []
    for file_path in sorted(p for p in path.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = file_path.relative_to(path).as_posix()
        entries.append(f"{rel}\0{_file_sha256(file_path)}")
    manifest = "\n".join(entries).encode()
    return hashlib.sha256(manifest).hexdigest()


def compute_backup_checksums(config: dict, vol_map_dir: Path, exclude_volumes: list[str]) -> dict[str, str]:
    """Call right after collect_volumes has staged tar-fallback volumes into
    vol_map_dir — hashes exactly what restic is about to back up, whether
    direct (live docker mountpoint) or tar (vol_map_dir/<name>.tar.gz)."""
    checksums: dict[str, str] = {}
    for vol_name in discover_volumes(config):
        if is_excluded_volume(vol_name, exclude_volumes):
            continue
        real_vol = resolve_volume_name(vol_name)
        if not real_vol:
            continue
        vol_path = resolve_volume_path(real_vol)
        tar_path = vol_map_dir / f"{vol_name}.tar.gz"
        if vol_path and Path(vol_path).is_dir():
            checksums[vol_name] = checksum_directory(Path(vol_path))
        elif tar_path.is_file():
            checksums[vol_name] = _file_sha256(tar_path)
    return checksums


def compute_dump_checksums(dumps_dir: Path) -> dict[str, str]:
    if not dumps_dir.is_dir():
        return {}
    return {p.name: _file_sha256(p) for p in sorted(dumps_dir.iterdir()) if p.is_file()}


def verify_restore_checksums(expected: dict, restored_volume_paths: dict[str, Path], dumps_dir: Path) -> list[str]:
    """Recompute checksums on restored data, diff against the manifest
    captured at backup time. Returns human-readable mismatch descriptions;
    empty list = clean. A volume/dump the manifest expected but that isn't
    present after restore counts as a mismatch too — silent data loss must
    not read as green."""
    mismatches: list[str] = []

    for vol_name, expected_hash in (expected.get("volumes") or {}).items():
        restored_path = restored_volume_paths.get(vol_name)
        if restored_path is None or not Path(restored_path).exists():
            mismatches.append(f"volume '{vol_name}': not restored")
            continue
        actual_hash = checksum_directory(Path(restored_path))
        if actual_hash != expected_hash:
            mismatches.append(f"volume '{vol_name}': checksum mismatch")

    for dump_name, expected_hash in (expected.get("dumps") or {}).items():
        dump_path = Path(dumps_dir) / dump_name
        if not dump_path.is_file():
            mismatches.append(f"dump '{dump_name}': not restored")
            continue
        actual_hash = _file_sha256(dump_path)
        if actual_hash != expected_hash:
            mismatches.append(f"dump '{dump_name}': checksum mismatch")

    return mismatches
