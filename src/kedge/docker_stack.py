"""Stack lifecycle — port of stop_stack/start_stack (backup.sh:702-724).

Also home of the generic (engine-agnostic) `docker exec`/`docker cp` helpers
used by kedge.engines' per-DB-engine dump/import implementations — these were
previously duplicated between hooks.py and restore.py (KEDGE-W-004)."""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import time
from pathlib import Path

from kedge import log


def is_stack_running(stack_dir, compose_cmd: list[str]) -> bool:
    result = subprocess.run(
        [*compose_cmd, "ps", "-q"], cwd=stack_dir, capture_output=True, text=True, check=False,
    )
    return bool(result.stdout.strip())


def stop_stack(stack_dir, compose_cmd: list[str], backup_stop_stack: bool) -> bool:
    """Returns True if the stack was running (and just got stopped)."""
    if not backup_stop_stack:
        log.info("BACKUP_STOP_STACK=false — stack stays running (backup may be inconsistent)")
        return False
    if not is_stack_running(stack_dir, compose_cmd):
        return False
    log.info("Stopping stack for consistent backup...")
    subprocess.run([*compose_cmd, "stop"], cwd=stack_dir, check=True)
    log.ok("Stack stopped")
    return True


def start_stack(stack_dir, compose_cmd: list[str], was_running: bool) -> None:
    if not was_running:
        return
    log.info("Restarting stack...")
    subprocess.run([*compose_cmd, "start"], cwd=stack_dir, check=True)
    log.ok("Stack restarted")


def container_for_service(stack_dir, compose_cmd: list[str], svc: str) -> str:
    """First running container id for a compose service, or "" if none."""
    result = subprocess.run(
        [*compose_cmd, "ps", "-q", svc], cwd=stack_dir, capture_output=True, text=True, check=False,
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else ""


def container_env(container: str) -> dict[str, str]:
    """Container's env vars as a dict, parsed from `docker inspect`."""
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


def container_name(container: str) -> str:
    """`docker inspect --format {{.Name}}`, leading '/' stripped."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.Name}}", container],
        capture_output=True, text=True, check=False,
    )
    name = result.stdout.strip()
    return name.lstrip("/") if name else container


def first_env(env: dict[str, str], *keys: str) -> str:
    """First present key from env, in priority order — empty string if none match."""
    for key in keys:
        if key in env:
            return env[key]
    return ""


def binary_exists(container: str, binary: str) -> bool:
    result = subprocess.run(["docker", "exec", container, "which", binary], capture_output=True, check=False)
    return result.returncode == 0


def exec_output(container: str, cmd: list[str], env_vars: dict[str, str] | None = None) -> str:
    docker_cmd = ["docker", "exec"]
    for key, value in (env_vars or {}).items():
        docker_cmd += ["-e", f"{key}={value}"]
    docker_cmd += [container, *cmd]
    result = subprocess.run(docker_cmd, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def cmd_args(container: str) -> list[str]:
    """Container's `Config.Cmd`, as parsed from `docker inspect --format json`."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Cmd}}", container],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(result.stdout) or []
    except ValueError:
        return []


def stream_gzip(container_cmd: list[str], dest_path: Path, env_vars: dict[str, str] | None = None) -> bool:
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


def stream_raw(container_cmd: list[str], dest_path: Path) -> bool:
    """`docker exec <container_cmd> > dest_path` (no gzip wrapping — for tools
    like mongodump that already compress their own output). container_cmd is
    [container, *actual_command]."""
    with subprocess.Popen(["docker", "exec", *container_cmd], stdout=subprocess.PIPE) as proc:
        with open(dest_path, "wb") as f:
            shutil.copyfileobj(proc.stdout, f)
        proc.wait()
    return proc.returncode == 0


def copy_from_container(container: str, src_path: str, dest_dir: Path) -> bool:
    """`docker cp <container>:<src_path> <dest_dir>` — pulls a file or directory
    tree out of a container (used by engines whose backup tool writes to a
    directory instead of stdout, e.g. InfluxDB's `influx backup`)."""
    result = subprocess.run(
        ["docker", "cp", f"{container}:{src_path}", str(dest_dir)], capture_output=True, check=False,
    )
    return result.returncode == 0


def copy_to_container(container: str, src_path: Path, dest_path: str) -> bool:
    """`docker cp <src_path> <container>:<dest_path>` — the inverse of
    copy_from_container, used on restore."""
    result = subprocess.run(
        ["docker", "cp", str(src_path), f"{container}:{dest_path}"], capture_output=True, check=False,
    )
    return result.returncode == 0


def wait_until_ready(predicate, attempts: int = 30, interval: float = 2.0) -> bool:
    """Poll predicate() until it returns truthy or attempts are exhausted."""
    for _ in range(attempts):
        if predicate():
            return True
        time.sleep(interval)
    return False
