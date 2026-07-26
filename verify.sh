#!/usr/bin/env bash
# =============================================================================
# verify.sh — Automated restore verification on ephemeral Hetzner Cloud box
#
# Proves that a backup snapshot is bootable: restores on a fresh VPS, checks
# that all containers start and services respond. No data comparison — just
# structural integrity ("can this snapshot produce a running stack?").
#
# Designed to run monthly via cron. Exit 0 = verified, exit 1 = broken.
#
# Usage:
#   verify.sh                              Verify latest snapshot
#   verify.sh <snapshot-id>                Verify specific snapshot
#   verify.sh --keep                       Don't burn box after (debugging)
#   verify.sh --burn                       Burn leftover verify boxes
#
# Environment (required):
#   RESTIC_REPOSITORY     Where to pull the backup from
#   RESTIC_PASSWORD       Restic encryption password
#   RESTIC_PASSWORD_FILE  Alternative: file containing the password
#
# Environment (optional):
#   HCLOUD_CONTEXT        hcloud CLI context (default: kigulls-test)
#   HCLOUD_TOKEN          Alternative: API token directly
#   VERIFY_SERVER_TYPE    Server type (default: cpx23)
#   VERIFY_LOCATION       Datacenter (default: nbg1)
#   SSH_KEY_NAME          SSH key in hcloud (default: stephan@waldtmann.de)
#   RESTORE_TARGET        Where to restore on the box (default: /opt/stack)
#   VERIFY_POST_HOOK      Command after successful verify
#   VERIFY_FAIL_HOOK      Command after failed verify
# =============================================================================

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
HCLOUD_TOKEN="${HCLOUD_TOKEN:-}"
HCLOUD_CONTEXT="${HCLOUD_CONTEXT:-kigulls-test}"
VERIFY_SERVER_TYPE="${VERIFY_SERVER_TYPE:-cpx23}"
VERIFY_LOCATION="${VERIFY_LOCATION:-nbg1}"
VERIFY_IMAGE="ubuntu-24.04"
SSH_KEY_NAME="${SSH_KEY_NAME:-stephan@waldtmann.de}"
RESTORE_TARGET="${RESTORE_TARGET:-/opt/stack}"
VERIFY_POST_HOOK="${VERIFY_POST_HOOK:-}"
VERIFY_FAIL_HOOK="${VERIFY_FAIL_HOOK:-}"
KEEP_BOX=false

SNAPSHOT_ID="${1:-latest}"
VERIFY_PREFIX="kedge-verify"
BOX_NAME="${VERIFY_PREFIX}-$(date +%H%M)"
BOX_IP=""

# Verify context (exported for hooks)
VERIFY_HOSTNAME=""
VERIFY_STACK=""
VERIFY_TIMESTAMP=""
VERIFY_DURATION=""
VERIFY_SNAPSHOT=""
VERIFY_CONTAINERS=""
VERIFY_ERROR=""

# ---------------------------------------------------------------------------
# Logging (all to stderr so cron captures it)
# ---------------------------------------------------------------------------

_log()  { echo "[$(date '+%H:%M:%S')] $1  $2" >&2; }
info()  { _log "==>" "$*"; }
ok()    { _log " ok" "$*"; }
warn()  { _log "wrn" "$*"; }
err()   { _log "ERR" "$*"; }
die()   { err "$1"; exit 1; }

# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

run_hook() {
    local hook_cmd="$1"
    local hook_name="$2"
    if [[ -z "$hook_cmd" ]]; then return 0; fi

    export VERIFY_HOSTNAME VERIFY_STACK VERIFY_TIMESTAMP VERIFY_DURATION
    export VERIFY_SNAPSHOT VERIFY_CONTAINERS VERIFY_ERROR

    info "Running $hook_name..."
    if eval "$hook_cmd"; then
        ok "$hook_name completed"
    else
        warn "$hook_name failed (exit $?) — continuing"
    fi
}

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

check_prereqs() {
    for cmd in hcloud ssh scp restic; do
        command -v "$cmd" >/dev/null 2>&1 || die "Required: $cmd"
    done

    if [[ -z "$RESTIC_REPOSITORY" ]]; then
        die "RESTIC_REPOSITORY not set"
    fi
    if [[ -z "$RESTIC_PASSWORD" && -z "${RESTIC_PASSWORD_FILE:-}" ]]; then
        die "RESTIC_PASSWORD or RESTIC_PASSWORD_FILE not set"
    fi

    export RESTIC_REPOSITORY RESTIC_PASSWORD
    if [[ -n "${RESTIC_PASSWORD_FILE:-}" ]]; then
        export RESTIC_PASSWORD_FILE
    fi

    # hcloud auth
    if [[ -n "$HCLOUD_TOKEN" ]]; then
        export HCLOUD_TOKEN
    else
        local prev
        prev="$(hcloud context active 2>/dev/null || true)"
        if [[ "$prev" != "$HCLOUD_CONTEXT" ]]; then
            hcloud context use "$HCLOUD_CONTEXT" || die "hcloud context '$HCLOUD_CONTEXT' not found"
        fi
        hcloud server list >/dev/null 2>&1 || die "hcloud context not working"
    fi

    # Verify snapshot exists
    info "Checking snapshot: $SNAPSHOT_ID"
    if [[ "$SNAPSHOT_ID" == "latest" ]]; then
        local snap_check
        snap_check="$(restic snapshots --latest 1 --json 2>/dev/null | jq -r '.[0].short_id // empty' 2>/dev/null || true)"
        if [[ -z "$snap_check" ]]; then
            die "No snapshots found in repository"
        fi
        VERIFY_SNAPSHOT="$snap_check"
        ok "Latest snapshot: $VERIFY_SNAPSHOT"
    else
        VERIFY_SNAPSHOT="$SNAPSHOT_ID"
    fi
}

# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

source "$SCRIPT_DIR/lib/ssh.sh"

wait_for_ssh() {
    local ip="$1" max=120 elapsed=0
    ssh-keygen -R "$ip" 2>/dev/null || true
    info "Waiting for SSH on $ip..."
    while ! ssh $SSH_OPTS "root@$ip" true 2>/dev/null; do
        sleep 5
        elapsed=$((elapsed + 5))
        if [[ $elapsed -ge $max ]]; then die "SSH timeout after ${max}s"; fi
    done
    ok "SSH ready ($ip, ${elapsed}s)"
}

# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

create_box() {
    local name="$1"
    local types=("$VERIFY_SERVER_TYPE" cpx23 cpx21 cax11)
    local locations=("$VERIFY_LOCATION" nbg1 fsn1)
    local created=false

    for stype in "${types[@]}"; do
        for loc in "${locations[@]}"; do
            info "Trying $name ($stype, $loc)..."
            if hcloud server create \
                --name "$name" \
                --type "$stype" \
                --image "$VERIFY_IMAGE" \
                --location "$loc" \
                --ssh-key "$SSH_KEY_NAME" \
                --label "purpose=kedge-verify" >/dev/null 2>&1; then
                created=true
                ok "Server $name created ($stype, $loc)"
                break 2
            fi
            hcloud server delete "$name" 2>/dev/null || true
        done
    done

    if ! $created; then
        die "Could not create server — all type/location combinations failed"
    fi

    local ip
    ip="$(hcloud server list -o columns=name,ipv4 | grep "^${name} " | awk '{print $2}')"
    if [[ -z "$ip" ]]; then die "Could not get IP for $name"; fi
    echo "$ip"
}

burn_box() {
    local name="$1"
    if hcloud server list -o columns=name | grep -q "^${name}$"; then
        info "Burning $name..."
        hcloud server delete "$name"
        ok "$name deleted"
    fi
}

bootstrap_box() {
    local ip="$1"
    info "Bootstrapping $ip..."
    ssh_box "$ip" bash -s <<'BOOTSTRAP'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 restic jq rsync >/dev/null 2>&1
systemctl enable --now docker
BOOTSTRAP
    ok "Bootstrap complete ($ip)"
}

# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

run_health_checks() {
    local ip="$1"
    info "Running health checks..."

    local result
    result="$(ssh_box "$ip" bash -s -- "$RESTORE_TARGET" <<'HEALTHCHECK'
set -euo pipefail
STACK_DIR="$1"
cd "$STACK_DIR"

FAILURES=0
CHECKS=0
CONTAINERS_TOTAL=0
CONTAINERS_RUNNING=0

# Detect compose command
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
else
    COMPOSE="docker-compose"
fi

echo "--- Health Checks ---"

# Check 1: All services defined in compose are running
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

# Check 2: Database containers accept connections
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
                # Try with auth from env
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

# Check 3: Services with exposed ports respond to HTTP
echo ""
echo "CHECK: HTTP endpoints"
HTTP_CHECKED=0
for svc in $(echo "$CONFIG" | jq -r '.services | keys[]'); do
    # Check for port mappings
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
            echo "  PASS: $svc :${port} → HTTP $STATUS"
        else
            echo "  WARN: $svc :${port} → HTTP $STATUS [may be expected for non-HTTP services]"
        fi
    done
done

if [ "$HTTP_CHECKED" -eq 0 ]; then
    echo "  -- no exposed ports found --"
fi

# Check 4: .env exists
echo ""
echo "CHECK: Configuration files"
CHECKS=$((CHECKS + 1))
if [ -f "$STACK_DIR/.env" ]; then
    echo "  PASS: .env present"
else
    echo "  FAIL: .env missing"
    FAILURES=$((FAILURES + 1))
fi

# Summary
echo ""
echo "--- Summary ---"
echo "CHECKS=$CHECKS"
echo "FAILURES=$FAILURES"
echo "CONTAINERS=$CONTAINERS_RUNNING/$CONTAINERS_TOTAL"
HEALTHCHECK
)"

    echo "$result" >&2

    # Parse summary
    local failures containers
    failures="$(echo "$result" | grep '^FAILURES=' | cut -d= -f2)"
    containers="$(echo "$result" | grep '^CONTAINERS=' | cut -d= -f2)"
    VERIFY_CONTAINERS="$containers"

    if [[ "$failures" == "0" ]]; then
        return 0
    else
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

cleanup_on_exit() {
    local exit_code=$?
    if [[ "$KEEP_BOX" == "true" && -n "$BOX_IP" ]]; then
        warn "Box kept alive (--keep): $BOX_NAME ($BOX_IP)"
        warn "Clean up: $0 --burn"
    else
        burn_box "$BOX_NAME" 2>/dev/null || true
    fi

    VERIFY_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if [[ $exit_code -eq 0 ]]; then
        run_hook "$VERIFY_POST_HOOK" "verify-post-hook" || true
    else
        VERIFY_ERROR="Restore verification failed"
        run_hook "$VERIFY_FAIL_HOOK" "verify-fail-hook" || true
    fi

    exit $exit_code
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_verify() {
    check_prereqs
    trap cleanup_on_exit EXIT

    local start_time
    start_time="$(date +%s)"

    info "========================================="
    info "  Restore Verification"
    info "  Snapshot: $VERIFY_SNAPSHOT"
    info "  Repository: $RESTIC_REPOSITORY"
    info "========================================="

    # Step 1: Create box
    info "--- Step 1: Create verify box ---"
    BOX_IP="$(create_box "$BOX_NAME")"
    wait_for_ssh "$BOX_IP"
    bootstrap_box "$BOX_IP"

    # Step 2: Upload restore script + restic credentials
    info "--- Step 2: Upload restore script ---"
    scp $SSH_OPTS "$SCRIPT_DIR/restore.sh" "root@$BOX_IP:/usr/local/bin/kedge-restore"
    ssh_box "$BOX_IP" "chmod +x /usr/local/bin/kedge-restore"

    # For local restic repos: won't work remotely. For sftp/s3: works directly.
    # For local repos in tests: copy the repo to the box first.
    if [[ "$RESTIC_REPOSITORY" == /* ]]; then
        info "Local repo detected — copying to verify box..."
        ssh_box "$BOX_IP" "mkdir -p $(dirname "$RESTIC_REPOSITORY")"
        rsync -az -e "ssh $SSH_OPTS" \
            "$RESTIC_REPOSITORY/" "root@$BOX_IP:$RESTIC_REPOSITORY/"
    fi

    # Step 3: Restore
    info "--- Step 3: Restore snapshot ---"
    local restic_pass_arg=""
    if [[ -n "$RESTIC_PASSWORD" ]]; then
        restic_pass_arg="$RESTIC_PASSWORD"
    elif [[ -n "${RESTIC_PASSWORD_FILE:-}" ]]; then
        restic_pass_arg="$(cat "$RESTIC_PASSWORD_FILE")"
    fi

    ssh_box "$BOX_IP" bash -s -- "$RESTIC_REPOSITORY" "$restic_pass_arg" "$RESTORE_TARGET" "$SNAPSHOT_ID" <<'RESTORE'
set -euo pipefail
export RESTIC_REPOSITORY="$1"
export RESTIC_PASSWORD="$2"
export RESTORE_TARGET="$3"
SNAP="$4"

kedge-restore "$SNAP"
RESTORE

    # Step 4: Wait for services to settle
    info "Waiting for services to settle..."
    sleep 20

    # Step 5: Health checks
    info "--- Step 4: Health checks ---"
    local verify_ok=true
    if ! run_health_checks "$BOX_IP"; then
        verify_ok=false
    fi

    local end_time
    end_time="$(date +%s)"
    VERIFY_DURATION="$((end_time - start_time))"
    VERIFY_HOSTNAME="$(hostname -f 2>/dev/null || hostname)"

    # Try to get stack name from meta.json on the box
    VERIFY_STACK="$(ssh_box "$BOX_IP" "jq -r '.stack_dir // empty' $RESTORE_TARGET/meta.json 2>/dev/null | xargs basename 2>/dev/null" || echo "unknown")"

    echo "" >&2
    info "========================================="
    if $verify_ok; then
        ok "  RESTORE VERIFIED (${VERIFY_DURATION}s)"
        info "  Snapshot: $VERIFY_SNAPSHOT"
        info "  Containers: $VERIFY_CONTAINERS"
        info "========================================="
        return 0
    else
        err "  RESTORE VERIFICATION FAILED (${VERIFY_DURATION}s)"
        info "  Snapshot: $VERIFY_SNAPSHOT"
        info "  Containers: $VERIFY_CONTAINERS"
        info "========================================="
        return 1
    fi
}

cmd_burn() {
    if [[ -n "$HCLOUD_TOKEN" ]]; then
        export HCLOUD_TOKEN
    else
        hcloud context use "$HCLOUD_CONTEXT" 2>/dev/null || true
    fi

    info "Burning all $VERIFY_PREFIX boxes..."
    local servers
    servers="$(hcloud server list -o columns=name | grep "^${VERIFY_PREFIX}-" || true)"
    for name in $servers; do
        burn_box "$name"
    done
    ok "Cleanup complete"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
kedge verify — Automated restore verification

Restores the latest (or specified) backup snapshot on a fresh Hetzner Cloud
VPS and runs health checks. Proves the backup is bootable.

Usage: $(basename "$0") [options] [snapshot-id]

Options:
  --keep    Don't burn box after verification (for debugging)
  --burn    Burn leftover verify boxes
  --help    Show this help

Arguments:
  snapshot-id  Restic snapshot to verify (default: latest)

Environment (required):
  RESTIC_REPOSITORY      Restic repository
  RESTIC_PASSWORD        Encryption password
  RESTIC_PASSWORD_FILE   Alternative: path to password file

Environment (optional):
  HCLOUD_CONTEXT         hcloud CLI context (default: kigulls-test)
  VERIFY_SERVER_TYPE     Server type (default: cpx23)
  VERIFY_LOCATION        Datacenter (default: nbg1)
  RESTORE_TARGET         Where to restore (default: /opt/stack)
  VERIFY_POST_HOOK       Command after successful verify
  VERIFY_FAIL_HOOK       Command after failed verify

Health checks performed:
  - All containers from docker-compose.yml are running
  - Database containers accept connections (PostgreSQL, MySQL, Valkey, MongoDB)
  - Services with exposed ports respond to HTTP
  - .env configuration file is present

Cron example (monthly):
  0 5 1 * * root . /etc/kedge-backup.env && /usr/local/bin/kedge-verify latest >> /var/log/kedge-verify.log 2>&1

Exit codes:
  0  All health checks passed
  1  One or more checks failed
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    case "${1:-}" in
        --keep)
            KEEP_BOX=true
            shift
            SNAPSHOT_ID="${1:-latest}"
            cmd_verify
            ;;
        --burn)
            cmd_burn
            ;;
        --help|-h|help)
            usage
            ;;
        *)
            if [[ "${1:-}" == --* ]]; then
                err "Unknown option: $1"
                usage
                exit 1
            fi
            SNAPSHOT_ID="${1:-latest}"
            cmd_verify
            ;;
    esac
}

# Nur ausfuehren, wenn direkt aufgerufen -- beim Sourcen (bats-Tests) still bleiben.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
