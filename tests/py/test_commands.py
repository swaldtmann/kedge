"""Unit tests for kedge.commands — orchestration, mocked at module boundaries."""

import pytest

from kedge import commands
from kedge.config import Config
from kedge.errors import KedgeError
from kedge.prereqs import Prereqs


def _cfg(tmp_path, **overrides):
    defaults = dict(
        stack_dir=tmp_path,
        restic_repository="/backup/x",
        restic_password="secret",
        staging_base=tmp_path / "staging-base",
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture(autouse=True)
def _stub_check_prereqs(monkeypatch):
    monkeypatch.setattr(commands, "check_prereqs", lambda cfg: Prereqs(compose_cmd=["docker", "compose"]))


def test_cmd_init_calls_restic_init(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(commands.restic, "init", lambda cfg: calls.append("init"))
    commands.cmd_init(_cfg(tmp_path))
    assert calls == ["init"]


def test_cmd_list_calls_restic_list(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(commands.restic, "list_snapshots", lambda cfg: calls.append("list"))
    commands.cmd_list(_cfg(tmp_path))
    assert calls == ["list"]


def test_cmd_check_calls_restic_check(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(commands.restic, "check", lambda cfg: calls.append("check"))
    commands.cmd_check(_cfg(tmp_path))
    assert calls == ["check"]


def test_cmd_prune_passes_retention_settings(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        commands.restic, "prune",
        lambda cfg, d, w, m: captured.update(daily=d, weekly=w, monthly=m),
    )
    commands.cmd_prune(_cfg(tmp_path, keep_daily=1, keep_weekly=2, keep_monthly=3))
    assert captured == {"daily": 1, "weekly": 2, "monthly": 3}


def test_cmd_backup_raises_if_repo_not_initialized(monkeypatch, tmp_path):
    monkeypatch.setattr(commands.restic, "repo_initialized", lambda cfg: False)
    with pytest.raises(KedgeError, match="Run: kedge init"):
        commands.cmd_backup(_cfg(tmp_path))


def test_cmd_backup_happy_path(monkeypatch, tmp_path):
    calls = []
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    monkeypatch.setattr(commands.restic, "repo_initialized", lambda cfg: True)
    monkeypatch.setattr(commands, "compose_config", lambda stack_dir, cmd: {"services": {}})
    monkeypatch.setattr(commands, "check_hot_safety", lambda cfg: (True, []))
    monkeypatch.setattr(commands, "run_pre_hooks", lambda cfg, dump_dir: calls.append("pre_hooks") or 0)
    monkeypatch.setattr(commands, "stop_stack", lambda *a, **kw: calls.append("stop") or False)
    monkeypatch.setattr(commands, "collect_volumes", lambda *a, **kw: calls.append("collect_volumes") or ["/data/x"])
    monkeypatch.setattr(commands, "collect_stack_files", lambda *a, **kw: calls.append("collect_stack_files"))
    monkeypatch.setattr(commands, "write_metadata", lambda *a, **kw: calls.append("write_metadata"))
    monkeypatch.setattr(commands.restic, "backup", lambda *a, **kw: calls.append("restic_backup"))
    monkeypatch.setattr(commands, "start_stack", lambda *a, **kw: calls.append("start"))
    monkeypatch.setattr(commands.restic, "print_latest_snapshot", lambda cfg: calls.append("print_latest"))

    cfg = _cfg(tmp_path)
    commands.cmd_backup(cfg)

    assert calls == [
        "pre_hooks", "stop", "collect_volumes", "collect_stack_files",
        "write_metadata", "restic_backup", "start", "print_latest",
    ]
    # staging dir was created then cleaned up
    assert not (cfg.staging_base / cfg.stack_dir.name).exists()


def test_cmd_backup_restarts_stack_and_cleans_staging_on_failure(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    monkeypatch.setattr(commands.restic, "repo_initialized", lambda cfg: True)
    monkeypatch.setattr(commands, "compose_config", lambda stack_dir, cmd: {"services": {}})
    monkeypatch.setattr(commands, "check_hot_safety", lambda cfg: (True, []))
    monkeypatch.setattr(commands, "run_pre_hooks", lambda cfg, dump_dir: 0)
    monkeypatch.setattr(commands, "stop_stack", lambda *a, **kw: True)  # stack WAS running

    def boom(*a, **kw):
        raise RuntimeError("collect failed")

    monkeypatch.setattr(commands, "collect_volumes", boom)

    restart_calls = []
    monkeypatch.setattr(commands, "start_stack", lambda *a, **kw: restart_calls.append(a))

    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError, match="collect failed"):
        commands.cmd_backup(cfg)

    assert restart_calls, "start_stack must be called to restore a running stack on failure"
    assert not (cfg.staging_base / cfg.stack_dir.name).exists()
