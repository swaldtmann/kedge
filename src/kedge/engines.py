"""DB engine registry (KEDGE-W-004) — one descriptor per database engine,
consumed by discovery.py/hooks.py/restore.py/verify.py instead of each
keeping its own hand-synced copy of "which DB types exist".

Anlass: the MariaDB bug from KEDGE-W-003 (modern mariadb:11+ images only ship
`mariadb`/`mariadb-admin`, not `mysql`/`mysqladmin`) wasn't a point bug — the
same DB type was defined at four separate places (discovery pattern, dump
hook, restore import, verify healthcheck), two of which (import, healthcheck)
were never updated when the dump hook grew its binary fallback. A new engine
now means one DBEngine() entry here, not four hand-edited lists.

Design note: healthcheck stays a **bash snippet** (executed on the remote
verify box inside verify.py's _HEALTHCHECK_SCRIPT), not a Python callable —
it inspects the *target box's* Docker/compose state over ssh, which cannot be
expressed as local Python. A DBEngine is therefore a plain descriptor with
mixed Python-callable and bash-string fields, not a uniform "everything is a
method" class.
"""

from __future__ import annotations

import gzip
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kedge import docker_stack, log
from kedge.errors import KedgeError

DumpFn = Callable[[str, str, str, Path, str], None]
# (svc, container, container_name, dump_dir, image) -> None. `image` is only
# consumed by engines whose dump behaviour depends on the image tag
# (InfluxDB v1 vs v2) -- the rest ignore it, kept for a uniform call site.

ImportFn = Callable[[list, Path, Path, str], None]
# (compose_cmd, restore_target, dump_path, svc) -> None


@dataclass(frozen=True)
class DBEngine:
    name: str
    image_patterns: tuple[str, ...] = ()
    """Image-name substrings for discovery.detect_db_type(). Empty for engines
    with no container-image auto-discovery (sqlite: an embedded library
    inside another app's image, not its own container)."""
    dump: DumpFn | None = None
    dump_suffix: str | None = None
    """Filename suffix dump() writes under dump_dir -- restore.py's generic
    dispatcher uses this to route a dump file back to import_()."""
    import_: ImportFn | None = None
    healthcheck_patterns: tuple[str, ...] = ()
    """Bash `case "$IMAGE" in` glob patterns, e.g. ("*postgres*", "*postgis*")."""
    healthcheck_bash: str = ""
    """Bash executed inside that case arm. Has $CONTAINER and $svc in scope
    (bound by the enclosing loop in verify.py's _HEALTHCHECK_SCRIPT) and is
    expected to increment $CHECKS/$FAILURES/$DB_CHECKED itself, same as
    every other arm."""

    def case_arm(self) -> str:
        if not self.healthcheck_patterns:
            return ""
        header = "|".join(self.healthcheck_patterns)
        return f"        {header})\n{self.healthcheck_bash}            ;;\n"


# --- Postgres ----------------------------------------------------------------

def dump_postgres(svc: str, container: str, container_name: str, dump_dir: Path, image: str = "") -> None:
    log.info(f"Dumping PostgreSQL ({container_name})...")
    pg_user = docker_stack.first_env(docker_stack.container_env(container), "POSTGRES_USER") or "postgres"
    dest = dump_dir / f"{svc}_postgres.sql.gz"
    ok = docker_stack.stream_gzip([container, "pg_dumpall", "-U", pg_user], dest)
    if not ok:
        raise KedgeError(f"PostgreSQL dump for '{svc}' ({container_name}) failed")
    log.ok(f"PostgreSQL dump: {dest.name}")


def import_postgres(compose_cmd: list, restore_target: Path, dump_path: Path, svc: str) -> None:
    container = docker_stack.container_for_service(restore_target, compose_cmd, svc)
    if not container:
        log.warn(f"  Container for {svc} not running — skip dump import")
        return
    pg_user = docker_stack.container_env(container).get("POSTGRES_USER") or "postgres"
    if not docker_stack.wait_until_ready(
        lambda: subprocess.run(
            ["docker", "exec", container, "pg_isready", "-U", pg_user], capture_output=True, check=False,
        ).returncode == 0,
    ):
        log.warn("  PostgreSQL not ready after 60s — attempting import anyway")
    # -d postgres: the maintenance DB always exists. Without it, psql connects
    # to a DB named after pg_user, which need not exist (POSTGRES_DB may differ
    # from POSTGRES_USER) -- the pg_dumpall stream then never gets applied and
    # the failure is swallowed as "may be harmless", i.e. silent data loss.
    with gzip.open(dump_path, "rb") as gz:
        result = subprocess.run(
            ["docker", "exec", "-i", container, "psql", "-U", pg_user, "-d", "postgres"],
            stdin=gz, capture_output=True, text=True, check=False,
        )
    if result.returncode != 0:
        log.warn(f"PostgreSQL import reported errors (may be harmless): {result.stderr[-500:]}")
    log.ok("  PostgreSQL dump imported")


_POSTGRES_HEALTHCHECK = """            PG_USER=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \\
                | grep '^POSTGRES_USER=' | cut -d= -f2)
            PG_USER="${PG_USER:-postgres}"
            if docker exec "$CONTAINER" pg_isready -U "$PG_USER" >/dev/null 2>&1; then
                echo "  PASS: PostgreSQL [$svc] accepting connections"
            else
                echo "  FAIL: PostgreSQL [$svc] not ready"
                FAILURES=$((FAILURES + 1))
            fi
            DB_CHECKED=$((DB_CHECKED + 1))
            CHECKS=$((CHECKS + 1))
"""

POSTGRES = DBEngine(
    name="postgres",
    image_patterns=("postgres", "postgis"),
    dump=dump_postgres,
    dump_suffix="_postgres.sql.gz",
    import_=import_postgres,
    healthcheck_patterns=("*postgres*", "*postgis*"),
    healthcheck_bash=_POSTGRES_HEALTHCHECK,
)


# --- MySQL / MariaDB -----------------------------------------------------------

def dump_mysql(svc: str, container: str, container_name: str, dump_dir: Path, image: str = "") -> None:
    log.info(f"Dumping MySQL/MariaDB ({container_name})...")
    env = docker_stack.container_env(container)
    mysql_pass = docker_stack.first_env(env, "MYSQL_ROOT_PASSWORD", "MARIADB_ROOT_PASSWORD", "DBROOT")
    if not mysql_pass:
        raise KedgeError(
            f"MySQL/MariaDB dump for '{svc}' ({container_name}): no root password found via "
            f"known env vars (MYSQL_ROOT_PASSWORD/MARIADB_ROOT_PASSWORD/DBROOT) — refusing an "
            f"unauthenticated dump attempt that could silently produce an empty/partial backup."
        )
    dump_cmd = "mariadb-dump" if docker_stack.binary_exists(container, "mariadb-dump") else "mysqldump"
    dest = dump_dir / f"{svc}_mysql.sql.gz"
    ok = docker_stack.stream_gzip(
        [container, dump_cmd, "--all-databases", "-uroot"], dest, env_vars={"MYSQL_PWD": mysql_pass},
    )
    if not ok:
        raise KedgeError(f"MySQL/MariaDB dump for '{svc}' ({container_name}) failed ({dump_cmd}) — not marking as ok.")
    log.ok(f"MySQL dump: {dest.name}")


def import_mysql(compose_cmd: list, restore_target: Path, dump_path: Path, svc: str) -> None:
    container = docker_stack.container_for_service(restore_target, compose_cmd, svc)
    if not container:
        log.warn(f"  Container for {svc} not running — skip dump import")
        return
    env = docker_stack.container_env(container)
    mysql_pass = env.get("MYSQL_ROOT_PASSWORD") or env.get("MARIADB_ROOT_PASSWORD") or ""
    # KEDGE-W-004: modern mariadb:11+ images only ship mariadb/mariadb-admin, not
    # mysql/mysqladmin -- the exact same binary split dump_mysql() above already
    # handles, but restore's import (and verify's healthcheck) had never picked
    # it up. Probe once, use consistently for both the readiness poll and the
    # actual import client.
    admin_bin = "mariadb-admin" if docker_stack.binary_exists(container, "mariadb-admin") else "mysqladmin"
    client_bin = "mariadb" if docker_stack.binary_exists(container, "mariadb") else "mysql"
    exec_prefix = ["docker", "exec"]
    if mysql_pass:
        exec_prefix += ["-e", f"MYSQL_PWD={mysql_pass}"]
    if not docker_stack.wait_until_ready(
        lambda: subprocess.run(
            [*exec_prefix, container, admin_bin, "ping", "-uroot"], capture_output=True, check=False,
        ).returncode == 0,
    ):
        log.warn("  MySQL/MariaDB not ready after 60s — attempting import anyway")
    with gzip.open(dump_path, "rb") as gz:
        result = subprocess.run(
            [*exec_prefix, "-i", container, client_bin, "-uroot"],
            stdin=gz, capture_output=True, text=True, check=False,
        )
    if result.returncode != 0:
        log.warn(f"MySQL/MariaDB import reported errors (may be harmless): {result.stderr[-500:]}")
    log.ok("  MySQL/MariaDB dump imported")


_MYSQL_HEALTHCHECK = """            ROOT_PASS=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \\
                | grep -E '^(MYSQL|MARIADB)_ROOT_PASSWORD=' | head -1 | cut -d= -f2)
            if docker exec "$CONTAINER" sh -c 'command -v mariadb-admin >/dev/null 2>&1'; then
                ADMIN_BIN=mariadb-admin
            else
                ADMIN_BIN=mysqladmin
            fi
            if docker exec "$CONTAINER" "$ADMIN_BIN" ping -uroot "-p${ROOT_PASS}" >/dev/null 2>&1; then
                echo "  PASS: MySQL/MariaDB [$svc] accepting connections"
            else
                echo "  FAIL: MySQL/MariaDB [$svc] not responding"
                FAILURES=$((FAILURES + 1))
            fi
            DB_CHECKED=$((DB_CHECKED + 1))
            CHECKS=$((CHECKS + 1))
"""

MYSQL = DBEngine(
    name="mysql",
    image_patterns=("mariadb", "mysql"),
    dump=dump_mysql,
    dump_suffix="_mysql.sql.gz",
    import_=import_mysql,
    healthcheck_patterns=("*mariadb*", "*mysql*"),
    healthcheck_bash=_MYSQL_HEALTHCHECK,
)


# --- Valkey / Redis ------------------------------------------------------------

def _discover_valkey_password(container: str) -> str:
    env = docker_stack.container_env(container)
    password = docker_stack.first_env(env, "VALKEY_PASSWORD", "REDIS_PASSWORD")
    if password:
        return password

    # AFKI-W-047: mounted password file (preferred pattern)
    secret = docker_stack.exec_output(container, ["cat", "/run/secrets/valkey_password"]).strip()
    if secret:
        return secret

    # Legacy fallback: --requirepass in command args (pre-W-047)
    args = docker_stack.cmd_args(container)
    for i, arg in enumerate(args):
        if arg == "--requirepass" and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith("--requirepass="):
            return arg.split("=", 1)[1]
    return ""


def dump_valkey(svc: str, container: str, container_name: str, dump_dir: Path, image: str = "") -> None:
    """No dump file -- Valkey/Redis persistence is BGSAVE into the container's
    own volume, restored via the generic volume restore path, not a dump
    import. dump_dir/image are accepted (unused) only for a uniform call site."""
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


_VALKEY_HEALTHCHECK = """            if docker exec "$CONTAINER" sh -c 'command -v valkey-cli >/dev/null && valkey-cli PING 2>/dev/null || redis-cli PING 2>/dev/null' | grep -qi pong; then
                echo "  PASS: Valkey/Redis [$svc] responding to PING"
            else
                PASS=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \\
                    | grep -E '^(VALKEY|REDIS)_PASSWORD=' | head -1 | cut -d= -f2 || true)
                if [ -n "$PASS" ]; then
                    if docker exec "$CONTAINER" sh -c "command -v valkey-cli >/dev/null && valkey-cli -a '$PASS' PING 2>/dev/null || redis-cli -a '$PASS' PING 2>/dev/null" | grep -qi pong; then
                        echo "  PASS: Valkey/Redis [$svc] responding to PING [with auth]"
                    else
                        echo "  FAIL: Valkey/Redis [$svc] not responding"
                        FAILURES=$((FAILURES + 1))
                    fi
                else
                    echo "  WARN: Valkey/Redis [$svc] PING failed [may need auth]"
                fi
            fi
            DB_CHECKED=$((DB_CHECKED + 1))
            CHECKS=$((CHECKS + 1))
"""

VALKEY = DBEngine(
    name="valkey",
    image_patterns=("valkey", "redis"),
    dump=dump_valkey,
    dump_suffix=None,
    import_=None,
    healthcheck_patterns=("*valkey*", "*redis*"),
    healthcheck_bash=_VALKEY_HEALTHCHECK,
)


# --- MongoDB -------------------------------------------------------------------

def dump_mongo(svc: str, container: str, container_name: str, dump_dir: Path, image: str = "") -> None:
    log.info(f"Dumping MongoDB ({container_name})...")
    dest = dump_dir / f"{svc}_mongo.archive.gz"
    ok = docker_stack.stream_raw([container, "mongodump", "--archive", "--gzip"], dest)
    if not ok:
        raise KedgeError(f"MongoDB dump for '{svc}' ({container_name}) failed")
    log.ok(f"MongoDB dump: {dest.name}")


def import_mongo(compose_cmd: list, restore_target: Path, dump_path: Path, svc: str) -> None:
    container = docker_stack.container_for_service(restore_target, compose_cmd, svc)
    if not container:
        log.warn(f"  Container for {svc} not running — skip dump import")
        return
    with open(dump_path, "rb") as f:
        result = subprocess.run(
            ["docker", "exec", "-i", container, "mongorestore", "--archive", "--gzip"],
            stdin=f, capture_output=True, text=True, check=False,
        )
    if result.returncode != 0:
        log.warn(f"MongoDB import reported errors: {result.stderr[-500:]}")
    log.ok("  MongoDB dump imported")


_MONGO_HEALTHCHECK = """            if docker exec "$CONTAINER" mongosh --eval 'db.runCommand({ping:1})' >/dev/null 2>&1; then
                echo "  PASS: MongoDB [$svc] responding"
            else
                echo "  FAIL: MongoDB [$svc] not responding"
                FAILURES=$((FAILURES + 1))
            fi
            DB_CHECKED=$((DB_CHECKED + 1))
            CHECKS=$((CHECKS + 1))
"""

MONGO = DBEngine(
    name="mongo",
    image_patterns=("mongo",),
    dump=dump_mongo,
    dump_suffix="_mongo.archive.gz",
    import_=import_mongo,
    healthcheck_patterns=("*mongo*",),
    healthcheck_bash=_MONGO_HEALTHCHECK,
)


# --- InfluxDB --------------------------------------------------------------
#
# Real fleet case: prod-multi01 runs influxdb 2.6 (cowork/servers.yaml,
# verified 2026-07-26) -- v2 is the primary, fully-supported target. v1 is
# only DETECTED (image tag major version) so a v1 image gets a clear "not
# supported" error at import time instead of a silent wrong command; v1 OSS
# restore fundamentally doesn't fit the "docker exec import while running"
# shape every other engine uses here (it needs the target stopped with a
# clean data dir first), so it isn't faked into that shape.

def _influx_is_v1(image: str) -> bool:
    tag = image.split(":", 1)[1] if ":" in image else "latest"
    major = tag.split(".", 1)[0]
    return major.isdigit() and major == "1"


def _influx_token(container: str) -> str:
    env = docker_stack.container_env(container)
    return docker_stack.first_env(env, "DOCKER_INFLUXDB_INIT_ADMIN_TOKEN", "INFLUX_TOKEN")


def dump_influxdb(svc: str, container: str, container_name: str, dump_dir: Path, image: str = "") -> None:
    log.info(f"Dumping InfluxDB ({container_name})...")
    remote_dir = f"/tmp/kedge-influx-backup-{svc}"
    subprocess.run(["docker", "exec", container, "rm", "-rf", remote_dir], capture_output=True, check=False)

    if _influx_is_v1(image):
        result = subprocess.run(
            ["docker", "exec", container, "influxd", "backup", "-portable", remote_dir],
            capture_output=True, text=True, check=False,
        )
    else:
        cmd = ["docker", "exec", container, "influx", "backup", remote_dir]
        token = _influx_token(container)
        if token:
            cmd += ["--token", token]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode != 0:
        raise KedgeError(f"InfluxDB backup for '{svc}' ({container_name}) failed: {result.stderr[-500:]}")

    with tempfile.TemporaryDirectory() as tmp:
        local_dir = Path(tmp) / "influx-backup"
        if not docker_stack.copy_from_container(container, remote_dir, local_dir):
            raise KedgeError(f"InfluxDB backup for '{svc}' ({container_name}): docker cp out of container failed")
        subprocess.run(["docker", "exec", container, "rm", "-rf", remote_dir], capture_output=True, check=False)

        dest = dump_dir / f"{svc}_influxdb.tar.gz"
        with tarfile.open(dest, "w:gz") as tar:
            for entry in sorted(local_dir.iterdir()):
                tar.add(entry, arcname=entry.name)
    log.ok(f"InfluxDB dump: {dest.name}")


def import_influxdb(compose_cmd: list, restore_target: Path, dump_path: Path, svc: str) -> None:
    container = docker_stack.container_for_service(restore_target, compose_cmd, svc)
    if not container:
        log.warn(f"  Container for {svc} not running — skip dump import")
        return

    image = subprocess.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if _influx_is_v1(image):
        raise KedgeError(
            f"InfluxDB v1 restore for '{svc}' ({image}) is not supported by kedge: v1 OSS restore "
            "needs the target stopped with a clean data directory first, unlike every other engine's "
            "docker-exec-while-running import. Restore manually: stop the container, place the "
            "-portable backup under the data dir, `influxd restore -portable <dir>`, restart."
        )

    remote_dir = f"/tmp/kedge-influx-restore-{svc}"
    subprocess.run(["docker", "exec", container, "rm", "-rf", remote_dir], capture_output=True, check=False)

    with tempfile.TemporaryDirectory() as tmp:
        local_dir = Path(tmp) / "influx-restore"
        local_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(dump_path, "r:gz") as tar:
            tar.extractall(local_dir, filter="data")
        if not docker_stack.copy_to_container(container, local_dir, remote_dir):
            raise KedgeError(f"InfluxDB restore for '{svc}': docker cp into container failed")

    token = _influx_token(container)
    cmd = ["docker", "exec", container, "influx", "restore", remote_dir, "--full"]
    if token:
        cmd += ["--token", token]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    subprocess.run(["docker", "exec", container, "rm", "-rf", remote_dir], capture_output=True, check=False)
    if result.returncode != 0:
        log.warn(f"InfluxDB import reported errors: {result.stderr[-500:]}")
    log.ok("  InfluxDB dump imported")


_INFLUXDB_HEALTHCHECK = """            if docker exec "$CONTAINER" sh -c 'command -v influx >/dev/null 2>&1 && influx ping 2>/dev/null || influxd version >/dev/null 2>&1'; then
                echo "  PASS: InfluxDB [$svc] responding"
            else
                echo "  FAIL: InfluxDB [$svc] not responding"
                FAILURES=$((FAILURES + 1))
            fi
            DB_CHECKED=$((DB_CHECKED + 1))
            CHECKS=$((CHECKS + 1))
"""

INFLUXDB = DBEngine(
    name="influxdb",
    image_patterns=("influxdb",),
    dump=dump_influxdb,
    dump_suffix="_influxdb.tar.gz",
    import_=import_influxdb,
    healthcheck_patterns=("*influxdb*",),
    healthcheck_bash=_INFLUXDB_HEALTHCHECK,
)


# --- SQLite ------------------------------------------------------------------
#
# Real fleet case: prod-poki (/var/poki/db, WAL mode, bind-mount). SQLite is
# an embedded library inside poki's own app image, not a container of its
# own -- there is no image to auto-discover a "sqlite" service from, so this
# engine deliberately has no image_patterns and never participates in
# discovery.detect_db_type()/hooks.run_pre_hooks()'s per-service loop. It has
# no dump/import in the usual sense either: the bind-mount itself already
# gets tarred by collect.collect_stack_files() like any other external mount.
# What it needs is a WAL checkpoint BEFORE that tar happens, so the plain
# .db file alone is a consistent, standalone snapshot (no dependency on
# capturing the -wal/-shm sidecar files atomically) -- wired in via the
# SQLITE_WAL_CHECKPOINT_PATHS config (config.py/commands.py), not
# auto-discovery.

def checkpoint_wal(db_path: Path) -> bool:
    """PRAGMA wal_checkpoint(TRUNCATE) against a real local SQLite file.
    Returns True if the checkpoint fully completed (WAL merged + truncated to
    empty), False if another connection held a lock (busy) -- non-fatal: the
    -wal/-shm files still get backed up alongside the main file in that case,
    just not merged in."""
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        busy, log_frames, checkpointed = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    return busy == 0 and log_frames == checkpointed


def checkpoint_wal_paths(paths: list[str]) -> None:
    for raw in paths:
        path = Path(raw)
        if not path.is_file():
            log.warn(f"SQLITE_WAL_CHECKPOINT_PATHS entry not found, skipping: {path}")
            continue
        try:
            ok = checkpoint_wal(path)
        except Exception as exc:  # sqlite3.Error et al. -- misconfigured/non-sqlite path
            log.warn(f"SQLite WAL checkpoint failed for {path}: {exc}")
            continue
        if ok:
            log.ok(f"SQLite WAL checkpoint: {path.name}")
        else:
            log.warn(
                f"SQLite WAL checkpoint for {path.name} was busy (another writer holds a lock) — "
                f"-wal/-shm sidecar files will be backed up alongside the main file instead"
            )


SQLITE = DBEngine(name="sqlite")


# --- Registry ------------------------------------------------------------------

ENGINES: tuple[DBEngine, ...] = (POSTGRES, MYSQL, VALKEY, MONGO, INFLUXDB, SQLITE)


def engine_for_image(image: str) -> DBEngine | None:
    """discovery.detect_db_type()'s replacement: first engine whose
    image_patterns matches, tag stripped. Mirrors the original shell case
    statement -- order doesn't matter, first match wins per category."""
    image_name = image.split(":", 1)[0]
    for engine in ENGINES:
        if engine.image_patterns and any(p in image_name for p in engine.image_patterns):
            return engine
    return None


def engine_by_name(name: str) -> DBEngine | None:
    for engine in ENGINES:
        if engine.name == name:
            return engine
    return None


def build_healthcheck_case_block() -> str:
    """Assembles the `case "$IMAGE" in ... esac` body for
    verify.py's _HEALTHCHECK_SCRIPT from every engine that defines one."""
    return "".join(engine.case_arm() for engine in ENGINES if engine.healthcheck_patterns)
