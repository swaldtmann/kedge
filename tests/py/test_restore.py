"""Unit tests for kedge.restore — port of restore.sh:106-433 (cmd_restore)."""

from __future__ import annotations

import json
import subprocess

import pytest

from kedge import docker_stack as docker_stack_module
from kedge.errors import KedgeError
from kedge import restore


def _write(path, content=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode()
    path.write_bytes(content)


# --- _find_backup_root ------------------------------------------------------

def test_find_backup_root_stable_staging_layout(tmp_path):
    root = tmp_path / "restic-restore-out" / "var" / "lib" / "kedge" / "staging" / "mystack"
    _write(root / "meta.json", "{}")
    assert restore._find_backup_root(tmp_path) == root


def test_find_backup_root_legacy_mktemp_layout(tmp_path):
    root = tmp_path / "restic-restore-out" / "tmp" / "kedge-staging.AbCdEf"
    _write(root / "meta.json", "{}")
    assert restore._find_backup_root(tmp_path) == root


def test_find_backup_root_raises_when_absent(tmp_path):
    (tmp_path / "unrelated").mkdir()
    with pytest.raises(KedgeError, match="No meta.json found"):
        restore._find_backup_root(tmp_path)


# --- _restore_stack_files ----------------------------------------------------

def test_restore_stack_files_copies_compose_and_env(monkeypatch, tmp_path):
    backup_root = tmp_path / "backup_root"
    restore_target = tmp_path / "target"
    _write(backup_root / "docker-compose.yml", "services: {}\n")
    _write(backup_root / ".env", "FOO=bar\n")

    rsync_calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: rsync_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    restore._restore_stack_files(backup_root, restore_target)

    assert (restore_target / "docker-compose.yml").read_text() == "services: {}\n"
    assert (restore_target / ".env").read_text() == "FOO=bar\n"
    assert rsync_calls == []  # no stack-dir subfolder present in this fixture


def test_restore_stack_files_rsyncs_stack_dir(monkeypatch, tmp_path):
    backup_root = tmp_path / "backup_root"
    restore_target = tmp_path / "target"
    (backup_root / "stack-dir").mkdir(parents=True)

    rsync_calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: rsync_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))

    restore._restore_stack_files(backup_root, restore_target)

    assert len(rsync_calls) == 1
    assert rsync_calls[0][:2] == ["rsync", "-a"]
    assert rsync_calls[0][2].startswith(str(backup_root / "stack-dir"))
    assert rsync_calls[0][3].startswith(str(restore_target))


# --- _restore_external_mounts ------------------------------------------------

def test_restore_external_mounts_legacy_tar_extracts_each_archive(monkeypatch, tmp_path):
    """Pre-CW-W-258 snapshots stored external mounts as tar.gz archives —
    these must stay restorable even after the fix switches new backups to
    direct-path (meta.json has no bind_mount_paths for such a snapshot)."""
    import tempfile
    import shutil

    staging_dir = tmp_path / "staging"
    backup_root = tmp_path / "backup_root"
    ext_dir = backup_root / "external-mounts"
    _write(ext_dir / "tmp_kedge-pytest-extmount.tar.gz", "fake tar")

    mount_target = tempfile.mkdtemp(prefix="kedge-pytest-extmount")
    try:
        tar_calls = []
        monkeypatch.setattr(subprocess, "run", lambda cmd, check: tar_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))

        restore._restore_external_mounts(staging_dir, backup_root, meta={})

        assert len(tar_calls) == 1
        assert tar_calls[0][0] == "tar"
        assert tar_calls[0][1] == "xzf"
        # Pre-existing bug fixed alongside CW-W-258: the archive was built
        # with the mount's own dirname as -C (tar czf ... -C mount.parent
        # mount.name), so it must be extracted into the PARENT, not into
        # mount_path itself -- else the restore double-nests the directory
        # (mount_path/mount_path.name/... instead of mount_path/...).
        assert tar_calls[0][-1] == "/tmp"
    finally:
        shutil.rmtree(mount_target, ignore_errors=True)


def test_restore_external_mounts_noop_when_nothing_to_restore(tmp_path):
    restore._restore_external_mounts(tmp_path / "staging", tmp_path / "backup_root", meta={})  # must not raise


def test_restore_external_mounts_direct_path_dir(monkeypatch, tmp_path):
    """CW-W-258: new-format snapshots list bind_mount_paths in meta.json —
    restic already restored the directory directly under staging_dir at its
    original absolute path (same layout as a direct-path volume); it must be
    rsynced back in place, no tar.gz involved."""
    staging_dir = tmp_path / "staging"
    backup_root = tmp_path / "backup_root"
    orig_path = "/var/poki/mirror"
    restored = staging_dir / "var" / "poki" / "mirror"
    _write(restored / "msg1.eml", "hi\n")

    rsync_calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: rsync_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(restore.Path, "mkdir", lambda self, parents=False, exist_ok=False: None)

    restore._restore_external_mounts(staging_dir, backup_root, meta={"bind_mount_paths": [orig_path]})

    assert len(rsync_calls) == 1
    assert rsync_calls[0][:3] == ["rsync", "-a", "--delete"]
    assert rsync_calls[0][3].startswith(str(restored))


def test_restore_external_mounts_direct_path_file(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    backup_root = tmp_path / "backup_root"
    orig_path = "/opt/poki/config/auto-rules.yml"
    restored = staging_dir / "opt" / "poki" / "config" / "auto-rules.yml"
    _write(restored, "rules: []\n")

    copy_calls = []
    monkeypatch.setattr(restore.shutil, "copy2", lambda src, dst: copy_calls.append((src, dst)))
    monkeypatch.setattr(restore.Path, "mkdir", lambda self, parents=False, exist_ok=False: None)

    restore._restore_external_mounts(staging_dir, backup_root, meta={"bind_mount_paths": [orig_path]})

    assert len(copy_calls) == 1
    assert copy_calls[0][0] == restored


def test_restore_external_mounts_direct_path_missing_warns(tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    backup_root = tmp_path / "backup_root"

    # must not raise — just warns and skips
    restore._restore_external_mounts(staging_dir, backup_root, meta={"bind_mount_paths": ["/var/poki/mirror"]})


# --- _project_name -------------------------------------------------------------

def test_project_name_strips_non_alnum():
    from pathlib import Path

    assert restore._project_name(Path("/opt/My-Stack_01")) == "mystack01"


# --- _restore_volumes ----------------------------------------------------------

def _volume_subprocess_dispatch(handlers):
    def fake_run(cmd, **kwargs):
        for prefix, handler in handlers.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                return handler(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return fake_run


def test_restore_volumes_direct_mode(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    orig_vol_path = "/var/lib/docker/volumes/stack_db_data/_data"
    restored_vol_dir = staging_dir / "restic-out" / orig_vol_path.lstrip("/")
    restored_vol_dir.mkdir(parents=True)
    (restored_vol_dir / "ibdata1").write_bytes(b"data")

    backup_root = tmp_path / "backup_root"
    backup_root.mkdir()

    new_mountpoint = tmp_path / "docker-mountpoint"
    new_mountpoint.mkdir()

    rsync_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "volume", "create"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:4] == ["docker", "volume", "inspect", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{new_mountpoint}\n")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        if cmd[0] == "rsync":
            rsync_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    meta = {
        "volume_mapping": {"db_data": "stack_db_data"},
        "volume_paths": {"db_data": orig_vol_path},
    }
    restored = restore._restore_volumes(staging_dir, backup_root, meta, tmp_path / "target", False, False)

    assert restored == {"db_data": new_mountpoint}
    assert len(rsync_calls) == 1
    assert rsync_calls[0][:3] == ["rsync", "-a", "--delete"]


def test_restore_volumes_tar_fallback_mode(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    backup_root = tmp_path / "backup_root"
    _write(backup_root / "volumes" / "db_data.tar.gz", "fake tar")

    new_mountpoint = tmp_path / "docker-mountpoint"
    new_mountpoint.mkdir()

    docker_run_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "volume", "create"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:4] == ["docker", "volume", "inspect", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{new_mountpoint}\n")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        if cmd[:2] == ["docker", "run"]:
            docker_run_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    meta = {
        "volume_mapping": {"db_data": "stack_db_data"},
        "volume_paths": {"db_data": "/no/such/path"},  # not found in staging -> tar fallback
    }
    restored = restore._restore_volumes(staging_dir, backup_root, meta, tmp_path / "target", False, False)

    assert restored == {"db_data": new_mountpoint}
    assert len(docker_run_calls) == 1
    assert "tar xzf /backup/db_data.tar.gz" in docker_run_calls[0][-1]


def test_restore_volumes_no_data_found_skips(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    backup_root = tmp_path / "backup_root"
    backup_root.mkdir()

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "volume", "create"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:4] == ["docker", "volume", "inspect", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tmp_path}\n")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    meta = {"volume_mapping": {"db_data": "stack_db_data"}, "volume_paths": {}}
    restored = restore._restore_volumes(staging_dir, backup_root, meta, tmp_path / "target", False, False)
    assert restored == {}


def test_restore_volumes_live_guard_blocks_without_force_live(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    backup_root = tmp_path / "backup_root"
    backup_root.mkdir()

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "volume", "inspect"] and "--format" not in cmd:
            return subprocess.CompletedProcess(cmd, 0)  # volume exists
        if cmd[:3] == ["docker", "ps", "-q"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="running-container-id\n")  # mounted live
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    meta = {"volume_mapping": {"db_data": "stack_db_data"}, "volume_paths": {}}
    with pytest.raises(KedgeError, match="Refusing to overwrite live data"):
        restore._restore_volumes(staging_dir, backup_root, meta, tmp_path / "target", False, False)


def test_restore_volumes_live_guard_force_live_overrides(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    backup_root = tmp_path / "backup_root"
    backup_root.mkdir()
    new_mountpoint = tmp_path / "mnt"
    new_mountpoint.mkdir()

    def fake_run(cmd, **kwargs):
        if cmd[:4] == ["docker", "volume", "inspect", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{new_mountpoint}\n")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0)  # exists
        if cmd[:3] == ["docker", "ps", "-q"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="running-container-id\n")
        if cmd[:3] == ["docker", "volume", "create"]:
            return subprocess.CompletedProcess(cmd, 0)
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    meta = {"volume_mapping": {"db_data": "stack_db_data"}, "volume_paths": {}}
    # no data found (volume_paths empty, no tar) -> skipped, but must not raise
    restored = restore._restore_volumes(staging_dir, backup_root, meta, tmp_path / "target", False, True)
    assert restored == {}


def test_restore_volumes_verify_only_uses_restoretest_name(monkeypatch, tmp_path):
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    backup_root = tmp_path / "backup_root"
    backup_root.mkdir()

    created_names = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "volume", "create"]:
            created_names.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:4] == ["docker", "volume", "inspect", "--format"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{tmp_path}\n")
        if cmd[:3] == ["docker", "volume", "inspect"]:
            return subprocess.CompletedProcess(cmd, 0)
        if cmd[:3] == ["docker", "ps", "-q"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="running-container-id\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    meta = {"volume_mapping": {"db_data": "stack_db_data"}, "volume_paths": {}}
    restore._restore_volumes(staging_dir, backup_root, meta, tmp_path / "target", True, False)

    assert created_names == ["stack_db_data_restoretest"]


# --- dump import ---------------------------------------------------------------
#
# The per-engine import implementations (_import_postgres et al.) moved to
# kedge.engines (KEDGE-W-004) -- their tests moved with them, see
# test_engines.py. Only the generic suffix-dispatch loop stays here.

def test_import_dumps_dispatches_by_suffix(monkeypatch, tmp_path, capsys):
    dumps_dir = tmp_path / "dumps"
    _write(dumps_dir / "db_postgres.sql.gz", "x")

    monkeypatch.setattr(restore, "compose_config", lambda target, cmd: {"services": {"db": {"image": "postgres:16"}}})
    monkeypatch.setattr(restore, "discover_services", lambda config: [("db", "postgres:16")])
    monkeypatch.setattr(restore, "detect_db_type", lambda image: "postgres")
    monkeypatch.setattr(docker_stack_module, "container_for_service", lambda *a: "")  # -> warn+skip, proves dispatch happened

    up_calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: up_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, stdout=""))
    monkeypatch.setattr(restore, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))

    restore._import_dumps(["docker", "compose"], tmp_path, dumps_dir)

    assert any(cmd[:3] == ["docker", "compose", "up"] for cmd in up_calls)
    # "db_postgres.sql.gz" -> suffix stripped correctly to svc "db", not the postgres importer's
    # generic name — proves _postgres.sql.gz dispatched to _import_postgres, not another importer.
    assert "Importing dump: db_postgres.sql.gz -> db" in capsys.readouterr().out


def test_import_dumps_noop_when_no_dumps(tmp_path):
    restore._import_dumps(["docker", "compose"], tmp_path, tmp_path / "no-dumps")  # must not raise


def test_import_dumps_noop_when_no_db_services(monkeypatch, tmp_path):
    dumps_dir = tmp_path / "dumps"
    _write(dumps_dir / "db_postgres.sql.gz", "x")

    monkeypatch.setattr(restore, "compose_config", lambda target, cmd: {"services": {"web": {"image": "nginx"}}})
    monkeypatch.setattr(restore, "discover_services", lambda config: [("web", "nginx")])
    monkeypatch.setattr(restore, "detect_db_type", lambda image: "")

    def fail(*a, **kw):
        raise AssertionError("compose up must not be called with no db services")

    monkeypatch.setattr(subprocess, "run", fail)
    restore._import_dumps(["docker", "compose"], tmp_path, dumps_dir)


# --- _check_restore_prereqs -----------------------------------------------------

def test_check_restore_prereqs_missing_tools(monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: None)
    from kedge.config import Config

    with pytest.raises(KedgeError, match="Missing required tools"):
        restore._check_restore_prereqs(Config(stack_dir=None))


def test_check_restore_prereqs_missing_restic_repository(monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(restore, "detect_compose_cmd", lambda: ["docker", "compose"])
    from kedge.config import Config

    with pytest.raises(KedgeError, match="RESTIC_REPOSITORY not set"):
        restore._check_restore_prereqs(Config(stack_dir=None))


def test_check_restore_prereqs_missing_password(monkeypatch):
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(restore, "detect_compose_cmd", lambda: ["docker", "compose"])
    from kedge.config import Config

    with pytest.raises(KedgeError, match="RESTIC_PASSWORD"):
        restore._check_restore_prereqs(Config(stack_dir=None, restic_repository="/backup/x"))


def test_check_restore_prereqs_ok_does_not_require_compose_file(monkeypatch, tmp_path):
    """Unlike backup's check_prereqs, restore must NOT require a compose file
    to already exist in the (empty, pre-restore) target."""
    import shutil as shutil_mod

    monkeypatch.setattr(shutil_mod, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(restore, "detect_compose_cmd", lambda: ["docker", "compose"])
    from kedge.config import Config

    cfg = Config(stack_dir=tmp_path, restic_repository="/backup/x", restic_password="secret")
    assert restore._check_restore_prereqs(cfg) == ["docker", "compose"]


# --- cmd_restore (full orchestration) -------------------------------------------

def _fake_meta(stack_dir="/orig/stack", checksums=None):
    return {
        "stack_dir": stack_dir,
        "volume_mapping": {},
        "volume_paths": {},
        "checksums": checksums or {},
    }


def _stub_restore_pipeline(monkeypatch, meta, staging_dir):
    """Common stubbing for cmd_restore integration tests: restic.restore
    stages a real backup_root+meta.json into staging_dir, everything else
    (volume/compose/dump machinery) is mocked."""
    backup_root = staging_dir / "staging" / "mystack"

    def fake_restic_restore(cfg, snapshot_id, target):
        _write(backup_root / "meta.json", json.dumps(meta))

    monkeypatch.setattr(restore.restic, "restore", fake_restic_restore)
    monkeypatch.setattr(restore, "_check_restore_prereqs", lambda cfg: ["docker", "compose"])
    monkeypatch.setattr(restore, "_restore_stack_files", lambda *a: None)
    monkeypatch.setattr(restore, "_restore_external_mounts", lambda *a: None)
    monkeypatch.setattr(restore, "_restore_volumes", lambda *a: {})
    return backup_root


def test_cmd_restore_verify_only_never_starts_stack(monkeypatch, tmp_path):
    import tempfile

    staging_dir = tmp_path / "restic-restore"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix=None: str(staging_dir))
    staging_dir.mkdir()

    _stub_restore_pipeline(monkeypatch, _fake_meta(), staging_dir)

    def fail(*a, **kw):
        raise AssertionError("must not start the stack in --verify mode")

    monkeypatch.setattr(restore, "_import_dumps", fail)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no subprocess expected")))

    from kedge.config import Config

    cfg = Config(stack_dir=None, restic_repository="/backup/x", restic_password="secret")
    restore.cmd_restore(cfg, tmp_path / "target", "latest", verify_only=True, force_live=False)


def test_cmd_restore_full_starts_stack_and_imports_dumps(monkeypatch, tmp_path):
    import tempfile

    staging_dir = tmp_path / "restic-restore"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix=None: str(staging_dir))
    staging_dir.mkdir()

    _stub_restore_pipeline(monkeypatch, _fake_meta(), staging_dir)

    calls = []
    monkeypatch.setattr(restore, "_import_dumps", lambda *a: calls.append("import_dumps"))
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    from kedge.config import Config

    cfg = Config(stack_dir=None, restic_repository="/backup/x", restic_password="secret")
    restore.cmd_restore(cfg, tmp_path / "target", "latest", verify_only=False, force_live=False)

    assert "import_dumps" in calls
    assert any(isinstance(c, list) and c[:3] == ["docker", "compose", "up"] for c in calls if isinstance(c, list))


def test_cmd_restore_no_checksum_manifest_skips_verification(monkeypatch, tmp_path, capsys):
    """Cross-version compat: a bash-backup.sh snapshot has no "checksums" key
    at all — restore must treat that as nothing-to-verify, not an error."""
    import tempfile

    staging_dir = tmp_path / "restic-restore"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix=None: str(staging_dir))
    staging_dir.mkdir()

    meta = {"stack_dir": "/orig/stack", "volume_mapping": {}, "volume_paths": {}}  # no "checksums" key
    _stub_restore_pipeline(monkeypatch, meta, staging_dir)
    monkeypatch.setattr(restore, "_import_dumps", lambda *a: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0))

    from kedge.config import Config

    cfg = Config(stack_dir=None, restic_repository="/backup/x", restic_password="secret")
    restore.cmd_restore(cfg, tmp_path / "target", "latest", verify_only=True, force_live=False)

    assert "skipping integrity verification" in capsys.readouterr().out


def test_cmd_restore_checksum_mismatch_raises(monkeypatch, tmp_path):
    import tempfile

    staging_dir = tmp_path / "restic-restore"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix=None: str(staging_dir))
    staging_dir.mkdir()

    meta = _fake_meta(checksums={"volumes": {"db_data": "deadbeef"}, "dumps": {}})
    _stub_restore_pipeline(monkeypatch, meta, staging_dir)
    monkeypatch.setattr(restore, "_restore_volumes", lambda *a: {})  # db_data never restored -> mismatch

    from kedge.config import Config

    cfg = Config(stack_dir=None, restic_repository="/backup/x", restic_password="secret")
    with pytest.raises(KedgeError, match="checksum verification FAILED"):
        restore.cmd_restore(cfg, tmp_path / "target", "latest", verify_only=True, force_live=False)


def test_cmd_restore_cleans_up_staging_dir_on_failure(monkeypatch, tmp_path):
    import tempfile

    staging_dir = tmp_path / "restic-restore"
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix=None: str(staging_dir))
    staging_dir.mkdir()

    monkeypatch.setattr(restore, "_check_restore_prereqs", lambda cfg: ["docker", "compose"])

    def boom(cfg, snapshot_id, target):
        raise KedgeError("restic restore failed")

    monkeypatch.setattr(restore.restic, "restore", boom)

    from kedge.config import Config

    cfg = Config(stack_dir=None, restic_repository="/backup/x", restic_password="secret")
    with pytest.raises(KedgeError, match="restic restore failed"):
        restore.cmd_restore(cfg, tmp_path / "target", "latest")

    assert not staging_dir.exists()
