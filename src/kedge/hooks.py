"""DB pre-hooks — dump running database containers before backup.
Port of backup.sh:373-500 (run_pre_hooks + per-type dump logic).

KEDGE-W-004: the per-engine dump implementations (_dump_postgres et al.)
moved to kedge.engines — this module is now just the generic "loop over
discovered services, call the matching engine's dump()" dispatcher, driven
by the DB engine registry instead of an if/elif chain hand-synced with
discovery.py/restore.py/verify.py.
"""

from __future__ import annotations

from pathlib import Path

from kedge import docker_stack, log
from kedge.discovery import detect_db_type, discover_services
from kedge.engines import engine_by_name


def run_pre_hooks(config: dict, stack_dir: Path, compose_cmd: list[str], dump_dir: Path) -> int:
    dump_dir.mkdir(parents=True, exist_ok=True)
    hooks_run = 0

    for svc, image in discover_services(config):
        if not svc:
            continue
        db_type = detect_db_type(image)
        if not db_type:
            continue
        engine = engine_by_name(db_type)
        if engine is None or engine.dump is None:
            continue

        container = docker_stack.container_for_service(stack_dir, compose_cmd, svc)
        if not container:
            log.warn(f"Service '{svc}' ({db_type}) not running — skipping dump")
            continue

        container_name = docker_stack.container_name(container)
        engine.dump(svc, container, container_name, dump_dir, image)
        hooks_run += 1

    if hooks_run == 0:
        log.info("No database containers detected — skipping pre-hooks")
    else:
        log.ok(f"{hooks_run} database hook(s) completed")

    return hooks_run
