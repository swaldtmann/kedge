"""Unit tests for kedge.docker_stack — stop/start stack lifecycle."""

import subprocess

from kedge.docker_stack import container_env, container_for_service, is_stack_running, start_stack, stop_stack


def test_is_stack_running_true(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, cwd, capture_output, text, check: subprocess.CompletedProcess(cmd, 0, stdout="abc123\n"),
    )
    assert is_stack_running(tmp_path, ["docker", "compose"]) is True


def test_is_stack_running_false(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, cwd, capture_output, text, check: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert is_stack_running(tmp_path, ["docker", "compose"]) is False


def test_stop_stack_hot_backup_mode_skips(tmp_path):
    assert stop_stack(tmp_path, ["docker", "compose"], backup_stop_stack=False) is False


def test_stop_stack_not_running_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, cwd, capture_output, text, check: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert stop_stack(tmp_path, ["docker", "compose"], backup_stop_stack=True) is False


def test_stop_stack_running_stops_and_returns_true(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, cwd=None, capture_output=None, text=None, check=None):
        calls.append(cmd)
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert stop_stack(tmp_path, ["docker", "compose"], backup_stop_stack=True) is True
    assert ["docker", "compose", "stop"] in calls


def test_start_stack_noop_if_not_was_running(monkeypatch, tmp_path):
    def fake_run(*a, **kw):
        raise AssertionError("should not be called")

    monkeypatch.setattr(subprocess, "run", fake_run)
    start_stack(tmp_path, ["docker", "compose"], was_running=False)


def test_start_stack_restarts_if_was_running(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, cwd=None, check=None: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    start_stack(tmp_path, ["docker", "compose"], was_running=True)
    assert calls == [["docker", "compose", "start"]]


def test_container_for_service_returns_first_line(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, cwd, capture_output, text, check: subprocess.CompletedProcess(cmd, 0, stdout="c-db\nc-old\n"),
    )
    assert container_for_service(tmp_path, ["docker", "compose"], "db") == "c-db"


def test_container_for_service_not_running_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, cwd, capture_output, text, check: subprocess.CompletedProcess(cmd, 0, stdout=""),
    )
    assert container_for_service(tmp_path, ["docker", "compose"], "db") == ""


def test_container_env_parses_key_value_pairs(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, text, check: subprocess.CompletedProcess(
            cmd, 0, stdout="POSTGRES_USER=admin\nPATH=/usr/bin\nGARBAGE\n",
        ),
    )
    assert container_env("c-db") == {"POSTGRES_USER": "admin", "PATH": "/usr/bin"}
