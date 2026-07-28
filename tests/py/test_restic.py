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


class _FakePopen:
    """Minimal stand-in for subprocess.Popen — backup() streams proc.stdout
    line-by-line and calls proc.wait(), it never touches subprocess.run()."""

    def __init__(self, cmd, lines, returncode, **kwargs):
        self.args = cmd
        self.stdout = iter(lines)
        self.returncode = None
        self._exit_code = returncode

    def wait(self):
        self.returncode = self._exit_code
        return self.returncode


def test_backup_builds_correct_command(monkeypatch):
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd, ["Added to the repository: 1.2 GiB (900 MiB stored)\n"], 0, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    restic.backup(_cfg(), ["/staging", "/data/vol1"], ["/proc"], ["kedge", "stack:x"], "myhost")

    assert captured["cmd"] == [
        "restic", "backup", "/staging", "/data/vol1",
        "--exclude", "/proc",
        "--tag", "kedge", "--tag", "stack:x",
        "--host", "myhost",
    ]


def test_backup_raises_on_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kwargs: _FakePopen(cmd, [], 1, **kwargs),
    )
    with pytest.raises(KedgeError, match="restic backup failed"):
        restic.backup(_cfg(), ["/staging"], [], ["kedge"], "myhost")


def test_backup_returns_added_size(monkeypatch):
    lines = [
        "using parent snapshot abc123\n",
        "\n",
        "Files:           2 new,     0 changed,     0 unmodified\n",
        "Added to the repository: 3.976 KiB (2.978 KiB stored)\n",
        "\n",
        "processed 2 files, 500.035 KiB in 0:00\n",
        "snapshot 0e9dee17 saved\n",
    ]
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kwargs: _FakePopen(cmd, lines, 0, **kwargs))
    assert restic.backup(_cfg(), ["/staging"], [], ["kedge"], "myhost") == "3.976 KiB"


def test_backup_returns_unknown_when_summary_line_missing(monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda cmd, **kwargs: _FakePopen(cmd, ["some unexpected output\n"], 0, **kwargs),
    )
    assert restic.backup(_cfg(), ["/staging"], [], ["kedge"], "myhost") == "unknown"


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


