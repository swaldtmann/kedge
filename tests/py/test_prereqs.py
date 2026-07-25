"""Unit tests for kedge.prereqs — port of backup.sh check_prereqs()."""

import subprocess

import pytest

from kedge.config import Config
from kedge.errors import KedgeError
from kedge.prereqs import check_prereqs


def _cfg(tmp_path, **overrides):
    defaults = dict(
        stack_dir=tmp_path,
        restic_repository="/backup/mystack",
        restic_password="secret",
        restic_password_file="",
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_missing_required_tools(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda tool: None)
    with pytest.raises(KedgeError, match="Missing required tools"):
        check_prereqs(_cfg(tmp_path))


def test_docker_compose_v2_detected(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 0),
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    result = check_prereqs(_cfg(tmp_path))
    assert result.compose_cmd == ["docker", "compose"]


def test_falls_back_to_docker_compose_v1(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "shutil.which",
        lambda tool: "/usr/bin/docker-compose" if tool == "docker-compose" else (f"/usr/bin/{tool}" if tool in ("docker", "restic") else None),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 1),
    )
    (tmp_path / "compose.yaml").write_text("services: {}\n")

    result = check_prereqs(_cfg(tmp_path))
    assert result.compose_cmd == ["docker-compose"]


def test_no_compose_binary_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "shutil.which",
        lambda tool: f"/usr/bin/{tool}" if tool in ("docker", "restic") else None,
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 1),
    )
    with pytest.raises(KedgeError, match="Neither 'docker compose'"):
        check_prereqs(_cfg(tmp_path))


def test_no_compose_file_found(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 0),
    )
    with pytest.raises(KedgeError, match="No docker-compose file found"):
        check_prereqs(_cfg(tmp_path))


def test_env_file_appended_to_compose_cmd(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 0),
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".env").write_text("FOO=bar\n")

    result = check_prereqs(_cfg(tmp_path))
    assert result.compose_cmd == ["docker", "compose", "--env-file", str(tmp_path / ".env")]


def test_missing_restic_repository(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 0),
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    with pytest.raises(KedgeError, match="RESTIC_REPOSITORY not set"):
        check_prereqs(_cfg(tmp_path, restic_repository=""))


def test_missing_restic_password_and_file(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 0),
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    with pytest.raises(KedgeError, match="RESTIC_PASSWORD"):
        check_prereqs(_cfg(tmp_path, restic_password="", restic_password_file=""))


def test_restic_password_file_satisfies_check(monkeypatch, tmp_path):
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, check: subprocess.CompletedProcess(cmd, 0),
    )
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    result = check_prereqs(_cfg(tmp_path, restic_password="", restic_password_file="/etc/kedge/pw"))
    assert result.compose_cmd == ["docker", "compose"]
