#!/usr/bin/env bash
# =============================================================================
# test.sh — Full roundtrip backup/restore test on Hetzner Cloud
#
# 1. Creates Box A, deploys a sample Docker Compose stack with test data
# 2. Runs backup.sh on Box A
# 3. Creates Box B (fresh VPS)
# 4. Copies restic repo from A to B
# 5. Runs restore.sh on Box B
# 6. Verifies: checksums, container status, data integrity
# 7. Burns both boxes
#
# Usage:
#   test.sh                     Full roundtrip test
#   test.sh --keep              Don't burn boxes after test (for debugging)
#   test.sh --burn              Burn any leftover test boxes
#
# Environment:
#   HCLOUD_CONTEXT         hcloud CLI context to use (default: kigulls-test)
#   HCLOUD_TOKEN           Alternative: API token directly (overrides context)
#   TEST_SERVER_TYPE       Server type (default: cpx23)
#   TEST_LOCATION          Datacenter (default: nbg1)
# =============================================================================

set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HCLOUD_TOKEN="${HCLOUD_TOKEN:-}"
HCLOUD_CONTEXT="${HCLOUD_CONTEXT:-kigulls-test}"
TEST_SERVER_TYPE="${TEST_SERVER_TYPE:-cpx23}"
TEST_LOCATION="${TEST_LOCATION:-nbg1}"
TEST_IMAGE="ubuntu-24.04"
SSH_KEY_NAME="${SSH_KEY_NAME:-stephan@waldtmann.de}"
KEEP_BOXES=false

# Test prefix for easy cleanup
TEST_PREFIX="kedge-test"
BOX_A_NAME="${TEST_PREFIX}-a-$(date +%H%M)"
BOX_B_NAME="${TEST_PREFIX}-b-$(date +%H%M)"
BOX_A_IP=""
BOX_B_IP=""

BACKUP_PASSWORD="test-backup-$(openssl rand -hex 8)"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log()  { echo "[$(date '+%H:%M:%S')] $1  $2" >&2; }
info()  { _log "==>" "$*"; }
ok()    { _log " ok" "$*"; }
warn()  { _log "wrn" "$*"; }
err()   { _log "ERR" "$*"; }
die()   { err "$1"; exit 1; }

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

check_prereqs() {
    for cmd in hcloud ssh scp; do
        command -v "$cmd" >/dev/null 2>&1 || die "Required: $cmd"
    done

    if [[ -n "$HCLOUD_TOKEN" ]]; then
        export HCLOUD_TOKEN
    else
        # Use hcloud CLI context
        local prev_context
        prev_context="$(hcloud context active 2>/dev/null || true)"
        if [[ "$prev_context" != "$HCLOUD_CONTEXT" ]]; then
            info "Switching hcloud context: $prev_context -> $HCLOUD_CONTEXT"
            hcloud context use "$HCLOUD_CONTEXT" || die "hcloud context '$HCLOUD_CONTEXT' not found"
        fi
        # Verify context works
        hcloud server list >/dev/null 2>&1 || die "hcloud context '$HCLOUD_CONTEXT' not working — check token"
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
        [[ $elapsed -ge $max ]] && die "SSH timeout on $ip after ${max}s"
    done
    ok "SSH ready ($ip, ${elapsed}s)"
}

# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

create_box() {
    local name="$1"

    # Fallback: try multiple type/location combinations (DE only)
    local types=("$TEST_SERVER_TYPE" cpx23 cpx21 cax11)
    local locations=("$TEST_LOCATION" nbg1 fsn1)
    local created=false

    for stype in "${types[@]}"; do
        for loc in "${locations[@]}"; do
            info "Trying $name ($stype, $loc)..."
            if hcloud server create \
                --name "$name" \
                --type "$stype" \
                --image "$TEST_IMAGE" \
                --location "$loc" \
                --ssh-key "$SSH_KEY_NAME" \
                --label "purpose=kedge-test" >/dev/null 2>&1; then
                created=true
                ok "Server $name created ($stype, $loc)"
                break 2
            fi
            # Clean up failed attempt
            hcloud server delete "$name" 2>/dev/null || true
        done
    done

    if ! $created; then
        die "Could not create server $name — all type/location combinations failed"
    fi

    local ip
    ip="$(hcloud server list -o columns=name,ipv4 | grep "^${name} " | awk '{print $2}')"
    if [[ -z "$ip" ]]; then
        die "Could not get IP for $name"
    fi
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
    info "Bootstrapping $ip (Docker + restic + jq)..."
    ssh_box "$ip" bash -s <<'BOOTSTRAP'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 restic jq rsync python3-venv >/dev/null 2>&1
systemctl enable --now docker
BOOTSTRAP
    ok "Bootstrap complete ($ip)"
}

# ---------------------------------------------------------------------------
# mockforge (synthetic Nextcloud-volume content, AFKI-W-237)
# ---------------------------------------------------------------------------

MOCKFORGE_DIR="${MOCKFORGE_DIR:-$SCRIPT_DIR/../mockforge}"

deploy_mockforge() {
    local ip="$1"
    [[ -d "$MOCKFORGE_DIR" ]] || die "mockforge repo not found at $MOCKFORGE_DIR (set MOCKFORGE_DIR)"
    info "Installing mockforge on $ip..."

    tar -C "$MOCKFORGE_DIR" -czf /tmp/mockforge-src.tgz --exclude=.venv --exclude=.git .
    scp $SSH_OPTS /tmp/mockforge-src.tgz "root@$ip:/tmp/mockforge-src.tgz"
    rm -f /tmp/mockforge-src.tgz

    ssh_box "$ip" bash -s <<'MOCKFORGE_INSTALL'
set -euo pipefail
mkdir -p /opt/mockforge
tar -C /opt/mockforge -xzf /tmp/mockforge-src.tgz
rm -f /tmp/mockforge-src.tgz
python3 -m venv /opt/mockforge/.venv
/opt/mockforge/.venv/bin/pip install -q -e /opt/mockforge
MOCKFORGE_INSTALL

    ok "mockforge installed ($ip)"
}

# ---------------------------------------------------------------------------
# Sample stack (for testing)
# ---------------------------------------------------------------------------

deploy_sample_stack() {
    local ip="$1"
    info "Deploying sample stack on $ip..."

    ssh_box "$ip" bash -s <<'SAMPLE'
set -euo pipefail
mkdir -p /opt/test-stack/nginx-html /opt/test-stack/custom-config

# Create a docker-compose.yml with multiple service types
cat > /opt/test-stack/docker-compose.yml <<'COMPOSE'
services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: testuser
      POSTGRES_PASSWORD: testpass
      POSTGRES_DB: testdb
    volumes:
      - pg_data:/var/lib/postgresql/data

  valkey:
    image: valkey/valkey:8-alpine
    command: valkey-server --appendonly yes --requirepass testvalkey
    volumes:
      - valkey_data:/data

  nginx:
    image: nginx:1-alpine
    ports:
      - "8080:80"
    volumes:
      - ./nginx-html:/usr/share/nginx/html:ro
      - ./custom-config:/etc/nginx/conf.d:ro

  # Holds nextcloud_data alive -- content is a mockforge-generated file tree,
  # written directly to the volume's host path (KEDGE-W-007 regression case:
  # docker-volume backup via literal host path, not a bind mount).
  storage:
    image: busybox:stable
    command: ["sleep", "infinity"]
    volumes:
      - nextcloud_data:/data

volumes:
  pg_data:
  valkey_data:
  nextcloud_data:
COMPOSE

# Create .env
cat > /opt/test-stack/.env <<'DOTENV'
COMPOSE_PROJECT_NAME=teststack
TEST_SECRET=super-secret-value-12345
DOTENV

# Nginx custom config
cat > /opt/test-stack/custom-config/default.conf <<'NGINX'
server {
    listen 80;
    location / {
        root /usr/share/nginx/html;
        index index.html;
    }
}
NGINX

# Test data for nginx
echo "<h1>Kedge Test Page</h1><p>Backup test data: $(date)</p>" \
    > /opt/test-stack/nginx-html/index.html

# Start stack
cd /opt/test-stack
docker compose up -d

# Wait for services
sleep 10

# Seed Postgres with test data
docker exec teststack-postgres-1 psql -U testuser -d testdb -c "
    CREATE TABLE IF NOT EXISTS backup_test (
        id SERIAL PRIMARY KEY,
        data TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW()
    );
    INSERT INTO backup_test (data) VALUES
        ('row-alpha-$(date +%s)'),
        ('row-beta-verification'),
        ('row-gamma-integrity-check');
"

# Seed Valkey with test data
docker exec teststack-valkey-1 valkey-cli -a testvalkey SET backup:test:key "valkey-test-value-$(date +%s)"
docker exec teststack-valkey-1 valkey-cli -a testvalkey SET backup:test:verify "restore-should-match"
docker exec teststack-valkey-1 valkey-cli -a testvalkey BGSAVE
sleep 2

echo "=== Container status ==="
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
SAMPLE

    ok "Sample stack deployed and seeded ($ip)"
}

seed_nextcloud_volume() {
    local ip="$1"
    info "Seeding nextcloud_data volume with mockforge content ($ip)..."

    ssh_box "$ip" bash -s <<'SEED'
set -euo pipefail
MOUNTPOINT="$(docker volume inspect teststack_nextcloud_data --format '{{ .Mountpoint }}')"
/opt/mockforge/.venv/bin/mockforge nextcloud --out "$MOUNTPOINT" --files 300 --seed 1
echo "=== nextcloud_data volume seeded ($MOUNTPOINT) ==="
find "$MOUNTPOINT" -type f | wc -l
SEED

    ok "nextcloud_data volume seeded ($ip)"
}

# ---------------------------------------------------------------------------
# Capture checksums for verification
# ---------------------------------------------------------------------------

capture_checksums() {
    local ip="$1"
    info "Capturing verification data from $ip..."

    ssh_box "$ip" bash -s <<'CHECKSUMS'
set -euo pipefail
mkdir -p /tmp/kedge-verify

# Postgres row count + data
docker exec teststack-postgres-1 psql -U testuser -d testdb -t -c \
    "SELECT data FROM backup_test ORDER BY id" > /tmp/kedge-verify/pg-data.txt

# Valkey keys
docker exec teststack-valkey-1 valkey-cli -a testvalkey GET backup:test:verify \
    > /tmp/kedge-verify/valkey-verify.txt 2>/dev/null

# Nginx page
curl -s http://localhost:8080/ > /tmp/kedge-verify/nginx-page.txt

# .env content
cat /opt/test-stack/.env > /tmp/kedge-verify/env-content.txt

# nextcloud_data volume -- content hash + file count (KEDGE-W-007 regression case)
NC_MOUNT="$(docker volume inspect teststack_nextcloud_data --format '{{ .Mountpoint }}')"
find "$NC_MOUNT" -type f -exec sha256sum {} \; | awk '{print $1}' | sort | sha256sum | awk '{print $1}' \
    > /tmp/kedge-verify/nextcloud-hash.txt
find "$NC_MOUNT" -type f | wc -l > /tmp/kedge-verify/nextcloud-count.txt

echo "=== Verification data captured ==="
cat /tmp/kedge-verify/pg-data.txt
cat /tmp/kedge-verify/valkey-verify.txt
cat /tmp/kedge-verify/nextcloud-count.txt
CHECKSUMS
}

# ---------------------------------------------------------------------------
# Run backup on Box A
# ---------------------------------------------------------------------------

run_backup() {
    local ip="$1"
    info "Running backup on $ip..."

    # Upload backup.sh
    scp $SSH_OPTS "$SCRIPT_DIR/backup.sh" "root@$ip:/usr/local/bin/kedge-backup"
    ssh_box "$ip" "chmod +x /usr/local/bin/kedge-backup"

    ssh_box "$ip" bash -s -- "$BACKUP_PASSWORD" <<'RUNBACKUP'
set -euo pipefail
export STACK_DIR=/opt/test-stack
export RESTIC_REPOSITORY=/backup/test-repo
export RESTIC_PASSWORD="$1"
# KEDGE-W-007 regression guard: a broad SYSTEM_PATHS_EXCLUDE entry that would
# have shadowed the nextcloud_data docker-volume backup before the fix.
export SYSTEM_PATHS="/var/log"
export SYSTEM_PATHS_EXCLUDE="/var/lib/docker/volumes"

# Init repo
kedge-backup init

# Discovery (dry-run log)
kedge-backup discover

# Full backup
kedge-backup backup

# List snapshots
kedge-backup list

echo "=== Backup complete ==="
ls -lah /backup/test-repo/
RUNBACKUP

    ok "Backup completed on $ip"
}

# ---------------------------------------------------------------------------
# Transfer backup repo from A to B
# ---------------------------------------------------------------------------

transfer_backup() {
    local src_ip="$1"
    local dst_ip="$2"
    info "Transferring restic repo from $src_ip to $dst_ip..."

    # Generate a temporary SSH key on box A to connect to box B
    ssh_box "$src_ip" bash -s -- "$dst_ip" <<'TRANSFER'
set -euo pipefail
DST_IP="$1"

# Use rsync to copy the backup repo
rsync -az -e "ssh -o StrictHostKeyChecking=accept-new" \
    /backup/test-repo/ "root@${DST_IP}:/backup/test-repo/"

echo "=== Transfer complete ==="
TRANSFER

    ok "Backup repo transferred to $dst_ip"
}

# ---------------------------------------------------------------------------
# Run restore on Box B
# ---------------------------------------------------------------------------

run_restore() {
    local ip="$1"
    info "Running restore on $ip..."

    # Upload restore.sh
    scp $SSH_OPTS "$SCRIPT_DIR/restore.sh" "root@$ip:/usr/local/bin/kedge-restore"
    ssh_box "$ip" "chmod +x /usr/local/bin/kedge-restore"

    ssh_box "$ip" bash -s -- "$BACKUP_PASSWORD" <<'RUNRESTORE'
set -euo pipefail
export RESTIC_REPOSITORY=/backup/test-repo
export RESTIC_PASSWORD="$1"
export RESTORE_TARGET=/opt/test-stack

# List snapshots
kedge-restore --list

# Restore latest
kedge-restore latest

echo "=== Restore complete ==="
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
RUNRESTORE

    ok "Restore completed on $ip"
}

# ---------------------------------------------------------------------------
# Verify restore
# ---------------------------------------------------------------------------

verify_restore() {
    local src_ip="$1"
    local dst_ip="$2"
    info "Verifying restore on $dst_ip against $src_ip..."

    local failures=0

    # Capture verification data on box B
    ssh_box "$dst_ip" bash -s <<'VERIFY_CAPTURE'
set -euo pipefail
mkdir -p /tmp/kedge-verify

# Wait for services to be fully ready
sleep 15

# Postgres
docker exec teststack-postgres-1 psql -U testuser -d testdb -t -c \
    "SELECT data FROM backup_test ORDER BY id" > /tmp/kedge-verify/pg-data.txt 2>/dev/null || echo "POSTGRES_FAIL" > /tmp/kedge-verify/pg-data.txt

# Valkey
docker exec teststack-valkey-1 valkey-cli -a testvalkey GET backup:test:verify \
    > /tmp/kedge-verify/valkey-verify.txt 2>/dev/null || echo "VALKEY_FAIL" > /tmp/kedge-verify/valkey-verify.txt

# Nginx page
curl -s http://localhost:8080/ > /tmp/kedge-verify/nginx-page.txt 2>/dev/null || echo "NGINX_FAIL" > /tmp/kedge-verify/nginx-page.txt

# .env
cat /opt/test-stack/.env > /tmp/kedge-verify/env-content.txt 2>/dev/null || echo "ENV_FAIL" > /tmp/kedge-verify/env-content.txt

# nextcloud_data volume -- content hash + file count (KEDGE-W-007 regression case)
NC_MOUNT="$(docker volume inspect teststack_nextcloud_data --format '{{ .Mountpoint }}' 2>/dev/null || true)"
if [[ -n "$NC_MOUNT" ]]; then
    find "$NC_MOUNT" -type f -exec sha256sum {} \; | awk '{print $1}' | sort | sha256sum | awk '{print $1}' \
        > /tmp/kedge-verify/nextcloud-hash.txt 2>/dev/null || echo "NEXTCLOUD_HASH_FAIL" > /tmp/kedge-verify/nextcloud-hash.txt
    find "$NC_MOUNT" -type f | wc -l > /tmp/kedge-verify/nextcloud-count.txt 2>/dev/null || echo "0" > /tmp/kedge-verify/nextcloud-count.txt
else
    echo "NEXTCLOUD_VOLUME_MISSING" > /tmp/kedge-verify/nextcloud-hash.txt
    echo "0" > /tmp/kedge-verify/nextcloud-count.txt
fi
VERIFY_CAPTURE

    # Compare: Postgres data
    local src_pg dst_pg
    src_pg="$(ssh_box "$src_ip" cat /tmp/kedge-verify/pg-data.txt)"
    dst_pg="$(ssh_box "$dst_ip" cat /tmp/kedge-verify/pg-data.txt)"
    if [[ "$src_pg" == "$dst_pg" ]]; then
        ok "PASS: PostgreSQL data matches"
    else
        err "FAIL: PostgreSQL data mismatch"
        echo "  Source: $src_pg"
        echo "  Restore: $dst_pg"
        failures=$((failures + 1))
    fi

    # Compare: Valkey data
    local src_vk dst_vk
    src_vk="$(ssh_box "$src_ip" cat /tmp/kedge-verify/valkey-verify.txt)"
    dst_vk="$(ssh_box "$dst_ip" cat /tmp/kedge-verify/valkey-verify.txt)"
    if [[ "$src_vk" == "$dst_vk" ]]; then
        ok "PASS: Valkey data matches"
    else
        err "FAIL: Valkey data mismatch"
        echo "  Source: $src_vk"
        echo "  Restore: $dst_vk"
        failures=$((failures + 1))
    fi

    # Compare: .env
    local src_env dst_env
    src_env="$(ssh_box "$src_ip" cat /tmp/kedge-verify/env-content.txt)"
    dst_env="$(ssh_box "$dst_ip" cat /tmp/kedge-verify/env-content.txt)"
    if [[ "$src_env" == "$dst_env" ]]; then
        ok "PASS: .env matches"
    else
        err "FAIL: .env mismatch"
        failures=$((failures + 1))
    fi

    # Check: Nginx responds
    local dst_nginx
    dst_nginx="$(ssh_box "$dst_ip" cat /tmp/kedge-verify/nginx-page.txt)"
    if echo "$dst_nginx" | grep -q "Kedge Test Page"; then
        ok "PASS: Nginx serves restored page"
    else
        err "FAIL: Nginx page not restored"
        failures=$((failures + 1))
    fi

    # Compare: nextcloud_data volume (KEDGE-W-007 regression case -- docker
    # volume backup via literal host path, guarded by SYSTEM_PATHS_EXCLUDE
    # overreach protection)
    local src_nc_hash dst_nc_hash src_nc_count dst_nc_count
    src_nc_hash="$(ssh_box "$src_ip" cat /tmp/kedge-verify/nextcloud-hash.txt)"
    dst_nc_hash="$(ssh_box "$dst_ip" cat /tmp/kedge-verify/nextcloud-hash.txt)"
    src_nc_count="$(ssh_box "$src_ip" cat /tmp/kedge-verify/nextcloud-count.txt)"
    dst_nc_count="$(ssh_box "$dst_ip" cat /tmp/kedge-verify/nextcloud-count.txt)"
    if [[ "$src_nc_hash" == "$dst_nc_hash" && "$src_nc_count" == "$dst_nc_count" && "$dst_nc_count" != "0" ]]; then
        ok "PASS: nextcloud_data volume matches ($dst_nc_count files)"
    else
        err "FAIL: nextcloud_data volume mismatch (src: $src_nc_count files/$src_nc_hash, dst: $dst_nc_count files/$dst_nc_hash)"
        failures=$((failures + 1))
    fi

    # Check: All containers running
    local running_count
    running_count="$(ssh_box "$dst_ip" 'docker ps --format "{{.Names}}" | wc -l')"
    if [[ "$running_count" -ge 4 ]]; then
        ok "PASS: All containers running ($running_count)"
    else
        err "FAIL: Only $running_count containers running (expected 4+)"
        failures=$((failures + 1))
    fi

    echo ""
    if [[ $failures -eq 0 ]]; then
        ok "=== ALL VERIFICATIONS PASSED ==="
        return 0
    else
        err "=== $failures VERIFICATION(S) FAILED ==="
        return 1
    fi
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

burn_all_test_boxes() {
    info "Burning all $TEST_PREFIX boxes..."
    local servers
    servers="$(hcloud server list -o columns=name | grep "^${TEST_PREFIX}-" || true)"
    for name in $servers; do
        burn_box "$name"
    done
    ok "Cleanup complete"
}

cleanup_on_exit() {
    local exit_code=$?
    if [[ "$KEEP_BOXES" == "true" ]]; then
        warn "Boxes kept alive (--keep): $BOX_A_NAME ($BOX_A_IP), $BOX_B_NAME ($BOX_B_IP)"
        warn "Clean up later: $0 --burn"
    else
        burn_box "$BOX_A_NAME" 2>/dev/null || true
        burn_box "$BOX_B_NAME" 2>/dev/null || true
    fi
    exit $exit_code
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

cmd_test() {
    check_prereqs
    trap cleanup_on_exit EXIT

    local total_start
    total_start="$(date +%s)"

    info "========================================="
    info "  kedge — Roundtrip Test"
    info "========================================="
    echo ""

    # Step 1: Create Box A
    info "--- Step 1: Create Box A ---"
    BOX_A_IP="$(create_box "$BOX_A_NAME")"
    wait_for_ssh "$BOX_A_IP"
    bootstrap_box "$BOX_A_IP"

    # Step 2: Deploy sample stack + seed data
    info "--- Step 2: Deploy sample stack ---"
    deploy_sample_stack "$BOX_A_IP"
    deploy_mockforge "$BOX_A_IP"
    seed_nextcloud_volume "$BOX_A_IP"
    capture_checksums "$BOX_A_IP"

    # Step 3: Run backup
    info "--- Step 3: Backup ---"
    run_backup "$BOX_A_IP"

    # Step 4: Create Box B
    info "--- Step 4: Create Box B ---"
    BOX_B_IP="$(create_box "$BOX_B_NAME")"
    wait_for_ssh "$BOX_B_IP"
    bootstrap_box "$BOX_B_IP"

    # Step 5: Transfer backup
    info "--- Step 5: Transfer backup repo ---"
    # Box A needs to ssh into Box B — add temporary SSH access
    ssh_box "$BOX_A_IP" "ssh-keygen -t ed25519 -f /root/.ssh/transfer_key -N '' -q"
    local pubkey
    pubkey="$(ssh_box "$BOX_A_IP" cat /root/.ssh/transfer_key.pub)"
    ssh_box "$BOX_B_IP" "echo '$pubkey' >> /root/.ssh/authorized_keys"
    ssh_box "$BOX_A_IP" bash -c "cat > /root/.ssh/config <<SSHCFG
Host restore-target
    HostName $BOX_B_IP
    User root
    IdentityFile /root/.ssh/transfer_key
    StrictHostKeyChecking accept-new
SSHCFG"

    # Create target directory on Box B
    ssh_box "$BOX_B_IP" "mkdir -p /backup/test-repo"

    ssh_box "$BOX_A_IP" bash -s -- "$BOX_B_IP" <<'XFER'
set -euo pipefail
DST="$1"
rsync -az -e "ssh -i /root/.ssh/transfer_key -o StrictHostKeyChecking=accept-new" \
    /backup/test-repo/ "root@${DST}:/backup/test-repo/"
echo "Transfer done"
XFER
    ok "Backup repo transferred"

    # Step 6: Restore on Box B
    info "--- Step 6: Restore ---"
    run_restore "$BOX_B_IP"

    # Step 7: Verify
    info "--- Step 7: Verify ---"
    local verify_ok=true
    verify_restore "$BOX_A_IP" "$BOX_B_IP" || verify_ok=false

    local total_end total_duration
    total_end="$(date +%s)"
    total_duration="$((total_end - total_start))"

    echo ""
    info "========================================="
    if $verify_ok; then
        ok "  ROUNDTRIP TEST PASSED (${total_duration}s)"
    else
        err "  ROUNDTRIP TEST FAILED (${total_duration}s)"
    fi
    info "  Box A: $BOX_A_NAME ($BOX_A_IP)"
    info "  Box B: $BOX_B_NAME ($BOX_B_IP)"
    info "========================================="

    $verify_ok || exit 1
}

usage() {
    cat <<EOF
kedge test — Full roundtrip backup/restore test on Hetzner Cloud

Usage: $(basename "$0") [options]

Options:
  --keep    Don't burn boxes after test (for debugging)
  --burn    Burn all leftover test boxes
  --help    Show this help

Environment:
  HCLOUD_TOKEN         Hetzner Cloud API token (required)
  TEST_SERVER_TYPE     Server type (default: cx22)
  TEST_LOCATION        Datacenter (default: nbg1)
  SSH_KEY_NAME         SSH key name in hcloud (default: stephan@waldtmann.de)
  MOCKFORGE_DIR        Path to the mockforge repo (default: ../mockforge, sibling of kedge)

What it does:
  1. Creates Box A (Hetzner), deploys a sample stack (Postgres + Valkey + Nginx +
     a nextcloud_data docker volume seeded via mockforge -- KEDGE-W-007 regression case)
  2. Seeds test data into all services
  3. Runs backup.sh (SYSTEM_PATHS_EXCLUDE=/var/lib/docker/volumes, the exact
     prod-bug pattern) → local restic repo
  4. Creates Box B (fresh VPS)
  5. Transfers restic repo from A to B
  6. Runs restore.sh on B
  7. Verifies: DB rows, Valkey keys, Nginx page, .env, nextcloud_data volume
     (file count + content hash), container count
  8. Burns both boxes
EOF
}

main() {
    case "${1:-}" in
        --keep)     KEEP_BOXES=true; cmd_test ;;
        --burn)     check_prereqs; burn_all_test_boxes ;;
        --help|-h)  usage ;;
        *)          cmd_test ;;
    esac
}

# Nur ausfuehren, wenn direkt aufgerufen -- beim Sourcen (bats-Tests) still bleiben.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
