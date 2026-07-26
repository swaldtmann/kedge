"""Stack lifecycle — port of stop_stack/start_stack (backup.sh:702-724)."""

from __future__ import annotations

import subprocess

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
