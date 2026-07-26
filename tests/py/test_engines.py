"""Unit tests for kedge.engines — the DB engine registry (KEDGE-W-004).

Dump/import tests for postgres/mysql/valkey/mongo moved here from
test_hooks.py/test_restore.py along with the functions themselves (one owner
per engine now, not four hand-synced copies). The MariaDB regression tests
are the actual point of this ticket: restore-import and healthcheck used to
hardcode mysql/mysqladmin, breaking against modern mariadb:11+ images that
only ship mariadb/mariadb-admin.
"""

from __future__ import annotations

import gzip
import sqlite3
import subprocess
import tarfile

import pytest

from kedge import docker_stack, engines
from kedge.errors import KedgeError


# --- registry lookups ----------------------------------------------------------

@pytest.mark.parametrize("image,expected", [
    ("postgres:16", "postgres"),
    ("postgis/postgis:16-3.4", "postgres"),
    ("mariadb:11.5", "mysql"),
    ("mysql:8.0", "mysql"),
    ("valkey:7", "valkey"),
    ("redis:7-alpine", "valkey"),
    ("mongo:7", "mongo"),
    ("influxdb:2.7", "influxdb"),
    ("nginx:alpine", None),
])
def test_engine_for_image(image, expected):
    engine = engines.engine_for_image(image)
    assert (engine.name if engine else None) == expected


def test_engine_by_name_known_and_unknown():
    assert engines.engine_by_name("postgres").name == "postgres"
    assert engines.engine_by_name("nope") is None


def test_registry_has_exactly_one_descriptor_per_engine_name():
    names = [e.name for e in engines.ENGINES]
    assert len(names) == len(set(names))


def test_build_healthcheck_case_block_contains_every_healthcheck_engine():
    block = engines.build_healthcheck_case_block()
    for engine in engines.ENGINES:
        if engine.healthcheck_patterns:
            for pattern in engine.healthcheck_patterns:
                assert pattern in block
    assert block.count(";;") == sum(1 for e in engines.ENGINES if e.healthcheck_patterns)


# --- postgres ------------------------------------------------------------------

def test_dump_postgres_success(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {"POSTGRES_USER": "admin"})
    monkeypatch.setattr(docker_stack, "stream_gzip", lambda cmd, dest, env_vars=None: dest.write_bytes(b"x") or True)
    engines.dump_postgres("db", "c-db", "mystack-db-1", tmp_path)
    assert (tmp_path / "db_postgres.sql.gz").is_file()


def test_dump_postgres_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {})
    monkeypatch.setattr(docker_stack, "stream_gzip", lambda *a, **kw: False)
    with pytest.raises(KedgeError, match="PostgreSQL dump"):
        engines.dump_postgres("db", "c-db", "mystack-db-1", tmp_path)


def test_import_postgres_success(monkeypatch, tmp_path):
    dump_path = tmp_path / "db_postgres.sql.gz"
    with gzip.open(dump_path, "wb") as gz:
        gz.write(b"SQL DUMP")

    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "c-db")
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {"POSTGRES_USER": "admin"})
    monkeypatch.setattr(docker_stack, "wait_until_ready", lambda *a, **kw: True)

    captured = {}

    def fake_run(cmd, **kwargs):
        if "psql" in cmd:
            captured["cmd"] = cmd
            captured["stdin"] = kwargs.get("stdin")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    engines.import_postgres(["docker", "compose"], tmp_path, dump_path, "db")

    assert "-U" in captured["cmd"] and "admin" in captured["cmd"]
    # -d postgres (maintenance DB, always exists) — regression guard for the
    # silent dump-only data loss found in the live verify roundtrip (CW-W-003).
    assert captured["cmd"][captured["cmd"].index("-d") + 1] == "postgres"


def test_import_postgres_no_container_warns_and_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a) or subprocess.CompletedProcess([], 0))
    engines.import_postgres(["docker", "compose"], tmp_path, tmp_path / "x.sql.gz", "db")
    assert calls == []


# --- mysql / mariadb — the KEDGE-W-004 regression -------------------------------

def test_dump_mysql_no_password_hard_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {})
    with pytest.raises(KedgeError, match="refusing an unauthenticated dump"):
        engines.dump_mysql("db", "c-db", "mystack-db-1", tmp_path)


def test_import_mysql_falls_back_to_mariadb_binaries_when_mysql_absent(monkeypatch, tmp_path):
    """KEDGE-W-004 regression: a mariadb:11+ image has NO mysql/mysqladmin at
    all -- import must probe for and use mariadb-admin/mariadb, exactly like
    dump_mysql already did (hooks.py had the fallback, restore's import and
    verify's healthcheck never picked it up)."""
    dump_path = tmp_path / "db_mysql.sql.gz"
    with gzip.open(dump_path, "wb") as gz:
        gz.write(b"SQL DUMP")

    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "c-db")
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {"MARIADB_ROOT_PASSWORD": "hunter2"})
    monkeypatch.setattr(docker_stack, "wait_until_ready", lambda *a, **kw: True)
    # only mariadb-admin/mariadb exist on this (modern mariadb:11) container
    monkeypatch.setattr(
        docker_stack, "binary_exists",
        lambda container, binary: binary in ("mariadb-admin", "mariadb"),
    )

    captured = {}

    def fake_run(cmd, **kwargs):
        if "-i" in cmd:  # the actual import invocation carries stdin
            captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    engines.import_mysql(["docker", "compose"], tmp_path, dump_path, "db")

    assert "mariadb" in captured["cmd"]
    assert "mysql" not in captured["cmd"]  # would have failed on a real mariadb:11 image
    assert "MYSQL_PWD=hunter2" in captured["cmd"]


def test_import_mysql_uses_mysql_binaries_when_available(monkeypatch, tmp_path):
    """Older mysql:8 images still ship mysql/mysqladmin -- must not force the
    mariadb-* names onto them."""
    dump_path = tmp_path / "db_mysql.sql.gz"
    with gzip.open(dump_path, "wb") as gz:
        gz.write(b"SQL DUMP")

    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "c-db")
    monkeypatch.setattr(
        docker_stack, "container_env",
        lambda c: {"MYSQL_ROOT_PASSWORD": "correct", "MARIADB_ROOT_PASSWORD": "wrong"},
    )
    monkeypatch.setattr(docker_stack, "wait_until_ready", lambda *a, **kw: True)
    monkeypatch.setattr(docker_stack, "binary_exists", lambda container, binary: False)  # no mariadb-* binaries

    captured = {}

    def fake_run(cmd, **kwargs):
        if "-i" in cmd:
            captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    engines.import_mysql(["docker", "compose"], tmp_path, dump_path, "db")

    assert "mysql" in captured["cmd"]
    assert "MYSQL_PWD=correct" in captured["cmd"]  # MYSQL_ROOT_PASSWORD wins priority


def test_healthcheck_mysql_arm_falls_back_to_mariadb_admin():
    """Same regression, bash side: the verify.py healthcheck case arm must
    probe for mariadb-admin before assuming mysqladmin."""
    arm = engines.MYSQL.case_arm()
    assert "mariadb-admin" in arm
    assert "command -v mariadb-admin" in arm


# --- valkey ----------------------------------------------------------------------

def test_dump_valkey_never_raises_even_on_bgsave_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {"VALKEY_PASSWORD": "secret"})
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    )
    engines.dump_valkey("cache", "c-cache", "mystack-cache-1", tmp_path)  # must not raise
    assert calls  # something was actually attempted


def test_valkey_has_no_import_dump_import_via_volume_restore_instead():
    assert engines.VALKEY.import_ is None
    assert engines.VALKEY.dump_suffix is None


# --- mongo -------------------------------------------------------------------

def test_dump_mongo_success(monkeypatch, tmp_path):
    monkeypatch.setattr(
        docker_stack, "stream_raw",
        lambda cmd, dest: dest.write_bytes(b"BSON archive bytes") or True,
    )
    engines.dump_mongo("db", "c-db", "mystack-db-1", tmp_path)
    dest = tmp_path / "db_mongo.archive.gz"
    assert dest.read_bytes() == b"BSON archive bytes"  # not double-gzipped


def test_import_mongo_streams_raw_archive(monkeypatch, tmp_path):
    dump_path = tmp_path / "db_mongo.archive.gz"
    dump_path.write_bytes(b"BSON archive bytes")

    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "c-db")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["stdin_bytes"] = kwargs["stdin"].read()
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    engines.import_mongo(["docker", "compose"], tmp_path, dump_path, "db")

    assert captured["cmd"] == ["docker", "exec", "-i", "c-db", "mongorestore", "--archive", "--gzip"]
    assert captured["stdin_bytes"] == b"BSON archive bytes"


# --- SQLite (KEDGE-W-004 new engine) — real local sqlite3, no mocks ------------

def _wal_path(db_path):
    return db_path.parent / (db_path.name + "-wal")


def test_checkpoint_wal_truncates_and_makes_main_file_self_consistent(tmp_path):
    """Real WAL-mode SQLite (poki-like): write + commit while a second
    connection stays open (so SQLite's own auto-checkpoint never fires),
    checkpoint via a fresh connection like production code does, then prove
    the PLAIN .db file alone (no -wal/-shm) already has the data -- that is
    the entire point of WAL_CHECKPOINT(TRUNCATE) before a bind-mount tar."""
    db_path = tmp_path / "poki.db"
    keep_open = sqlite3.connect(str(db_path))
    keep_open.execute("PRAGMA journal_mode=WAL")
    keep_open.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    keep_open.execute("INSERT INTO t (v) VALUES ('hello')")
    keep_open.commit()

    assert _wal_path(db_path).is_file()
    assert _wal_path(db_path).stat().st_size > 0  # data is only in the WAL so far

    ok = engines.checkpoint_wal(db_path)
    assert ok is True
    assert _wal_path(db_path).stat().st_size == 0  # merged into the main file + truncated

    keep_open.close()

    # Copy ONLY the main .db file (no -wal/-shm) to a "restored" location and
    # read it back fresh -- proves the main file is standalone-consistent now.
    restored = tmp_path / "restored.db"
    restored.write_bytes(db_path.read_bytes())
    fresh = sqlite3.connect(str(restored))
    assert fresh.execute("SELECT v FROM t").fetchone() == ("hello",)
    fresh.close()


def test_checkpoint_wal_busy_is_non_fatal(tmp_path):
    """A concurrent writer holding the WAL lock must not raise -- the caller
    just backs up the -wal/-shm sidecar files alongside the main file
    instead of a fully truncated one."""
    db_path = tmp_path / "poki.db"
    writer = sqlite3.connect(str(db_path))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    writer.execute("BEGIN IMMEDIATE")  # holds a write lock open

    ok = engines.checkpoint_wal(db_path)
    assert ok is False  # busy, but no exception

    writer.commit()
    writer.close()


def test_checkpoint_wal_paths_skips_missing_file_with_warning(tmp_path, capsys):
    engines.checkpoint_wal_paths([str(tmp_path / "does-not-exist.db")])
    assert "not found" in capsys.readouterr().out


def test_checkpoint_wal_paths_warns_on_non_sqlite_file_without_raising(tmp_path, capsys):
    junk = tmp_path / "not-a-db.db"
    junk.write_text("this is not a sqlite file")
    engines.checkpoint_wal_paths([str(junk)])  # must not raise
    assert "checkpoint failed" in capsys.readouterr().out


def test_checkpoint_wal_paths_ok_path_logs_ok(tmp_path, capsys):
    db_path = tmp_path / "poki.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    engines.checkpoint_wal_paths([str(db_path)])
    assert "SQLite WAL checkpoint: poki.db" in capsys.readouterr().out


def test_sqlite_engine_has_no_image_patterns_never_auto_discovered():
    assert engines.SQLITE.image_patterns == ()
    assert engines.engine_for_image("sqlite:latest") is None  # not a thing; confirms no accidental match
    assert all(e.name != "sqlite" or e is engines.SQLITE for e in engines.ENGINES)


def test_sqlite_bind_mount_backup_restore_roundtrip_via_existing_tar_mechanics(tmp_path):
    """Ties the checkpoint into the SAME tar mechanics collect.py/restore.py
    already use for any external bind mount -- proves the checkpointed main
    .db file survives a real tar+untar cycle with data intact, using real
    sqlite3 throughout (no mocks)."""
    bind_mount_dir = tmp_path / "var_poki_db"
    bind_mount_dir.mkdir()
    db_path = bind_mount_dir / "poki.sqlite"

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, body TEXT)")
    conn.execute("INSERT INTO messages (body) VALUES ('backup me')")
    conn.commit()

    assert engines.checkpoint_wal(db_path) is True

    # backup.sh/collect.py's actual mechanism: tar czf the whole mount dir.
    tar_path = tmp_path / "external-mounts" / "var_poki_db.tar.gz"
    tar_path.parent.mkdir()
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(bind_mount_dir, arcname="var_poki_db")
    conn.close()

    # restore.sh/_restore_external_mounts's actual mechanism: tar xzf back out.
    restore_dir = tmp_path / "restored"
    restore_dir.mkdir()
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(restore_dir, filter="data")

    restored_db = restore_dir / "var_poki_db" / "poki.sqlite"
    assert restored_db.is_file()
    fresh = sqlite3.connect(str(restored_db))
    assert fresh.execute("SELECT body FROM messages").fetchone() == ("backup me",)
    fresh.close()


# --- InfluxDB (KEDGE-W-004 new engine) — mocked docker, real fleet is v2.6 -----
# (cowork/servers.yaml: prod-multi01 runs influxdb 2.6 -- v1 is detected only
# so it fails loudly at import time instead of running the wrong command.)

def test_influx_is_v1_detects_by_major_tag():
    assert engines._influx_is_v1("influxdb:1.8") is True
    assert engines._influx_is_v1("influxdb:2.7") is False
    assert engines._influx_is_v1("influxdb:latest") is False  # current upstream default is v2


def test_dump_influxdb_v2_uses_influx_backup_with_token(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {"DOCKER_INFLUXDB_INIT_ADMIN_TOKEN": "tok123"})

    captured_cmds = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    def fake_copy_from(container, src, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "manifest.json").write_text("{}")
        return True

    monkeypatch.setattr(docker_stack, "copy_from_container", fake_copy_from)

    engines.dump_influxdb("monitoring", "c-influx", "multi01-influx-1", tmp_path, image="influxdb:2.7")

    backup_cmd = next(c for c in captured_cmds if "backup" in c)
    assert "influx" in backup_cmd and "influxd" not in backup_cmd
    assert "--token" in backup_cmd and "tok123" in backup_cmd

    dest = tmp_path / "monitoring_influxdb.tar.gz"
    assert dest.is_file()
    with tarfile.open(dest, "r:gz") as tar:
        assert "manifest.json" in tar.getnames()


def test_dump_influxdb_v1_uses_influxd_backup_portable(monkeypatch, tmp_path):
    captured_cmds = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: captured_cmds.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""),
    )

    def fake_copy_from(container, src, dest):
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "meta.00").write_text("x")
        return True

    monkeypatch.setattr(docker_stack, "copy_from_container", fake_copy_from)

    engines.dump_influxdb("monitoring", "c-influx", "multi01-influx-1", tmp_path, image="influxdb:1.8")

    backup_cmd = next(c for c in captured_cmds if "backup" in c)
    assert backup_cmd[:4] == ["docker", "exec", "c-influx", "influxd"]
    assert "-portable" in backup_cmd


def test_dump_influxdb_backup_failure_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {})
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="backup failed: disk full"),
    )
    with pytest.raises(KedgeError, match="InfluxDB backup"):
        engines.dump_influxdb("monitoring", "c-influx", "multi01-influx-1", tmp_path, image="influxdb:2.7")


def test_import_influxdb_v2_roundtrip(monkeypatch, tmp_path):
    dump_path = tmp_path / "monitoring_influxdb.tar.gz"
    with tarfile.open(dump_path, "w:gz") as tar:
        member_src = tmp_path / "manifest.json"
        member_src.write_text("{}")
        tar.add(member_src, arcname="manifest.json")

    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "c-influx")
    monkeypatch.setattr(docker_stack, "container_env", lambda c: {"INFLUX_TOKEN": "tok456"})
    monkeypatch.setattr(docker_stack, "copy_to_container", lambda container, src, dest: True)

    captured_cmds = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="influxdb:2.7\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    engines.import_influxdb(["docker", "compose"], tmp_path, dump_path, "monitoring")

    restore_cmd = next(c for c in captured_cmds if "restore" in c)
    assert "--full" in restore_cmd
    assert "--token" in restore_cmd and "tok456" in restore_cmd


def test_import_influxdb_v1_refuses_with_clear_error(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "c-influx")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="influxdb:1.8\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(KedgeError, match="v1 restore.*not supported"):
        engines.import_influxdb(["docker", "compose"], tmp_path, tmp_path / "x.tar.gz", "monitoring")


def test_import_influxdb_no_container_warns_and_skips(monkeypatch, tmp_path):
    monkeypatch.setattr(docker_stack, "container_for_service", lambda *a: "")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a) or subprocess.CompletedProcess([], 0))
    engines.import_influxdb(["docker", "compose"], tmp_path, tmp_path / "x.tar.gz", "monitoring")
    assert calls == []
