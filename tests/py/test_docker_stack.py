"""Unit tests for kedge.docker_stack — stop/start stack lifecycle, plus the
generic docker exec/cp helpers used by kedge.engines (KEDGE-W-004)."""

import subprocess

from kedge.docker_stack import (
    cmd_args,
    container_env,
    container_for_service,
    copy_from_container,
    copy_to_container,
    is_stack_running,
    start_stack,
    stop_stack,
    wait_until_ready,
)


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


# --- cmd_args ----------------------------------------------------------------

def test_cmd_args_parses_json_array(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, text, check: subprocess.CompletedProcess(cmd, 0, stdout='["redis-server","--requirepass","x"]'),
    )
    assert cmd_args("c-cache") == ["redis-server", "--requirepass", "x"]


def test_cmd_args_invalid_json_returns_empty_list(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, text, check: subprocess.CompletedProcess(cmd, 0, stdout="not json"),
    )
    assert cmd_args("c-cache") == []


# --- copy_from_container / copy_to_container (KEDGE-W-004, InfluxDB) ----------

def test_copy_from_container_success(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert copy_from_container("c-influx", "/tmp/backup", tmp_path) is True
    assert captured["cmd"] == ["docker", "cp", "c-influx:/tmp/backup", str(tmp_path)]


def test_copy_from_container_failure_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert copy_from_container("c-influx", "/tmp/backup", tmp_path) is False


def test_copy_to_container_success(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert copy_to_container("c-influx", tmp_path, "/tmp/restore") is True
    assert captured["cmd"] == ["docker", "cp", str(tmp_path), "c-influx:/tmp/restore"]


def test_copy_to_container_failure_returns_false(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert copy_to_container("c-influx", tmp_path, "/tmp/restore") is False


# --- wait_until_ready ----------------------------------------------------------

def test_wait_until_ready_returns_true_immediately_if_predicate_already_true():
    assert wait_until_ready(lambda: True, attempts=5, interval=0) is True


def test_wait_until_ready_returns_true_after_a_few_attempts(monkeypatch):
    calls = {"n": 0}

    def predicate():
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr("time.sleep", lambda s: None)
    assert wait_until_ready(predicate, attempts=5, interval=0) is True
    assert calls["n"] == 3


def test_wait_until_ready_returns_false_when_attempts_exhausted(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert wait_until_ready(lambda: False, attempts=3, interval=0) is False
