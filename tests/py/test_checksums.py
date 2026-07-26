"""Unit tests for kedge.checksums — KEDGE-W-003 #3 (roadmap issue #1)."""

from __future__ import annotations

from kedge.checksums import (
    checksum_directory,
    compute_backup_checksums,
    compute_dump_checksums,
    verify_restore_checksums,
)


def _make_tree(base, files: dict[str, bytes]):
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def test_checksum_directory_is_deterministic(tmp_path):
    _make_tree(tmp_path, {"a.txt": b"hello", "sub/b.txt": b"world"})
    assert checksum_directory(tmp_path) == checksum_directory(tmp_path)


def test_checksum_directory_changes_when_content_changes(tmp_path):
    _make_tree(tmp_path, {"a.txt": b"hello"})
    before = checksum_directory(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"HELLO")
    after = checksum_directory(tmp_path)
    assert before != after


def test_checksum_directory_changes_when_file_added(tmp_path):
    _make_tree(tmp_path, {"a.txt": b"hello"})
    before = checksum_directory(tmp_path)
    (tmp_path / "b.txt").write_bytes(b"new file")
    after = checksum_directory(tmp_path)
    assert before != after


def test_checksum_directory_same_for_two_identical_trees(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _make_tree(left, {"a.txt": b"hello", "sub/b.txt": b"world"})
    _make_tree(right, {"a.txt": b"hello", "sub/b.txt": b"world"})
    assert checksum_directory(left) == checksum_directory(right)


def test_compute_backup_checksums_direct_mode(monkeypatch, tmp_path):
    vol_dir = tmp_path / "live-volume"
    _make_tree(vol_dir, {"data.db": b"payload"})

    monkeypatch.setattr("kedge.checksums.discover_volumes", lambda config: ["db_data"])
    monkeypatch.setattr("kedge.checksums.is_excluded_volume", lambda name, excl: False)
    monkeypatch.setattr("kedge.checksums.resolve_volume_name", lambda name: "stack_db_data")
    monkeypatch.setattr("kedge.checksums.resolve_volume_path", lambda vol: str(vol_dir))

    result = compute_backup_checksums({}, tmp_path / "volumes", [])
    assert result == {"db_data": checksum_directory(vol_dir)}


def test_compute_backup_checksums_tar_fallback_mode(monkeypatch, tmp_path):
    vol_map_dir = tmp_path / "volumes"
    vol_map_dir.mkdir()
    tar_path = vol_map_dir / "db_data.tar.gz"
    tar_path.write_bytes(b"fake tar bytes")

    monkeypatch.setattr("kedge.checksums.discover_volumes", lambda config: ["db_data"])
    monkeypatch.setattr("kedge.checksums.is_excluded_volume", lambda name, excl: False)
    monkeypatch.setattr("kedge.checksums.resolve_volume_name", lambda name: "stack_db_data")
    monkeypatch.setattr("kedge.checksums.resolve_volume_path", lambda vol: "")  # not a live mountpoint

    from kedge.checksums import _file_sha256

    result = compute_backup_checksums({}, vol_map_dir, [])
    assert result == {"db_data": _file_sha256(tar_path)}


def test_compute_backup_checksums_skips_excluded_volumes(monkeypatch, tmp_path):
    monkeypatch.setattr("kedge.checksums.discover_volumes", lambda config: ["db_data"])
    monkeypatch.setattr("kedge.checksums.is_excluded_volume", lambda name, excl: True)
    result = compute_backup_checksums({}, tmp_path / "volumes", ["db_data"])
    assert result == {}


def test_compute_dump_checksums(tmp_path):
    dumps_dir = tmp_path / "dumps"
    _make_tree(dumps_dir, {"db_postgres.sql.gz": b"dump bytes"})
    from kedge.checksums import _file_sha256

    result = compute_dump_checksums(dumps_dir)
    assert result == {"db_postgres.sql.gz": _file_sha256(dumps_dir / "db_postgres.sql.gz")}


def test_compute_dump_checksums_missing_dir_returns_empty(tmp_path):
    assert compute_dump_checksums(tmp_path / "nope") == {}


def test_verify_restore_checksums_clean_match(tmp_path):
    vol_dir = tmp_path / "restored-vol"
    _make_tree(vol_dir, {"data.db": b"payload"})
    dumps_dir = tmp_path / "dumps"
    _make_tree(dumps_dir, {"db_postgres.sql.gz": b"dump"})
    from kedge.checksums import _file_sha256

    expected = {
        "volumes": {"db_data": checksum_directory(vol_dir)},
        "dumps": {"db_postgres.sql.gz": _file_sha256(dumps_dir / "db_postgres.sql.gz")},
    }
    mismatches = verify_restore_checksums(expected, {"db_data": vol_dir}, dumps_dir)
    assert mismatches == []


def test_verify_restore_checksums_detects_corrupted_volume(tmp_path):
    """The DoD control case: intentionally corrupted restore data must hard-fail,
    never a fake-green."""
    vol_dir = tmp_path / "restored-vol"
    _make_tree(vol_dir, {"data.db": b"payload"})
    expected = {"volumes": {"db_data": checksum_directory(vol_dir)}, "dumps": {}}

    # Corrupt the restored data after the "backup-time" checksum was captured.
    (vol_dir / "data.db").write_bytes(b"CORRUPTED")

    mismatches = verify_restore_checksums(expected, {"db_data": vol_dir}, tmp_path / "dumps")
    assert len(mismatches) == 1
    assert "db_data" in mismatches[0]
    assert "checksum mismatch" in mismatches[0]


def test_verify_restore_checksums_detects_corrupted_dump(tmp_path):
    dumps_dir = tmp_path / "dumps"
    _make_tree(dumps_dir, {"db_mysql.sql.gz": b"original dump"})
    from kedge.checksums import _file_sha256

    expected = {"volumes": {}, "dumps": {"db_mysql.sql.gz": _file_sha256(dumps_dir / "db_mysql.sql.gz")}}

    (dumps_dir / "db_mysql.sql.gz").write_bytes(b"corrupted dump bytes")

    mismatches = verify_restore_checksums(expected, {}, dumps_dir)
    assert len(mismatches) == 1
    assert "db_mysql.sql.gz" in mismatches[0]


def test_verify_restore_checksums_detects_missing_volume(tmp_path):
    expected = {"volumes": {"db_data": "deadbeef"}, "dumps": {}}
    mismatches = verify_restore_checksums(expected, {}, tmp_path / "dumps")
    assert mismatches == ["volume 'db_data': not restored"]


def test_verify_restore_checksums_detects_missing_dump(tmp_path):
    expected = {"volumes": {}, "dumps": {"db_mysql.sql.gz": "deadbeef"}}
    mismatches = verify_restore_checksums(expected, {}, tmp_path / "dumps")
    assert mismatches == ["dump 'db_mysql.sql.gz': not restored"]


def test_verify_restore_checksums_empty_manifest_is_clean(tmp_path):
    assert verify_restore_checksums({}, {}, tmp_path / "dumps") == []
