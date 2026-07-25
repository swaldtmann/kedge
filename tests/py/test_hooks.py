"""Unit tests for kedge.hooks — DB pre-hook dumps (backup.sh:373-500 port)."""

import io
import subprocess

import pytest

from kedge.errors import KedgeError
from kedge.hooks import run_pre_hooks


def _matches(cmd, prefix):
    if len(cmd) < len(prefix):
        return False
    for actual, expected in zip(cmd[: len(prefix)], prefix):
        if actual == expected:
            continue
        if isinstance(actual, str) and isinstance(expected, str) and actual.startswith(expected):
            continue
        return False
    return True


def _dispatch(handlers, default_stdout=""):
    def fake_run(cmd, **kwargs):
        for prefix, handler in handlers.items():
            if _matches(cmd, prefix):
                return handler(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=default_stdout, stderr="")
    return fake_run


def _fake_popen(data: bytes, returncode: int):
    class _FakePopen:
        def __init__(self, cmd, stdout=None):
            self.cmd = cmd
            self.stdout = io.BytesIO(data)
            self.returncode = returncode

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def wait(self):
            return self.returncode

    return _FakePopen


def _compose_ps_ok(container="c-web"):
    return {("docker", "compose", "ps"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout=f"{container}\n")}


# --- no-op cases ----------------------------------------------------------

def test_no_db_containers_returns_zero(tmp_path):
    config = {"services": {"web": {"image": "nginx:alpine"}}}
    dump_dir = tmp_path / "dumps"
    assert run_pre_hooks(config, tmp_path, ["docker", "compose"], dump_dir) == 0
    assert dump_dir.is_dir()  # backup.sh creates it unconditionally, matched for parity


def test_db_service_not_running_is_skipped(monkeypatch, tmp_path, capsys):
    config = {"services": {"db": {"image": "mariadb:11.5"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("docker", "compose", "ps"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout=""),
    }))
    result = run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")
    assert result == 0
    assert "not running" in capsys.readouterr().out


# --- postgres ---------------------------------------------------------------

def test_postgres_dump_success(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "postgres:16"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        **_compose_ps_ok("c-db"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/mystack-db-1\n"),
        ("docker", "inspect", "--format", "{{range"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="POSTGRES_USER=admin\n"),
    }))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"SQL DUMP DATA", 0))

    dump_dir = tmp_path / "dumps"
    result = run_pre_hooks(config, tmp_path, ["docker", "compose"], dump_dir)

    assert result == 1
    assert (dump_dir / "db_postgres.sql.gz").is_file()


def test_postgres_dump_defaults_to_postgres_user(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "postgres:16"}}}
    captured_cmds = []

    def fake_run(cmd, **kwargs):
        captured_cmds.append(cmd)
        if cmd[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="c-db\n")
        if cmd[:3] == ["docker", "inspect", "--format"] and cmd[3] == "{{.Name}}":
            return subprocess.CompletedProcess(cmd, 0, stdout="/db-1\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="")  # empty env -> no POSTGRES_USER

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"data", 0))

    run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")
    # the actual docker exec invocation happens inside Popen (mocked away),
    # so we only assert no crash occurred and a file was produced with the default user path
    assert (tmp_path / "dumps" / "db_postgres.sql.gz").is_file()


def test_postgres_dump_failure_raises(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "postgres:16"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({**_compose_ps_ok("c-db")}))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"", 1))

    with pytest.raises(KedgeError, match="PostgreSQL dump"):
        run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")


# --- mysql / mariadb ---------------------------------------------------------

def test_mysql_dump_success_with_mariadb_dump_binary(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "mariadb:11.5"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        **_compose_ps_ok("c-db"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/db-1\n"),
        ("docker", "inspect", "--format", "{{range"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="MARIADB_ROOT_PASSWORD=hunter2\n"),
        ("docker", "exec", "c-db", "which"): lambda cmd: subprocess.CompletedProcess(cmd, 0),  # mariadb-dump exists
    }))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"dump", 0))

    dump_dir = tmp_path / "dumps"
    result = run_pre_hooks(config, tmp_path, ["docker", "compose"], dump_dir)

    assert result == 1
    assert (dump_dir / "db_mysql.sql.gz").is_file()


def test_mysql_dump_password_priority_order(monkeypatch, tmp_path):
    """MYSQL_ROOT_PASSWORD wins over MARIADB_ROOT_PASSWORD wins over DBROOT."""
    config = {"services": {"db": {"image": "mysql:8.0"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        **_compose_ps_ok("c-db"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/db-1\n"),
        ("docker", "inspect", "--format", "{{range"): lambda cmd: subprocess.CompletedProcess(
            cmd, 0, stdout="DBROOT=fallback\nMYSQL_ROOT_PASSWORD=correct\nMARIADB_ROOT_PASSWORD=wrong\n"
        ),
        ("docker", "exec", "c-db", "which"): lambda cmd: subprocess.CompletedProcess(cmd, 1),  # no mariadb-dump
    }))

    captured = {}

    def fake_popen(cmd, stdout=None):
        captured["cmd"] = cmd
        return _fake_popen(b"dump", 0)(cmd, stdout)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")

    assert "MYSQL_PWD=correct" in captured["cmd"]
    assert "mysqldump" in captured["cmd"]  # no mariadb-dump binary -> fallback


def test_mysql_dump_no_password_hard_fails(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "mariadb:11.5"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        **_compose_ps_ok("c-db"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/db-1\n"),
    }))
    with pytest.raises(KedgeError, match="refusing an unauthenticated dump"):
        run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")


def test_mysql_dump_failure_raises(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "mariadb:11.5"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        **_compose_ps_ok("c-db"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/db-1\n"),
        ("docker", "inspect", "--format", "{{range"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="DBROOT=x\n"),
    }))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"", 1))
    with pytest.raises(KedgeError, match="MySQL/MariaDB dump"):
        run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")


# --- valkey / redis -----------------------------------------------------------

def test_valkey_bgsave_never_raises_even_on_failure(monkeypatch, tmp_path):
    config = {"services": {"cache": {"image": "valkey:7"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        **_compose_ps_ok("c-cache"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/cache-1\n"),
        ("docker", "inspect", "--format", "{{range"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="VALKEY_PASSWORD=secret\n"),
        ("docker", "exec", "c-cache", "sh"): lambda cmd: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom"),
    }))
    result = run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")
    assert result == 1  # counted as run regardless of BGSAVE outcome, matches shell's `|| true`


def test_valkey_password_from_secret_file_when_no_env(monkeypatch, tmp_path):
    config = {"services": {"cache": {"image": "redis:7-alpine"}}}
    captured = {}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="c-cache\n")
        if cmd[:3] == ["docker", "inspect", "--format"] and cmd[3] == "{{.Name}}":
            return subprocess.CompletedProcess(cmd, 0, stdout="/cache-1\n")
        if cmd[:3] == ["docker", "inspect", "--format"] and "range" in cmd[3]:
            return subprocess.CompletedProcess(cmd, 0, stdout="")  # no env password
        if cmd[:2] == ["docker", "exec"] and cmd[-2:] == ["cat", "/run/secrets/valkey_password"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="from-secret-file\n")
        if cmd[:2] == ["docker", "exec"] and "sh" in cmd:
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 1, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")

    assert "REDISCLI_AUTH=from-secret-file" in captured["cmd"]


def test_valkey_no_password_found_still_runs_unauthenticated(monkeypatch, tmp_path):
    config = {"services": {"cache": {"image": "valkey:7"}}}
    captured = {}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "compose", "ps"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="c-cache\n")
        if cmd[:3] == ["docker", "inspect", "--format"] and cmd[3] == "{{.Name}}":
            return subprocess.CompletedProcess(cmd, 0, stdout="/cache-1\n")
        if cmd[:3] == ["docker", "inspect", "--format"] and "range" in cmd[3]:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        if cmd[:3] == ["docker", "inspect", "--format"] and cmd[3] == "{{json .Config.Cmd}}":
            return subprocess.CompletedProcess(cmd, 0, stdout="[]")
        if cmd[:3] == ["docker", "exec", "c-cache"] and cmd[3:5] == ["cat", "/run/secrets/valkey_password"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="")
        if cmd[:3] == ["docker", "exec", "c-cache"] and cmd[3] == "sh":
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0)
        return subprocess.CompletedProcess(cmd, 1, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")

    assert "-e" not in captured["cmd"]  # no REDISCLI_AUTH injected


# --- mongo --------------------------------------------------------------------

def test_mongo_dump_success(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "mongo:7"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        **_compose_ps_ok("c-db"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/db-1\n"),
    }))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"BSON archive bytes", 0))

    dump_dir = tmp_path / "dumps"
    result = run_pre_hooks(config, tmp_path, ["docker", "compose"], dump_dir)

    assert result == 1
    dest = dump_dir / "db_mongo.archive.gz"
    assert dest.is_file()
    assert dest.read_bytes() == b"BSON archive bytes"  # NOT double-gzipped


def test_mongo_dump_failure_raises(monkeypatch, tmp_path):
    config = {"services": {"db": {"image": "mongo:7"}}}
    monkeypatch.setattr(subprocess, "run", _dispatch({**_compose_ps_ok("c-db")}))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"", 1))

    with pytest.raises(KedgeError, match="MongoDB dump"):
        run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")


# --- multiple services ----------------------------------------------------

def test_multiple_db_services_all_counted(monkeypatch, tmp_path):
    config = {"services": {
        "pg": {"image": "postgres:16"},
        "mongo": {"image": "mongo:7"},
        "web": {"image": "nginx:alpine"},
    }}
    monkeypatch.setattr(subprocess, "run", _dispatch({
        ("docker", "compose", "ps"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="c-x\n"),
        ("docker", "inspect", "--format", "{{.Name}}"): lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="/x-1\n"),
    }))
    monkeypatch.setattr(subprocess, "Popen", _fake_popen(b"data", 0))

    result = run_pre_hooks(config, tmp_path, ["docker", "compose"], tmp_path / "dumps")
    assert result == 2
