"""Unit tests for kedge.config.Config.from_env()."""

from pathlib import Path

from kedge.config import Config


def test_from_env_defaults(monkeypatch, tmp_path):
    for var in (
        "STACK_DIR", "RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE",
        "BACKUP_EXCLUDE_VOLUMES", "BACKUP_EXCLUDE_MOUNTS", "SYSTEM_PATHS", "SYSTEM_PATHS_EXCLUDE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)

    cfg = Config.from_env()

    assert cfg.stack_dir == tmp_path.resolve() or cfg.stack_dir == Path.cwd()
    assert cfg.restic_repository == ""
    assert cfg.exclude_volumes == []
    assert cfg.system_paths == []


def test_from_env_reads_space_separated_lists(monkeypatch, tmp_path):
    monkeypatch.setenv("STACK_DIR", str(tmp_path))
    monkeypatch.setenv("RESTIC_REPOSITORY", "/backup/x")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")
    monkeypatch.setenv("BACKUP_EXCLUDE_VOLUMES", "cache_data tmp_data")
    monkeypatch.setenv("SYSTEM_PATHS", "/etc /root")
    monkeypatch.setenv("SYSTEM_PATHS_EXCLUDE", "/etc/shadow")

    cfg = Config.from_env()

    assert cfg.stack_dir == Path(tmp_path)
    assert cfg.restic_repository == "/backup/x"
    assert cfg.exclude_volumes == ["cache_data", "tmp_data"]
    assert cfg.system_paths == ["/etc", "/root"]
    assert cfg.system_paths_exclude == ["/etc/shadow"]
