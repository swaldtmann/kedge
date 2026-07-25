"""End-to-end CLI test for `kedge discover`, mocked at the subprocess boundary."""

import json
import subprocess

from click.testing import CliRunner

from kedge.cli import main


def _mock_compose_ok(monkeypatch, config_json):
    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["docker", "compose"] and "version" in cmd:
            return subprocess.CompletedProcess(cmd, 0)
        if "config" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(config_json), stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")


def test_discover_json_output(monkeypatch, tmp_path):
    _mock_compose_ok(monkeypatch, {"services": {"web": {"image": "traefik:v3.1"}}})
    (tmp_path / "docker-compose.yml").write_text("services:\n  web:\n    image: traefik:v3.1\n")

    monkeypatch.setenv("STACK_DIR", str(tmp_path))
    monkeypatch.setenv("RESTIC_REPOSITORY", "/backup/x")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")

    result = CliRunner().invoke(main, ["discover", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["services"][0]["service"] == "web"
    assert payload["hot_backup_safety"]["all_safe"] is True


def test_discover_human_readable(monkeypatch, tmp_path):
    _mock_compose_ok(monkeypatch, {"services": {"app": {"image": "myapp:1.0"}}})
    (tmp_path / "docker-compose.yml").write_text("services:\n  app:\n    image: myapp:1.0\n")

    monkeypatch.setenv("STACK_DIR", str(tmp_path))
    monkeypatch.setenv("RESTIC_REPOSITORY", "/backup/x")
    monkeypatch.setenv("RESTIC_PASSWORD", "secret")

    result = CliRunner().invoke(main, ["discover"])

    assert result.exit_code == 0, result.output
    assert "=== Stack:" in result.output
    assert "myapp:1.0" in result.output


def test_discover_missing_restic_env_fails_loud(monkeypatch, tmp_path):
    _mock_compose_ok(monkeypatch, {"services": {}})
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")

    monkeypatch.setenv("STACK_DIR", str(tmp_path))
    monkeypatch.delenv("RESTIC_REPOSITORY", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)

    result = CliRunner().invoke(main, ["discover"])

    assert result.exit_code == 1
    assert "RESTIC_REPOSITORY not set" in result.output
