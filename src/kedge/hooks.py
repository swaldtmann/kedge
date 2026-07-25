"""DB pre-hooks — dump running database containers before backup.

Full implementation (pg_dumpall/mysqldump/mariadb-dump/BGSAVE/mongodump)
lands in KEDGE-W-001 sub-task #4. Until then this only detects DB
containers and warns loudly — it does NOT invoke a real dump. A backup
that looks complete but silently skipped the DB dump would be worse than
no backup at all, so this stays noisy rather than a quiet no-op.
"""

from __future__ import annotations

from pathlib import Path

from kedge import log
from kedge.discovery import detect_db_type, discover_services


def run_pre_hooks(config: dict, dump_dir: Path) -> int:
    # backup.sh:375-376 creates dump_dir unconditionally, even with no DB
    # services — matched here for structural snapshot parity.
    dump_dir.mkdir(parents=True, exist_ok=True)

    detected = [
        (svc, image, detect_db_type(image))
        for svc, image in discover_services(config)
    ]
    detected = [(svc, image, db_type) for svc, image, db_type in detected if db_type]

    if not detected:
        log.info("No database containers detected — skipping pre-hooks")
        return 0

    for svc, image, db_type in detected:
        log.warn(
            f"Service '{svc}' ({db_type}, {image}) detected but the dump is not yet "
            f"ported (KEDGE-W-001 #4) — this backup will NOT contain a consistent DB dump for it"
        )
    return 0
