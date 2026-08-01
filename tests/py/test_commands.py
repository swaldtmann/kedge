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
    monkeypatch.setattr(commands, "run_pre_hooks", lambda cfg, stack_dir, cmd, dump_dir: calls.append("pre_hooks") or 0)
    monkeypatch.setattr(commands, "stop_stack", lambda *a, **kw: calls.append("stop") or False)
    monkeypatch.setattr(commands, "collect_volumes", lambda *a, **kw: calls.append("collect_volumes") or ["/data/x"])
    monkeypatch.setattr(commands, "collect_stack_files", lambda *a, **kw: calls.append("collect_stack_files") or [])
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
    monkeypatch.setattr(commands, "run_pre_hooks", lambda cfg, stack_dir, cmd, dump_dir: 0)
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


def _stub_happy_path(monkeypatch):
    monkeypatch.setattr(commands.restic, "repo_initialized", lambda cfg: True)
    monkeypatch.setattr(commands, "compose_config", lambda stack_dir, cmd: {"services": {}})
    monkeypatch.setattr(commands, "check_hot_safety", lambda cfg: (True, []))
    monkeypatch.setattr(commands, "run_pre_hooks", lambda cfg, stack_dir, cmd, dump_dir: 0)
    monkeypatch.setattr(commands, "stop_stack", lambda *a, **kw: False)
    monkeypatch.setattr(commands, "collect_volumes", lambda *a, **kw: [])
    monkeypatch.setattr(commands, "collect_stack_files", lambda *a, **kw: [])
    monkeypatch.setattr(commands, "write_metadata", lambda *a, **kw: None)
    monkeypatch.setattr(commands.restic, "backup", lambda *a, **kw: "1.2 GiB")
    monkeypatch.setattr(commands, "start_stack", lambda *a, **kw: None)
    monkeypatch.setattr(commands.restic, "print_latest_snapshot", lambda cfg: None)
    monkeypatch.setattr(commands.restic, "latest_snapshot_short_id", lambda cfg: "abc123")
    monkeypatch.setattr(commands, "hostname", lambda: "test-host")


def test_cmd_backup_passes_bind_mount_paths_to_restic(monkeypatch, tmp_path):
    """CW-W-258: collect_stack_files' direct bind-mount paths must reach
    restic.backup's path list — that's the actual fix (no more tar.gz
    staging for external mounts, so they only get backed up if they flow
    through here)."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    _stub_happy_path(monkeypatch)
    monkeypatch.setattr(
        commands, "collect_stack_files", lambda *a, **kw: ["/var/poki/mirror", "/var/poki/db"]
    )

    captured = {}
    monkeypatch.setattr(
        commands.restic, "backup",
        lambda cfg, paths, excludes, tags, host: captured.setdefault("paths", paths) or "1.2 GiB",
    )

    commands.cmd_backup(_cfg(tmp_path))

    assert "/var/poki/mirror" in captured["paths"]
    assert "/var/poki/db" in captured["paths"]


# KEDGE-W-007: SYSTEM_PATHS_EXCLUDE is passed to restic for the whole backup
# invocation, not scoped to SYSTEM_PATHS -- an exclude entry that also matches
# an explicit volume/staging backup path must not shadow it (prod-cloud,
# 2026-07-27/28: "/var/lib/docker/volumes" in SYSTEM_PATHS_EXCLUDE silently
# emptied every Docker volume backup).
def test_filter_shadowing_excludes_drops_overlap_with_backup_path():
    backup_paths = ["/staging/x", "/var/lib/docker/volumes/xwiki_nextcloud_base/_data"]
    excludes = ["/var/lib/docker/volumes", "/var/lib/docker/overlay2"]
    kept = commands._filter_shadowing_excludes(backup_paths, excludes)
    assert kept == ["/var/lib/docker/overlay2"]


def test_filter_shadowing_excludes_keeps_non_overlapping_and_exact_match():
    backup_paths = ["/var/lib/docker/volumes/x/_data"]
    assert commands._filter_shadowing_excludes(backup_paths, ["/var/log"]) == ["/var/log"]
    # exact-path exclude (no trailing content) must also be treated as a shadow
    assert commands._filter_shadowing_excludes(["/a/b"], ["/a/b"]) == []


def test_cmd_backup_drops_exclude_that_shadows_a_volume_path(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    _stub_happy_path(monkeypatch)
    monkeypatch.setattr(
        commands, "collect_volumes",
        lambda *a, **kw: ["/var/lib/docker/volumes/xwiki_nextcloud_base/_data"],
    )

    captured = {}
    monkeypatch.setattr(
        commands.restic, "backup",
        lambda cfg, paths, excludes, **kw: captured.update(paths=paths, excludes=excludes),
    )

    cfg = _cfg(
        tmp_path,
        system_paths_exclude=["/var/lib/docker/volumes", "/var/lib/docker/overlay2"],
    )
    commands.cmd_backup(cfg)

    assert "/var/lib/docker/volumes/xwiki_nextcloud_base/_data" in captured["paths"]
    assert captured["excludes"] == ["/var/lib/docker/overlay2"]


def test_cmd_backup_fires_pre_and_post_hook_with_context(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    _stub_happy_path(monkeypatch)

    hook_calls = []
    monkeypatch.setattr(commands, "run_hook", lambda cmd, name, ctx: hook_calls.append((name, ctx)))

    cfg = _cfg(tmp_path, backup_pre_hook="echo pre", backup_post_hook="echo post")
    commands.cmd_backup(cfg)

    names = [name for name, _ in hook_calls]
    assert names == ["pre-hook", "post-hook"]
    post_ctx = hook_calls[1][1]
    assert post_ctx.snapshot == "abc123"
    assert post_ctx.size == "1.2 GiB"
    assert post_ctx.hostname == "test-host"
    assert post_ctx.stack == cfg.stack_dir.name


def test_cmd_backup_fires_ok_healthcheck(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    _stub_happy_path(monkeypatch)

    hc_calls = []
    monkeypatch.setattr(commands, "ping_healthcheck", lambda url, status, ctx: hc_calls.append((url, status)))

    cfg = _cfg(tmp_path, backup_healthcheck_url="https://hc.example.com/x")
    commands.cmd_backup(cfg)

    assert hc_calls == [("https://hc.example.com/x", "ok")]


def test_cmd_backup_fires_fail_hook_and_healthcheck_on_error(monkeypatch, tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    _stub_happy_path(monkeypatch)

    def boom(*a, **kw):
        raise RuntimeError("restic exploded")

    monkeypatch.setattr(commands.restic, "backup", boom)

    hook_calls = []
    hc_calls = []
    # mimic real run_hook's/ping_healthcheck's own empty-cmd/no-url no-op guard,
    # since here we mock the function itself rather than its subprocess call
    monkeypatch.setattr(commands, "run_hook", lambda cmd, name, ctx: hook_calls.append((name, ctx)) if cmd else None)
    monkeypatch.setattr(commands, "ping_healthcheck", lambda url, status, ctx: hc_calls.append((url, status, ctx)) if url else None)

    cfg = _cfg(tmp_path, backup_fail_hook="echo fail", backup_healthcheck_url="https://hc.example.com/x")
    with pytest.raises(RuntimeError, match="restic exploded"):
        commands.cmd_backup(cfg)

    names = [name for name, _ in hook_calls]
    assert names == ["fail-hook"]
    assert hook_calls[0][1].error == "restic exploded"
    assert hc_calls == [("https://hc.example.com/x", "fail", hook_calls[0][1])]
