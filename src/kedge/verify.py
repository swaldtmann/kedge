"""Verify — port of verify.sh (cmd_verify/cmd_burn). Restores the latest (or
given) snapshot onto a fresh, ephemeral Hetzner Cloud box and runs health
checks — proves a snapshot is bootable. The box is burned afterwards
regardless of outcome (unless --keep).

The health-check step itself stays a remote bash script piped over ssh
(verify.sh:239-396, unchanged) rather than being re-implemented in Python —
it inspects the *target* box's Docker/compose state, which is what
verify.sh already does correctly; re-porting it would just be the same
logic twice.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass

from kedge import log
from kedge.config import Config
from kedge.errors import KedgeError
from kedge.lifecycle_hooks import run_hook

VERIFY_IMAGE = "ubuntu-24.04"
VERIFY_PREFIX = "kedge-verify"
DEFAULT_RESTORE_TARGET = "/opt/stack"

SSH_OPTS = ["-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]

_BOOTSTRAP_SCRIPT = """set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 restic jq rsync >/dev/null 2>&1
systemctl enable --now docker
"""

_HEALTHCHECK_SCRIPT = r"""set -euo pipefail
STACK_DIR="$1"
cd "$STACK_DIR"

FAILURES=0
CHECKS=0
CONTAINERS_TOTAL=0
CONTAINERS_RUNNING=0

if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    COMPOSE="docker-compose"
fi

echo "--- Health Checks ---"

echo ""
echo "CHECK: Container status"
EXPECTED=$($COMPOSE config --format json 2>/dev/null | jq -r '.services | keys[]' | wc -l)
RUNNING=$($COMPOSE ps --format json 2>/dev/null | jq -s '[.[] | select(.State == "running")] | length')
CONTAINERS_TOTAL=$EXPECTED
CONTAINERS_RUNNING=$RUNNING

if [ "$RUNNING" -ge "$EXPECTED" ]; then
    echo "  PASS: $RUNNING/$EXPECTED containers running"
else
    echo "  FAIL: $RUNNING/$EXPECTED containers running"
    echo "  Non-running:"
    $COMPOSE ps --format json 2>/dev/null | jq -r 'select(.State != "running") | "    \(.Name): \(.State)"'
    FAILURES=$((FAILURES + 1))
fi
CHECKS=$((CHECKS + 1))

echo ""
echo "CHECK: Database connectivity"
CONFIG=$($COMPOSE config --format json 2>/dev/null)
DB_CHECKED=0

for svc in $(echo "$CONFIG" | jq -r '.services | keys[]'); do
    IMAGE=$(echo "$CONFIG" | jq -r --arg s "$svc" '.services[$s].image // "build"')
    CONTAINER=$($COMPOSE ps -q "$svc" 2>/dev/null | head -1)
    [ -z "$CONTAINER" ] && continue

    case "$IMAGE" in
        *postgres*|*postgis*)
            PG_USER=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
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
            ;;
        *mariadb*|*mysql*)
            ROOT_PASS=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
                | grep -E '^(MYSQL|MARIADB)_ROOT_PASSWORD=' | head -1 | cut -d= -f2)
            if docker exec "$CONTAINER" mysqladmin ping -uroot "-p${ROOT_PASS}" >/dev/null 2>&1; then
                echo "  PASS: MySQL/MariaDB [$svc] accepting connections"
            else
                echo "  FAIL: MySQL/MariaDB [$svc] not responding"
                FAILURES=$((FAILURES + 1))
            fi
            DB_CHECKED=$((DB_CHECKED + 1))
            CHECKS=$((CHECKS + 1))
            ;;
        *valkey*|*redis*)
            if docker exec "$CONTAINER" sh -c 'command -v valkey-cli >/dev/null && valkey-cli PING 2>/dev/null || redis-cli PING 2>/dev/null' | grep -qi pong; then
                echo "  PASS: Valkey/Redis [$svc] responding to PING"
            else
                PASS=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
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
            ;;
        *mongo*)
            if docker exec "$CONTAINER" mongosh --eval 'db.runCommand({ping:1})' >/dev/null 2>&1; then
                echo "  PASS: MongoDB [$svc] responding"
            else
                echo "  FAIL: MongoDB [$svc] not responding"
                FAILURES=$((FAILURES + 1))
            fi
            DB_CHECKED=$((DB_CHECKED + 1))
            CHECKS=$((CHECKS + 1))
            ;;
    esac
done

if [ "$DB_CHECKED" -eq 0 ]; then
    echo "  -- no database containers found --"
fi

echo ""
echo "CHECK: HTTP endpoints"
HTTP_CHECKED=0
for svc in $(echo "$CONFIG" | jq -r '.services | keys[]'); do
    PORTS=$(echo "$CONFIG" | jq -r --arg s "$svc" '
        .services[$s].ports // [] | .[] |
        if type == "object" then .published else split(":")[0] end
    ' 2>/dev/null)

    for port in $PORTS; do
        [ -z "$port" ] && continue
        HTTP_CHECKED=$((HTTP_CHECKED + 1))
        CHECKS=$((CHECKS + 1))
        STATUS=$(curl -sf -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:${port}/" 2>/dev/null || echo "000")
        if [ "$STATUS" -ge 200 ] && [ "$STATUS" -lt 500 ]; then
            echo "  PASS: $svc :${port} -> HTTP $STATUS"
        else
            echo "  WARN: $svc :${port} -> HTTP $STATUS [may be expected for non-HTTP services]"
        fi
    done
done

if [ "$HTTP_CHECKED" -eq 0 ]; then
    echo "  -- no exposed ports found --"
fi

echo ""
echo "CHECK: Configuration files"
CHECKS=$((CHECKS + 1))
if [ -f "$STACK_DIR/.env" ]; then
    echo "  PASS: .env present"
else
    echo "  FAIL: .env missing"
    FAILURES=$((FAILURES + 1))
fi

echo ""
echo "--- Summary ---"
echo "CHECKS=$CHECKS"
echo "FAILURES=$FAILURES"
echo "CONTAINERS=$CONTAINERS_RUNNING/$CONTAINERS_TOTAL"
"""


@dataclass
class VerifyConfig:
    hcloud_context: str = "kigulls-test"
    hcloud_token: str = ""
    server_type: str = "cpx23"
    location: str = "nbg1"
    ssh_key_name: str = "stephan@waldtmann.de"
    restore_target: str = DEFAULT_RESTORE_TARGET
    post_hook: str = ""
    fail_hook: str = ""

    @classmethod
    def from_env(cls) -> "VerifyConfig":
        return cls(
            hcloud_context=os.environ.get("HCLOUD_CONTEXT") or "kigulls-test",
            hcloud_token=os.environ.get("HCLOUD_TOKEN", ""),
            server_type=os.environ.get("VERIFY_SERVER_TYPE") or "cpx23",
            location=os.environ.get("VERIFY_LOCATION") or "nbg1",
            ssh_key_name=os.environ.get("SSH_KEY_NAME") or "stephan@waldtmann.de",
            restore_target=os.environ.get("RESTORE_TARGET") or DEFAULT_RESTORE_TARGET,
            post_hook=os.environ.get("VERIFY_POST_HOOK", ""),
            fail_hook=os.environ.get("VERIFY_FAIL_HOOK", ""),
        )


@dataclass
class VerifyContext:
    hostname: str = ""
    stack: str = ""
    timestamp: str = ""
    duration: str = ""
    snapshot: str = ""
    containers: str = ""
    error: str = ""

    def as_env(self) -> dict[str, str]:
        return {
            "VERIFY_HOSTNAME": self.hostname,
            "VERIFY_STACK": self.stack,
            "VERIFY_TIMESTAMP": self.timestamp,
            "VERIFY_DURATION": self.duration,
            "VERIFY_SNAPSHOT": self.snapshot,
            "VERIFY_CONTAINERS": self.containers,
            "VERIFY_ERROR": self.error,
        }


def _hcloud_env(vcfg: VerifyConfig) -> dict:
    env = os.environ.copy()
    if vcfg.hcloud_token:
        env["HCLOUD_TOKEN"] = vcfg.hcloud_token
    return env


def check_verify_prereqs(cfg: Config, vcfg: VerifyConfig) -> None:
    missing = [tool for tool in ("hcloud", "ssh", "scp", "restic") if shutil.which(tool) is None]
    if missing:
        raise KedgeError(f"Required: {' '.join(missing)}")
    if not cfg.restic_repository:
        raise KedgeError("RESTIC_REPOSITORY not set")
    if not cfg.restic_password and not cfg.restic_password_file:
        raise KedgeError("RESTIC_PASSWORD or RESTIC_PASSWORD_FILE not set")

    if not vcfg.hcloud_token:
        env = _hcloud_env(vcfg)
        prev = subprocess.run(["hcloud", "context", "active"], capture_output=True, text=True, check=False).stdout.strip()
        if prev != vcfg.hcloud_context:
            if subprocess.run(["hcloud", "context", "use", vcfg.hcloud_context], env=env, check=False).returncode != 0:
                raise KedgeError(f"hcloud context '{vcfg.hcloud_context}' not found")
        if subprocess.run(["hcloud", "server", "list"], env=env, capture_output=True, check=False).returncode != 0:
            raise KedgeError("hcloud context not working")


def resolve_snapshot(cfg: Config, snapshot_id: str) -> str:
    if snapshot_id != "latest":
        return snapshot_id
    from kedge import restic

    short_id = restic.latest_snapshot_short_id(cfg)
    if short_id == "unknown":
        raise KedgeError("No snapshots found in repository")
    return short_id


def create_box(vcfg: VerifyConfig, name: str) -> str:
    """verify.sh:174-206 — try server type/location combos in order,
    de-duplicated, first successful create wins."""
    env = _hcloud_env(vcfg)
    types = list(dict.fromkeys([vcfg.server_type, "cpx23", "cpx21", "cax11"]))
    locations = list(dict.fromkeys([vcfg.location, "nbg1", "fsn1"]))

    created = False
    for stype in types:
        for loc in locations:
            result = subprocess.run(
                [
                    "hcloud", "server", "create", "--name", name, "--type", stype,
                    "--image", VERIFY_IMAGE, "--location", loc, "--ssh-key", vcfg.ssh_key_name,
                    "--label", "purpose=kedge-verify",
                ],
                env=env, capture_output=True, check=False,
            )
            if result.returncode == 0:
                created = True
                break
            subprocess.run(["hcloud", "server", "delete", name], env=env, capture_output=True, check=False)
        if created:
            break

    if not created:
        raise KedgeError("Could not create server — all type/location combinations failed")

    result = subprocess.run(
        ["hcloud", "server", "list", "-o", "columns=name,ipv4"],
        env=env, capture_output=True, text=True, check=False,
    )
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == name:
            return parts[1]
    raise KedgeError(f"Could not get IP for {name}")


def burn_box(vcfg: VerifyConfig, name: str) -> None:
    env = _hcloud_env(vcfg)
    result = subprocess.run(
        ["hcloud", "server", "list", "-o", "columns=name"], env=env, capture_output=True, text=True, check=False,
    )
    if any(line.strip() == name for line in result.stdout.splitlines()):
        log.info(f"Burning {name}...")
        subprocess.run(["hcloud", "server", "delete", name], env=env, check=False)
        log.ok(f"{name} deleted")


def wait_for_ssh(ip: str, max_wait: int = 120, interval: int = 5) -> None:
    subprocess.run(["ssh-keygen", "-R", ip], capture_output=True, check=False)
    log.info(f"Waiting for SSH on {ip}...")
    elapsed = 0
    while subprocess.run(["ssh", *SSH_OPTS, f"root@{ip}", "true"], capture_output=True, check=False).returncode != 0:
        time.sleep(interval)
        elapsed += interval
        if elapsed >= max_wait:
            raise KedgeError(f"SSH timeout after {max_wait}s")
    log.ok(f"SSH ready ({ip}, {elapsed}s)")


def bootstrap_box(ip: str) -> None:
    log.info(f"Bootstrapping {ip}...")
    result = subprocess.run(
        ["ssh", *SSH_OPTS, f"root@{ip}", "bash", "-s"], input=_BOOTSTRAP_SCRIPT, text=True, check=False,
    )
    if result.returncode != 0:
        raise KedgeError(f"Bootstrap failed on {ip}")
    log.ok(f"Bootstrap complete ({ip})")


def run_health_checks(ip: str, restore_target: str) -> tuple[bool, str]:
    """Returns (all_passed, "running/total" containers string)."""
    log.info("Running health checks...")
    result = subprocess.run(
        ["ssh", *SSH_OPTS, f"root@{ip}", "bash", "-s", "--", restore_target],
        input=_HEALTHCHECK_SCRIPT, text=True, capture_output=True, check=False,
    )
    print(result.stdout, end="")

    failures = "1"
    containers = "0/0"
    for line in result.stdout.splitlines():
        if line.startswith("FAILURES="):
            failures = line.split("=", 1)[1]
        elif line.startswith("CONTAINERS="):
            containers = line.split("=", 1)[1]

    return failures == "0", containers


def cmd_verify(cfg: Config, vcfg: VerifyConfig, snapshot_id: str = "latest", keep_box: bool = False) -> bool:
    check_verify_prereqs(cfg, vcfg)
    resolved_snapshot = resolve_snapshot(cfg, snapshot_id)
    log.ok(f"Checking snapshot: {resolved_snapshot}")

    box_name = f"{VERIFY_PREFIX}-{time.strftime('%H%M')}"
    start_time = time.monotonic()
    box_ip = ""
    verify_ok = False
    containers = ""

    log.info("=========================================")
    log.info("  Restore Verification")
    log.info(f"  Snapshot: {resolved_snapshot}")
    log.info(f"  Repository: {cfg.restic_repository}")
    log.info("=========================================")

    try:
        log.info("--- Step 1: Create verify box ---")
        box_ip = create_box(vcfg, box_name)
        wait_for_ssh(box_ip)
        bootstrap_box(box_ip)

        log.info("--- Step 2: Restore snapshot ---")
        password = cfg.restic_password or (
            open(cfg.restic_password_file).read() if cfg.restic_password_file else ""
        )
        remote_env_script = (
            f'export RESTIC_REPOSITORY="{cfg.restic_repository}"\n'
            f'export RESTIC_PASSWORD="{password}"\n'
            f'export RESTORE_TARGET="{vcfg.restore_target}"\n'
            f'kedge restore "{resolved_snapshot}"\n'
        )
        subprocess.run(
            ["ssh", *SSH_OPTS, f"root@{box_ip}", "bash", "-s"],
            input=remote_env_script, text=True, check=True,
        )

        log.info("Waiting for services to settle...")
        time.sleep(20)

        log.info("--- Step 3: Health checks ---")
        verify_ok, containers = run_health_checks(box_ip, vcfg.restore_target)
    finally:
        if keep_box and box_ip:
            log.warn(f"Box kept alive (--keep): {box_name} ({box_ip})")
            log.warn(f"Clean up: kedge burn")
        elif box_name:
            burn_box(vcfg, box_name)

        duration = int(time.monotonic() - start_time)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        ctx = VerifyContext(
            timestamp=timestamp, duration=str(duration), snapshot=resolved_snapshot, containers=containers,
        )
        if verify_ok:
            run_hook(vcfg.post_hook, "verify-post-hook", ctx)
        else:
            ctx.error = "Restore verification failed"
            run_hook(vcfg.fail_hook, "verify-fail-hook", ctx)

    log.info("=========================================")
    if verify_ok:
        log.ok(f"  RESTORE VERIFIED ({duration}s)")
    else:
        log.err(f"  RESTORE VERIFICATION FAILED ({duration}s)")
    log.info(f"  Snapshot: {resolved_snapshot}")
    log.info(f"  Containers: {containers}")
    log.info("=========================================")
    return verify_ok


def cmd_burn(vcfg: VerifyConfig) -> None:
    env = _hcloud_env(vcfg)
    if not vcfg.hcloud_token:
        subprocess.run(["hcloud", "context", "use", vcfg.hcloud_context], env=env, check=False)

    log.info(f"Burning all {VERIFY_PREFIX} boxes...")
    result = subprocess.run(
        ["hcloud", "server", "list", "-o", "columns=name"], env=env, capture_output=True, text=True, check=False,
    )
    for line in result.stdout.splitlines():
        name = line.strip()
        if name.startswith(f"{VERIFY_PREFIX}-"):
            burn_box(vcfg, name)
    log.ok("Cleanup complete")
