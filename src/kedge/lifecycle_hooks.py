"""BACKUP_PRE_HOOK / BACKUP_POST_HOOK / BACKUP_FAIL_HOOK + healthcheck ping.
Port of backup.sh:112-155 (run_hook, ping_healthcheck).

Not to be confused with kedge.hooks (the per-service DB dump hooks) — the
shell script uses "hook" for both concepts too, but they're unrelated
mechanisms: this module runs one arbitrary admin-supplied shell command at
three points in the backup lifecycle, kedge.hooks dumps live databases.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from kedge import log


@dataclass
class HookContext:
    """Mirrors the exported BACKUP_* hook variables (backup.sh:88-95)."""

    duration: str = ""
    size: str = ""
    snapshot: str = ""
    hostname: str = ""
    stack: str = ""
    timestamp: str = ""
    error: str = ""

    def as_env(self) -> dict[str, str]:
        return {
            "BACKUP_DURATION": self.duration,
            "BACKUP_SIZE": self.size,
            "BACKUP_SNAPSHOT": self.snapshot,
            "BACKUP_HOSTNAME": self.hostname,
            "BACKUP_STACK": self.stack,
            "BACKUP_TIMESTAMP": self.timestamp,
            "BACKUP_ERROR": self.error,
        }


def run_hook(hook_cmd: str, hook_name: str, ctx: HookContext) -> None:
    """Runs hook_cmd via the shell (like the original's `eval`), with the
    hook context exported so the command string can reference $BACKUP_*.
    A failing hook only warns — it never fails the backup itself."""
    if not hook_cmd:
        return

    log.info(f"Running {hook_name}...")
    env = os.environ.copy()
    env.update(ctx.as_env())
    result = subprocess.run(hook_cmd, shell=True, env=env, check=False)
    if result.returncode == 0:
        log.ok(f"{hook_name} completed")
    else:
        log.warn(f"{hook_name} failed (exit {result.returncode}) — continuing")


def ping_healthcheck(url: str, status: str, ctx: HookContext) -> None:
    """status is "ok" or "fail". Silent, non-blocking, 10s timeout — a
    healthcheck outage must never fail the backup itself."""
    if not url:
        return

    target = f"{url.rstrip('/')}/fail" if status == "fail" else url

    body = f"host={ctx.hostname} stack={ctx.stack} snapshot={ctx.snapshot} duration={ctx.duration}s size={ctx.size}"
    if status == "fail" and ctx.error:
        body = f"error: {ctx.error} | {body}"

    try:
        subprocess.run(
            ["curl", "-sf", "--max-time", "10", "-X", "POST", "--data-raw", body, target],
            capture_output=True, check=False, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    log.ok(f"Healthcheck ping: {status}")
