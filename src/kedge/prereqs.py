"""Prerequisite checks — port of backup.sh check_prereqs() (lines 161-206).

jq is dropped as a requirement: the shell version shells out to jq for JSON
handling, Python parses JSON natively. That's strictly fewer moving parts,
not a behavior change a user would observe.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from kedge.config import Config
from kedge.errors import KedgeError

COMPOSE_FILE_CANDIDATES = (
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
)

REQUIRED_TOOLS = ("docker", "restic")


@dataclass
class Prereqs:
    compose_cmd: list[str]


def detect_compose_cmd() -> list[str]:
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    except FileNotFoundError:
        pass

    if shutil.which("docker-compose"):
        return ["docker-compose"]

    raise KedgeError("Neither 'docker compose' nor 'docker-compose' found")


def _find_compose_file(stack_dir) -> bool:
    return any((stack_dir / name).is_file() for name in COMPOSE_FILE_CANDIDATES)


def check_prereqs(cfg: Config) -> Prereqs:
    missing = [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        raise KedgeError(f"Missing required tools: {' '.join(missing)}")

    compose_cmd = detect_compose_cmd()

    if not _find_compose_file(cfg.stack_dir):
        raise KedgeError(f"No docker-compose file found in {cfg.stack_dir}")

    env_file = cfg.stack_dir / ".env"
    if env_file.is_file():
        compose_cmd = [*compose_cmd, "--env-file", str(env_file)]

    if not cfg.restic_repository:
        raise KedgeError("RESTIC_REPOSITORY not set")
    if not cfg.restic_password and not cfg.restic_password_file:
        raise KedgeError("RESTIC_PASSWORD or RESTIC_PASSWORD_FILE not set")

    return Prereqs(compose_cmd=compose_cmd)
