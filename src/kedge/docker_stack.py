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
