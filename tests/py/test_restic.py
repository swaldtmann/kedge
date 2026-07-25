"""Unit tests for kedge.restic — subprocess-boundary mocks."""

import subprocess

import pytest

from kedge.config import Config
from kedge.errors import KedgeError
from kedge import restic


def _cfg(**overrides):
    defaults = dict(stack_dir=None, restic_repository="/backup/x", restic_password="secret")
    defaults.update(overrides)
    return Config(**defaults)


def test_env_includes_repository_and_password():
    env = restic._env(_cfg())
    assert env["RESTIC_REPOSITORY"] == "/backup/x"
    assert env["RESTIC_PASSWORD"] == "secret"
    assert "RESTIC_PASSWORD_FILE" not in env or env.get("RESTIC_PASSWORD_FILE") == ""


def test_env_uses_password_file_when_no_password():
    env = restic._env(_cfg(restic_password="", restic_password_file="/etc/kedge/pw"))
    assert env["RESTIC_PASSWORD_FILE"] == "/etc/kedge/pw"


def test_repo_initialized_true(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 0))
    assert restic.repo_initialized(_cfg()) is True


def test_repo_initialized_false(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    assert restic.repo_initialized(_cfg()) is False


def test_init_raises_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(KedgeError, match="restic init failed"):
        restic.init(_cfg())


def test_backup_builds_correct_command(monkeypatch):
    captured = {}

    def fake_run(cmd, env):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    restic.backup(_cfg(), ["/staging", "/data/vol1"], ["/proc"], ["kedge", "stack:x"], "myhost")

    assert captured["cmd"] == [
        "restic", "backup", "/staging", "/data/vol1",
        "--exclude", "/proc",
        "--tag", "kedge", "--tag", "stack:x",
        "--host", "myhost",
    ]


def test_backup_raises_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(KedgeError, match="restic backup failed"):
        restic.backup(_cfg(), ["/staging"], [], ["kedge"], "myhost")


def test_latest_snapshot_short_id(monkeypatch):
    def fake_run(cmd, env, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 0, stdout='[{"short_id": "abc123"}]')

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert restic.latest_snapshot_short_id(_cfg()) == "abc123"


def test_latest_snapshot_short_id_empty_list(monkeypatch):
    def fake_run(cmd, env, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 0, stdout="[]")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert restic.latest_snapshot_short_id(_cfg()) == "unknown"


def test_latest_snapshot_short_id_failure(monkeypatch):
    def fake_run(cmd, env, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 1, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert restic.latest_snapshot_short_id(_cfg()) == "unknown"


def test_check_raises_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(KedgeError, match="restic check failed"):
        restic.check(_cfg())


def test_prune_builds_group_by_tags(monkeypatch):
    captured = {}

    def fake_run(cmd, env):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    restic.prune(_cfg(), 7, 4, 3)

    assert "--group-by" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--group-by") + 1] == "tags"
    assert "--prune" in captured["cmd"]


def test_prune_raises_on_failure(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1))
    with pytest.raises(KedgeError, match="restic prune failed"):
        restic.prune(_cfg(), 7, 4, 3)


def test_stats_size_formatted(monkeypatch):
    def fake_run(cmd, env, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 0, stdout='{"total_size_formatted": "1.2 GiB"}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert restic.stats_size_formatted(_cfg()) == "1.2 GiB"


def test_stats_size_formatted_unparseable(monkeypatch):
    def fake_run(cmd, env, capture_output, text, check):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert restic.stats_size_formatted(_cfg()) == "unknown"


def test_stats_size_formatted_falls_back_to_plain_text_parse(monkeypatch):
    """Modern restic's --json has no total_size_formatted (only a raw byte
    count) — confirmed live against restic 0.19.0. This fallback is
    load-bearing, not a hypothetical edge case."""
    def fake_run(cmd, env, capture_output, text, check):
        if "--json" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"total_size":461,"total_file_count":13}')
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout="Stats in restore-size mode:\n     Snapshots processed:  1\n        Total File Count:  13\n              Total Size:  461 B\n",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert restic.stats_size_formatted(_cfg()) == "461 B"
