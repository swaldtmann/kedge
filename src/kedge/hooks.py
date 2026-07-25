"""DB pre-hooks — dump running database containers before backup.
Port of backup.sh:373-500 (run_pre_hooks + per-type dump logic).
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
from pathlib import Path

from kedge import log
from kedge.discovery import detect_db_type, discover_services
from kedge.errors import KedgeError


def _running_container(stack_dir: Path, compose_cmd: list[str], svc: str) -> str:
    result = subprocess.run(
        [*compose_cmd, "ps", "-q", svc], cwd=stack_dir, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return ""
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else ""


def _container_name(container: str) -> str:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Name}}", container],
        capture_output=True, text=True, check=False,
    )
    name = result.stdout.strip()
    return name.lstrip("/") if name else container


def _container_env(container: str) -> dict[str, str]:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", container],
        capture_output=True, text=True, check=False,
    )
    env: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            env[key] = value
    return env


def _first_env(env: dict[str, str], *keys: str) -> str:
    for key in keys:
        if key in env:
            return env[key]
    return ""


def _binary_exists(container: str, binary: str) -> bool:
    result = subprocess.run(
        ["docker", "exec", container, "which", binary],
        capture_output=True, check=False,
    )
    return result.returncode == 0


def _exec_output(container: str, cmd: list[str], env_vars: dict[str, str] | None = None) -> str:
    docker_cmd = ["docker", "exec"]
    for key, value in (env_vars or {}).items():
        docker_cmd += ["-e", f"{key}={value}"]
    docker_cmd += [container, *cmd]
    result = subprocess.run(docker_cmd, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def _cmd_args(container: str) -> list[str]:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Cmd}}", container],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(result.stdout) or []
    except ValueError:
        return []


def _stream_gzip(container_cmd: list[str], dest_path: Path, env_vars: dict[str, str] | None = None) -> bool:
    """`docker exec <container_cmd> | gzip > dest_path`, streamed (no full-dump
    buffering in memory). container_cmd is [container, *actual_command]."""
    docker_cmd = ["docker", "exec"]
    for key, value in (env_vars or {}).items():
        docker_cmd += ["-e", f"{key}={value}"]
    docker_cmd += container_cmd
    with subprocess.Popen(docker_cmd, stdout=subprocess.PIPE) as proc:
        with gzip.open(dest_path, "wb") as gz:
            shutil.copyfileobj(proc.stdout, gz)
        proc.wait()
    return proc.returncode == 0


def _stream_raw(container_cmd: list[str], dest_path: Path) -> bool:
    """`docker exec <container_cmd> > dest_path` — mongodump already gzips its
    own output, so no extra gzip wrapping here. container_cmd is
    [container, *actual_command]."""
    with subprocess.Popen(["docker", "exec", *container_cmd], stdout=subprocess.PIPE) as proc:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(proc.stdout, f)
        proc.wait()
    return proc.returncode == 0


def _dump_postgres(svc: str, container: str, container_name: str, dump_dir: Path) -> None:
    log.info(f"Dumping PostgreSQL ({container_name})...")
    pg_user = _first_env(_container_env(container), "POSTGRES_USER") or "postgres"
    dest = dump_dir / f"{svc}_postgres.sql.gz"
    ok = _stream_gzip([container, "pg_dumpall", "-U", pg_user], dest)
    if not ok:
        raise KedgeError(f"PostgreSQL dump for '{svc}' ({container_name}) failed")
    log.ok(f"PostgreSQL dump: {dest.name}")


def _dump_mysql(svc: str, container: str, container_name: str, dump_dir: Path) -> None:
    log.info(f"Dumping MySQL/MariaDB ({container_name})...")
    env = _container_env(container)
    mysql_pass = _first_env(env, "MYSQL_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD", "DBROOT")
    if not mysql_pass:
        raise KedgeError(
            f"MySQL/MariaDB dump for '{svc}' ({container_name}): no root password found via "
            f"known env vars (MYSQL_ROOT_PASSWORD/MARIADB_ROOT_PASSWORD/DBROOT) — refusing an "
            f"unauthenticated dump attempt that could silently produce an empty/partial backup."
        )
    dump_cmd = "mariadb-dump" if _binary_exists(container, "mariadb-dump") else "mysqldump"
    dest = dump_dir / f"{svc}_mysql.sql.gz"
    ok = _stream_gzip(
        [container, dump_cmd, "--all-databases", "-uroot"], dest, env_vars={"MYSQL_PWD": mysql_pass},
    )
    if not ok:
        raise KedgeError(f"MySQL/MariaDB dump for '{svc}' ({container_name}) failed ({dump_cmd}) — not marking as ok.")
    log.ok(f"MySQL dump: {dest.name}")


def _discover_valkey_password(container: str) -> str:
    env = _container_env(container)
    password = _first_env(env, "VALKEY_PASSWORD", "REDIS_PASSWORD")
    if password:
        return password

    # AFKI-W-047: mounted password file (preferred pattern)
    secret = _exec_output(container, ["cat", "/run/secrets/valkey_password"]).strip()
    if secret:
        return secret

    # Legacy fallback: --requirepass in command args (pre-W-047)
    args = _cmd_args(container)
    for i, arg in enumerate(args):
        if arg == "--requirepass" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--requirepass="):
            return arg.split("=", 1)[1]
    return ""


def _dump_valkey(svc: str, container: str, container_name: str) -> None:
    log.info(f"Triggering BGSAVE on {container_name}...")
    vk_pass = _discover_valkey_password(container)
    env_vars = {"REDISCLI_AUTH": vk_pass} if vk_pass else {}
    docker_cmd = ["docker", "exec"]
    for key, value in env_vars.items():
        docker_cmd += ["-e", f"{key}={value}"]
    docker_cmd += [
        container, "sh", "-c",
        'if command -v valkey-cli >/dev/null 2>&1; then CLI=valkey-cli; else CLI=redis-cli; fi\n'
        'BEFORE=$($CLI LASTSAVE 2>/dev/null | tr -dc "0-9")\n'
        '$CLI BGSAVE >/dev/null 2>&1\n'
        'for i in $(seq 1 30); do\n'
        '    AFTER=$($CLI LASTSAVE 2>/dev/null | tr -dc "0-9")\n'
        '    if [ "$AFTER" != "$BEFORE" ] 2>/dev/null; then exit 0; fi\n'
        '    sleep 1\n'
        'done\n',
    ]
    subprocess.run(docker_cmd, capture_output=True, check=False)  # best-effort, like `|| true` in the shell
    log.ok(f"BGSAVE completed on {container_name}")


def _dump_mongo(svc: str, container: str, container_name: str, dump_dir: Path) -> None:
    log.info(f"Dumping MongoDB ({container_name})...")
    dest = dump_dir / f"{svc}_mongo.archive.gz"
    ok = _stream_raw([container, "mongodump", "--archive", "--gzip"], dest)
    if not ok:
        raise KedgeError(f"MongoDB dump for '{svc}' ({container_name}) failed")
    log.ok(f"MongoDB dump: {dest.name}")


def run_pre_hooks(config: dict, stack_dir: Path, compose_cmd: list[str], dump_dir: Path) -> int:
    dump_dir.mkdir(parents=True, exist_ok=True)
    hooks_run = 0

    for svc, image in discover_services(config):
        if not svc:
            continue
        db_type = detect_db_type(image)
        if not db_type:
            continue

        container = _running_container(stack_dir, compose_cmd, svc)
        if not container:
            log.warn(f"Service '{svc}' ({db_type}) not running — skipping dump")
            continue

        container_name = _container_name(container)

        if db_type == "postgres":
            _dump_postgres(svc, container, container_name, dump_dir)
            hooks_run += 1
        elif db_type == "mysql":
            _dump_mysql(svc, container, container_name, dump_dir)
            hooks_run += 1
        elif db_type == "valkey":
            _dump_valkey(svc, container, container_name)
            hooks_run += 1
        elif db_type == "mongo":
            _dump_mongo(svc, container, container_name, dump_dir)
            hooks_run += 1

    if hooks_run == 0:
        log.info("No database containers detected — skipping pre-hooks")
    else:
        log.ok(f"{hooks_run} database hook(s) completed")

    return hooks_run
