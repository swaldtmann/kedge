#!/usr/bin/env bash
# =============================================================================
# backup.sh — Generic encrypted backup for any Docker Compose stack
#
# Auto-discovers: named volumes, bind mounts, databases (by image).
# Pre-hooks: pg_dumpall, mysqldump, valkey/redis BGSAVE, mongodump.
# Encryption + dedup via restic.
#
# Usage:
#   backup.sh backup [--stack-dir /path]   Full backup
#   backup.sh init                         Initialize restic repository
#   backup.sh list                         List snapshots
#   backup.sh snapshots                    Alias for list
#   backup.sh restore [snapshot-id]        Restore (delegates to restore.sh)
#   backup.sh check                        Verify repository integrity
#   backup.sh prune                        Remove old snapshots per retention
#   backup.sh discover                     Dry-run: show what would be backed up
#
# Environment:
#   STACK_DIR             Docker Compose stack directory (default: pwd)
#   RESTIC_REPOSITORY     restic repo path (local, sftp:, s3:, rest:)
#   RESTIC_PASSWORD       restic encryption password
#   RESTIC_PASSWORD_FILE  Alternative: file containing the password
#   BACKUP_STOP_STACK     Stop stack during backup (default: true)
#   BACKUP_KEEP_DAILY     Retention: daily snapshots to keep (default: 7)
#   BACKUP_KEEP_WEEKLY    Retention: weekly snapshots to keep (default: 4)
#   BACKUP_KEEP_MONTHLY   Retention: monthly snapshots to keep (default: 3)
#   BACKUP_EXCLUDE_VOLUMES  Space-separated volume names to skip
#   BACKUP_POST_HOOK      Command to run after successful backup (optional)
#   BACKUP_FAIL_HOOK      Command to run after failed backup (optional)
#   BACKUP_HEALTHCHECK_URL  URL to ping after backup (Healthchecks.io, Uptime Kuma, etc.)
#
# Hook variables (expanded in BACKUP_POST_HOOK / BACKUP_FAIL_HOOK):
#   $BACKUP_DURATION      Backup duration in seconds
#   $BACKUP_SIZE          Restic repo size (human-readable)
#   $BACKUP_SNAPSHOT      Snapshot ID
#   $BACKUP_HOSTNAME      Server hostname
#   $BACKUP_STACK         Stack directory basename
#   $BACKUP_TIMESTAMP     ISO 8601 UTC timestamp
#   $BACKUP_ERROR         Error message (BACKUP_FAIL_HOOK only)
# =============================================================================

set -euo pipefail

readonly VERSION="1.0.0"
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Config (overridable via env)
# ---------------------------------------------------------------------------

STACK_DIR="${STACK_DIR:-$(pwd)}"
RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
BACKUP_STOP_STACK="${BACKUP_STOP_STACK:-true}"
BACKUP_KEEP_DAILY="${BACKUP_KEEP_DAILY:-7}"
BACKUP_KEEP_WEEKLY="${BACKUP_KEEP_WEEKLY:-4}"
BACKUP_KEEP_MONTHLY="${BACKUP_KEEP_MONTHLY:-3}"
BACKUP_EXCLUDE_VOLUMES="${BACKUP_EXCLUDE_VOLUMES:-}"
BACKUP_POST_HOOK="${BACKUP_POST_HOOK:-}"
BACKUP_FAIL_HOOK="${BACKUP_FAIL_HOOK:-}"
BACKUP_HEALTHCHECK_URL="${BACKUP_HEALTHCHECK_URL:-}"

# Internal
STAGING_DIR=""
STACK_WAS_RUNNING=false
COMPOSE_CMD=""
# Hook context (populated during backup, exported for hooks)
BACKUP_DURATION=""
BACKUP_SIZE=""
BACKUP_SNAPSHOT=""
BACKUP_HOSTNAME=""
BACKUP_STACK=""
BACKUP_TIMESTAMP=""
BACKUP_ERROR=""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log()  { echo "[$(date '+%H:%M:%S')] $1  $2"; }
info()  { _log "==>" "$*"; }
ok()    { _log " ok" "$*"; }
warn()  { _log "wrn" "$*"; }
err()   { _log "ERR" "$*" >&2; }
die()   { err "$1"; exit 1; }

# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------

run_hook() {
    local hook_cmd="$1"
    local hook_name="$2"

    if [[ -z "$hook_cmd" ]]; then
        return 0
    fi

    info "Running $hook_name..."
    # Export context variables so the hook command can use them
    export BACKUP_DURATION BACKUP_SIZE BACKUP_SNAPSHOT BACKUP_HOSTNAME
    export BACKUP_STACK BACKUP_TIMESTAMP BACKUP_ERROR

    # Run hook via eval so variables in the command string get expanded
    if eval "$hook_cmd"; then
        ok "$hook_name completed"
    else
        warn "$hook_name failed (exit $?) — continuing"
    fi
}

# Ping a healthcheck URL with status + context in the body
ping_healthcheck() {
    local status="$1"  # "ok" or "fail"

    if [[ -z "$BACKUP_HEALTHCHECK_URL" ]]; then
        return 0
    fi

    local url="$BACKUP_HEALTHCHECK_URL"
    if [[ "$status" == "fail" ]]; then
        url="${url%/}/fail"
    fi

    local body
    body="host=$BACKUP_HOSTNAME stack=$BACKUP_STACK snapshot=$BACKUP_SNAPSHOT duration=${BACKUP_DURATION}s size=$BACKUP_SIZE"
    if [[ "$status" == "fail" && -n "$BACKUP_ERROR" ]]; then
        body="error: $BACKUP_ERROR | $body"
    fi

    # Silent, non-blocking, 10s timeout
    curl -sf --max-time 10 -X POST --data-raw "$body" "$url" >/dev/null 2>&1 || true
    ok "Healthcheck ping: $status"
}

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

check_prereqs() {
    local missing=()
    for cmd in docker jq restic; do
        command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        die "Missing required tools: ${missing[*]}"
    fi

    # Detect compose command
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD="docker-compose"
    else
        die "Neither 'docker compose' nor 'docker-compose' found"
    fi

    # Find compose file
    local found=false
    for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml; do
        if [[ -f "$STACK_DIR/$f" ]]; then
            found=true
            break
        fi
    done
    $found || die "No docker-compose file found in $STACK_DIR"

    # Restic config
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
}

# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

# Get full compose config as JSON
compose_config() {
    cd "$STACK_DIR"
    $COMPOSE_CMD config --format json 2>/dev/null
}

# Discover named volumes from compose config
discover_volumes() {
    local config="$1"
    echo "$config" | jq -r '
        .volumes // {} | keys[]
    ' 2>/dev/null || true
}

# Discover bind mounts from compose config (absolute paths only)
discover_bind_mounts() {
    local config="$1"
    echo "$config" | jq -r '
        [.services[].volumes[]? |
         select(type == "object") |
         select(.type == "bind") |
         .source] | unique | .[]
    ' 2>/dev/null || true
}

# Discover services with their images
discover_services() {
    local config="$1"
    echo "$config" | jq -r '
        .services | to_entries[] | "\(.key)\t\(.value.image // "build")"
    ' 2>/dev/null || true
}

# Map image name to DB type for pre-hooks
detect_db_type() {
    local image="$1"
    case "$image" in
        *postgres*|*postgis*)   echo "postgres" ;;
        *mariadb*|*mysql*)      echo "mysql" ;;
        *valkey*|*redis*)       echo "valkey" ;;
        *mongo*)                echo "mongo" ;;
        *)                      echo "" ;;
    esac
}

# Check if a service image is known to be crash-consistent (safe for hot backup
# without a pre-hook). These services either use append-only storage, have
# built-in WAL/journal recovery, or are stateless/read-only by nature.
is_hot_safe_image() {
    local image="$1"
    case "$image" in
        # Monitoring / metrics (append-only, crash-tolerant WAL)
        *prometheus*|*grafana*|*loki*|*alertmanager*|*victoriametrics*)
            return 0 ;;
        # Reverse proxies / routers (config-driven, no persistent state)
        *traefik*|*nginx*|*caddy*|*haproxy*)
            return 0 ;;
        # Auth / SSO (config-driven or embedded DB with WAL)
        *authelia*|*lldap*|*keycloak*|*dex*)
            return 0 ;;
        # Security (stateless agents or crash-tolerant)
        *crowdsec*)
            return 0 ;;
        # Read-later / bookmarks (SQLite with WAL)
        *readeck*|*wallabag*|*linkding*)
            return 0 ;;
        # MQTT brokers (QoS state rebuilt on restart)
        *mosquitto*|*emqx*|*vernemq*)
            return 0 ;;
        # Message queues with persistence (fsync/journal)
        *rabbitmq*|*nats*)
            return 0 ;;
        # Wikis / CMS (file-based or embedded DB with recovery)
        *xwiki*|*bookstack*|*wiki.js*)
            return 0 ;;
        # Mail (journal-based storage)
        *mailcow*|*dovecot*|*mailserver*|*stalwart*)
            return 0 ;;
        # Password managers (SQLite with WAL)
        *vaultwarden*|*bitwarden*)
            return 0 ;;
        # Misc tools (embedded DBs, crash-tolerant)
        *listmonk*|*n8n*|*gitea*|*forgejo*|*miniflux*|*freshrss*)
            return 0 ;;
        *)
            return 1 ;;
    esac
}

# Check all services for hot-backup safety. Returns count of unsafe services.
# Output: one warning line per unsafe service to stderr.
check_hot_safety() {
    local config="$1"
    local unsafe=0

    while IFS=$'\t' read -r svc image; do
        if [[ -z "$svc" ]]; then continue; fi
        local db_type
        db_type="$(detect_db_type "$image")"
        # Has a pre-hook → safe (dump runs while stack is up)
        if [[ -n "$db_type" ]]; then continue; fi
        # Known crash-consistent → safe
        if is_hot_safe_image "$image"; then continue; fi
        # Build images → can't classify
        if [[ "$image" == "build" ]]; then
            warn "Service '$svc' uses a build image — verify hot-backup safety manually"
            unsafe=$((unsafe + 1))
            continue
        fi
        warn "Service '$svc' ($image) has no pre-hook and is not known to be crash-consistent"
        unsafe=$((unsafe + 1))
    done < <(discover_services "$config")

    [[ $unsafe -eq 0 ]]
}

# Get actual Docker volume name (compose may prefix with project name)
resolve_volume_name() {
    local pattern="$1"
    docker volume ls --format '{{.Name}}' | grep -E "(^|[-_])${pattern}$" | head -1
}

# Get the host mountpoint for a Docker volume
resolve_volume_path() {
    local vol_name="$1"
    docker volume inspect --format '{{.Mountpoint}}' "$vol_name" 2>/dev/null
}

# Check if a volume should be excluded
is_excluded_volume() {
    local vol="$1"
    for excl in $BACKUP_EXCLUDE_VOLUMES; do
        if [[ "$vol" == "$excl" ]]; then
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Pre-backup hooks (database dumps)
# ---------------------------------------------------------------------------

run_pre_hooks() {
    local config="$1"
    local dump_dir="$2"
    mkdir -p "$dump_dir"

    local hooks_run=0

    while IFS=$'\t' read -r svc image; do
        if [[ -z "$svc" ]]; then continue; fi
        local db_type
        db_type="$(detect_db_type "$image")"
        if [[ -z "$db_type" ]]; then continue; fi

        # Find running container for this service
        local container
        container="$(cd "$STACK_DIR" && $COMPOSE_CMD ps -q "$svc" 2>/dev/null | head -1)"
        if [[ -z "$container" ]]; then
            warn "Service '$svc' ($db_type) not running — skipping dump"
            continue
        fi

        local container_name
        container_name="$(docker inspect --format '{{.Name}}' "$container" | sed 's|^/||')"

        case "$db_type" in
            postgres)
                info "Dumping PostgreSQL ($container_name)..."
                local pg_user
                pg_user="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
                    | grep '^POSTGRES_USER=' | cut -d= -f2 || true)"
                pg_user="${pg_user:-postgres}"
                docker exec "$container" pg_dumpall -U "$pg_user" \
                    | gzip > "$dump_dir/${svc}_postgres.sql.gz"
                ok "PostgreSQL dump: ${svc}_postgres.sql.gz ($(du -h "$dump_dir/${svc}_postgres.sql.gz" | cut -f1))"
                hooks_run=$((hooks_run + 1))
                ;;

            mysql)
                info "Dumping MySQL/MariaDB ($container_name)..."
                # Extract password from container env — pass via MYSQL_PWD env var (not CLI arg)
                local mysql_pass=""
                mysql_pass="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
                    | grep -E '^(MYSQL_ROOT_PASSWORD|MARIADB_ROOT_PASSWORD)=' | head -1 | cut -d= -f2 || true)"
                local mysql_exec_args=()
                if [[ -n "$mysql_pass" ]]; then
                    mysql_exec_args=(-e "MYSQL_PWD=$mysql_pass")
                fi
                docker exec "${mysql_exec_args[@]}" "$container" mysqldump --all-databases -uroot 2>/dev/null \
                    | gzip > "$dump_dir/${svc}_mysql.sql.gz"
                ok "MySQL dump: ${svc}_mysql.sql.gz ($(du -h "$dump_dir/${svc}_mysql.sql.gz" | cut -f1))"
                hooks_run=$((hooks_run + 1))
                ;;

            valkey)
                info "Triggering BGSAVE on $container_name..."
                # Extract password from container env or command args — never pass via CLI
                local vk_pass=""
                vk_pass="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
                    | grep -E '^(VALKEY_PASSWORD|REDIS_PASSWORD)=' | head -1 | cut -d= -f2 || true)"
                if [[ -z "$vk_pass" ]]; then
                    # Try extracting --requirepass from command args
                    vk_pass="$(docker inspect --format '{{json .Config.Cmd}}' "$container" \
                        | jq -r '.[]?' 2>/dev/null | grep -A1 '^--requirepass$' | tail -1 || true)"
                fi
                if [[ -z "$vk_pass" ]]; then
                    # Try inline --requirepass=VALUE
                    vk_pass="$(docker inspect --format '{{json .Config.Cmd}}' "$container" \
                        | jq -r '.[]?' 2>/dev/null | grep '^--requirepass=' | cut -d= -f2 || true)"
                fi

                # Use REDISCLI_AUTH env var via docker exec -e (not visible in ps)
                local vk_exec_args=()
                if [[ -n "$vk_pass" ]]; then
                    vk_exec_args=(-e "REDISCLI_AUTH=$vk_pass")
                fi

                # BGSAVE + wait for completion
                docker exec "${vk_exec_args[@]}" "$container" sh -c '
                    if command -v valkey-cli >/dev/null 2>&1; then CLI=valkey-cli; else CLI=redis-cli; fi
                    BEFORE=$($CLI LASTSAVE 2>/dev/null | tr -dc "0-9")
                    $CLI BGSAVE >/dev/null 2>&1
                    for i in $(seq 1 30); do
                        AFTER=$($CLI LASTSAVE 2>/dev/null | tr -dc "0-9")
                        if [ "$AFTER" != "$BEFORE" ] 2>/dev/null; then exit 0; fi
                        sleep 1
                    done
                ' 2>/dev/null || true
                ok "BGSAVE completed on $container_name"
                hooks_run=$((hooks_run + 1))
                ;;

            mongo)
                info "Dumping MongoDB ($container_name)..."
                docker exec "$container" mongodump --archive --gzip 2>/dev/null \
                    > "$dump_dir/${svc}_mongo.archive.gz"
                ok "MongoDB dump: ${svc}_mongo.archive.gz ($(du -h "$dump_dir/${svc}_mongo.archive.gz" | cut -f1))"
                hooks_run=$((hooks_run + 1))
                ;;
        esac
    done < <(discover_services "$config")

    if [[ $hooks_run -eq 0 ]]; then
        info "No database containers detected — skipping pre-hooks"
    else
        ok "$hooks_run database hook(s) completed"
    fi
}

# ---------------------------------------------------------------------------
# Volume collection
# ---------------------------------------------------------------------------

# Collected volume paths for restic (populated by collect_volumes)
VOLUME_BACKUP_PATHS=()

collect_volumes() {
    local config="$1"
    local vol_map_dir="$2"
    mkdir -p "$vol_map_dir"

    VOLUME_BACKUP_PATHS=()
    local count=0

    while IFS= read -r vol_name; do
        if [[ -z "$vol_name" ]]; then continue; fi
        if is_excluded_volume "$vol_name"; then
            info "Skipping excluded volume: $vol_name"
            continue
        fi

        # Resolve actual Docker volume name
        local real_vol
        real_vol="$(resolve_volume_name "$vol_name")"
        if [[ -z "$real_vol" ]]; then
            warn "Volume '$vol_name' not found in Docker — skipping"
            continue
        fi

        # Get host mountpoint
        local vol_path
        vol_path="$(resolve_volume_path "$real_vol")"
        if [[ -z "$vol_path" || ! -d "$vol_path" ]]; then
            warn "Volume '$real_vol' mountpoint not accessible — falling back to tar export"
            # Fallback: tar.gz export (e.g., non-local volume drivers)
            docker run --rm \
                -v "$real_vol":/data:ro \
                -v "$vol_map_dir":/backup \
                alpine tar czf "/backup/${vol_name}.tar.gz" -C /data . 2>/dev/null
            local size
            size="$(du -h "$vol_map_dir/${vol_name}.tar.gz" | cut -f1)"
            ok "  $real_vol -> ${vol_name}.tar.gz ($size) [tar fallback]"
        else
            # Direct path: restic will back up with block-level dedup
            VOLUME_BACKUP_PATHS+=("$vol_path")
            local size
            size="$(du -sh "$vol_path" 2>/dev/null | cut -f1)"
            ok "  $real_vol -> $vol_path ($size) [direct]"
        fi

        count=$((count + 1))
    done < <(discover_volumes "$config")

    ok "$count volume(s) collected"
}

# ---------------------------------------------------------------------------
# Stack file collection
# ---------------------------------------------------------------------------

collect_stack_files() {
    local config="$1"
    local target_dir="$2"
    mkdir -p "$target_dir"

    # Compose file(s)
    for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml \
             docker-compose.override.yml docker-compose.override.yaml; do
        if [[ -f "$STACK_DIR/$f" ]]; then
            cp "$STACK_DIR/$f" "$target_dir/"
        fi
    done

    # .env files
    for f in .env .env.local .env.production; do
        if [[ -f "$STACK_DIR/$f" ]]; then
            cp "$STACK_DIR/$f" "$target_dir/"
        fi
    done

    # Bind mounts within stack dir: copy entire stack dir (excluding volumes data)
    # Bind mounts outside stack dir: copy separately
    local external_mounts=()
    while IFS= read -r mount_src; do
        if [[ -z "$mount_src" ]]; then continue; fi
        # Resolve relative to stack dir
        local abs_mount
        if [[ "$mount_src" == /* ]]; then
            abs_mount="$mount_src"
        else
            abs_mount="$(cd "$STACK_DIR" && realpath -m "$mount_src" 2>/dev/null || echo "$STACK_DIR/$mount_src")"
        fi

        # Check if inside or outside stack dir
        case "$abs_mount" in
            "$STACK_DIR"/*)
                # Inside stack dir — will be captured with stack copy
                ;;
            *)
                external_mounts+=("$abs_mount")
                ;;
        esac
    done < <(discover_bind_mounts "$config")

    # Copy stack directory (configs, bind-mount data inside stack dir)
    # Exclude: .git, backups, __pycache__, node_modules, large build artifacts
    rsync -a --relative \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='node_modules' \
        --exclude='.venv' \
        --exclude='*.pyc' \
        "$STACK_DIR/./" "$target_dir/stack-dir/"

    # External bind mounts
    if [[ ${#external_mounts[@]} -gt 0 ]]; then
        mkdir -p "$target_dir/external-mounts"
        for mount in "${external_mounts[@]}"; do
            if [[ -S "$mount" ]]; then
                warn "Skipping socket: $mount"
            elif [[ -e "$mount" ]]; then
                local mount_name
                mount_name="$(echo "$mount" | tr '/' '_' | sed 's/^_//')"
                info "Backing up external bind mount: $mount"
                if [[ -d "$mount" ]]; then
                    tar czf "$target_dir/external-mounts/${mount_name}.tar.gz" -C "$(dirname "$mount")" "$(basename "$mount")"
                else
                    cp "$mount" "$target_dir/external-mounts/"
                fi
            else
                warn "External bind mount not found: $mount"
            fi
        done
    fi

    ok "Stack files collected"
}

# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

write_metadata() {
    local config="$1"
    local target="$2"

    local hostname_str
    hostname_str="$(hostname -f 2>/dev/null || hostname)"

    # Collect container list
    local containers
    containers="$(cd "$STACK_DIR" && $COMPOSE_CMD ps --format json 2>/dev/null \
        | jq -s '[.[] | {name: .Name, image: .Image, state: .State}]' 2>/dev/null || echo '[]')"

    # Collect volume mapping + path mapping
    local vol_map="{}"
    local vol_paths="{}"
    while IFS= read -r vol_name; do
        if [[ -z "$vol_name" ]]; then continue; fi
        local real_vol
        real_vol="$(resolve_volume_name "$vol_name")"
        if [[ -n "$real_vol" ]]; then
            vol_map="$(echo "$vol_map" | jq --arg k "$vol_name" --arg v "$real_vol" '. + {($k): $v}')"
            local vol_path
            vol_path="$(resolve_volume_path "$real_vol")"
            if [[ -n "$vol_path" ]]; then
                vol_paths="$(echo "$vol_paths" | jq --arg k "$vol_name" --arg v "$vol_path" '. + {($k): $v}')"
            fi
        fi
    done < <(discover_volumes "$config")

    cat > "$target/meta.json" <<METAEOF
{
    "version": "$VERSION",
    "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "hostname": "$hostname_str",
    "stack_dir": "$STACK_DIR",
    "compose_cmd": "$COMPOSE_CMD",
    "containers": $containers,
    "volume_mapping": $vol_map,
    "volume_paths": $vol_paths,
    "docker_version": "$(docker --version 2>/dev/null | head -1)"
}
METAEOF

    ok "Metadata written"
}

# ---------------------------------------------------------------------------
# Stack start/stop
# ---------------------------------------------------------------------------

stop_stack() {
    if [[ "$BACKUP_STOP_STACK" != "true" ]]; then
        info "BACKUP_STOP_STACK=false — stack stays running (backup may be inconsistent)"
        return 0
    fi

    cd "$STACK_DIR"
    if $COMPOSE_CMD ps -q 2>/dev/null | head -1 | grep -q .; then
        STACK_WAS_RUNNING=true
        info "Stopping stack for consistent backup..."
        $COMPOSE_CMD stop
        ok "Stack stopped"
    fi
}

start_stack() {
    if [[ "$STACK_WAS_RUNNING" == "true" ]]; then
        cd "$STACK_DIR"
        info "Restarting stack..."
        $COMPOSE_CMD start
        ok "Stack restarted"
    fi
}

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

cleanup() {
    local exit_code=$?
    start_stack
    if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
        rm -rf "$STAGING_DIR"
    fi
    if [[ $exit_code -ne 0 ]]; then
        err "Backup failed (exit code $exit_code)"
        BACKUP_ERROR="Backup failed with exit code $exit_code"
        BACKUP_HOSTNAME="$(hostname -f 2>/dev/null || hostname)"
        BACKUP_STACK="$(basename "$STACK_DIR")"
        BACKUP_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        run_hook "$BACKUP_FAIL_HOOK" "fail-hook" || true
        ping_healthcheck "fail" || true
    fi
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_init() {
    check_prereqs
    info "Initializing restic repository: $RESTIC_REPOSITORY"
    restic init
    ok "Repository initialized"
}

cmd_discover() {
    check_prereqs
    local config
    config="$(compose_config)"

    echo ""
    echo "=== Stack: $STACK_DIR ==="
    echo ""

    echo "--- Services ---"
    while IFS=$'\t' read -r svc image; do
        local db_type
        db_type="$(detect_db_type "$image")"
        local status_info=""
        if [[ -n "$db_type" ]]; then
            status_info=" [pre-hook: $db_type]"
        elif is_hot_safe_image "$image"; then
            status_info=" [hot-safe]"
        elif [[ "$image" == "build" ]]; then
            status_info=" [build — verify manually]"
        fi
        echo "  $svc  ($image)$status_info"
    done < <(discover_services "$config")

    echo ""
    echo "--- Hot Backup Safety ---"
    if check_hot_safety "$config"; then
        echo "  All services have pre-hooks or are known crash-consistent"
        echo "  BACKUP_STOP_STACK=false is safe for this stack"
    else
        echo "  Some services may not be safe for hot backup (see warnings above)"
        echo "  Review before setting BACKUP_STOP_STACK=false"
    fi

    echo ""
    echo "--- Named Volumes ---"
    while IFS= read -r vol; do
        local real
        real="$(resolve_volume_name "$vol")"
        local excl=""
        if is_excluded_volume "$vol"; then excl=" [EXCLUDED]"; fi
        local vol_info="${real:-NOT FOUND}"
        if [[ -n "$real" ]]; then
            local vpath
            vpath="$(resolve_volume_path "$real")"
            if [[ -n "$vpath" && -d "$vpath" ]]; then
                local vsize
                vsize="$(du -sh "$vpath" 2>/dev/null | cut -f1)"
                vol_info="$real ($vpath, $vsize) [direct]"
            else
                vol_info="$real [tar fallback]"
            fi
        fi
        echo "  $vol  -> $vol_info$excl"
    done < <(discover_volumes "$config")

    echo ""
    echo "--- Bind Mounts ---"
    while IFS= read -r mount; do
        if [[ -z "$mount" ]]; then continue; fi
        local exists="exists"
        if [[ ! -e "$mount" ]]; then exists="NOT FOUND"; fi
        echo "  $mount  [$exists]"
    done < <(discover_bind_mounts "$config")

    echo ""
    echo "--- Compose Files ---"
    for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml \
             docker-compose.override.yml docker-compose.override.yaml; do
        if [[ -f "$STACK_DIR/$f" ]]; then echo "  $f"; fi
    done

    echo ""
    echo "--- Env Files ---"
    for f in .env .env.local .env.production; do
        if [[ -f "$STACK_DIR/$f" ]]; then echo "  $f"; fi
    done
    echo ""
}

cmd_backup() {
    check_prereqs

    # Check repo exists
    if ! restic snapshots --latest 1 >/dev/null 2>&1; then
        die "Restic repo not initialized. Run: $0 init"
    fi

    info "=== Backup started ==="
    info "Stack: $STACK_DIR"
    info "Target: $RESTIC_REPOSITORY"
    if [[ "$BACKUP_STOP_STACK" != "true" ]]; then
        info "Mode: HOT BACKUP (stack stays running)"
    fi

    local start_time
    start_time="$(date +%s)"

    STAGING_DIR="$(mktemp -d /tmp/dsb-staging.XXXXXX)"
    trap cleanup EXIT

    local config
    config="$(compose_config)"

    # Hot backup safety check
    if [[ "$BACKUP_STOP_STACK" != "true" ]]; then
        if ! check_hot_safety "$config"; then
            warn "Proceeding with hot backup despite unsafe services — data may be inconsistent"
        fi
    fi

    # Phase 1: Pre-hooks (DB dumps while stack is running)
    info "--- Phase 1: Database dumps ---"
    run_pre_hooks "$config" "$STAGING_DIR/dumps"

    # Phase 2: Stop stack + collect volumes
    info "--- Phase 2: Volume collection ---"
    stop_stack
    collect_volumes "$config" "$STAGING_DIR/volumes"

    # Phase 3: Collect stack files
    info "--- Phase 3: Stack files ---"
    collect_stack_files "$config" "$STAGING_DIR"

    # Phase 4: Metadata
    write_metadata "$config" "$STAGING_DIR"

    # Phase 5: restic backup (staging dir + direct volume paths)
    # Stack stays stopped during restic for consistency — restic is fast
    # because it deduplicates at block level (no tar overhead)
    info "--- Phase 5: Restic backup ---"
    local hostname_str
    hostname_str="$(hostname -f 2>/dev/null || hostname)"

    # Build backup path list: staging dir + direct volume paths
    local backup_paths=("$STAGING_DIR")
    for vp in "${VOLUME_BACKUP_PATHS[@]}"; do
        backup_paths+=("$vp")
    done

    restic backup "${backup_paths[@]}" \
        --tag "docker-stack-backup" \
        --tag "stack:$(basename "$STACK_DIR")" \
        --host "$hostname_str"

    BACKUP_SNAPSHOT="$(restic snapshots --latest 1 --json 2>/dev/null \
        | jq -r '.[0].short_id // empty' 2>/dev/null || echo "unknown")"

    # Phase 6: Restart stack
    start_stack
    STACK_WAS_RUNNING=false  # prevent double-start in cleanup

    # Cleanup staging
    rm -rf "$STAGING_DIR"
    STAGING_DIR=""

    local end_time duration
    end_time="$(date +%s)"
    duration="$((end_time - start_time))"

    # Populate hook context
    BACKUP_DURATION="$duration"
    BACKUP_HOSTNAME="$hostname_str"
    BACKUP_STACK="$(basename "$STACK_DIR")"
    BACKUP_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    BACKUP_SIZE="$(restic stats --json 2>/dev/null | jq -r '.total_size_formatted // empty' 2>/dev/null || echo "unknown")"
    if [[ "$BACKUP_SIZE" == "unknown" || -z "$BACKUP_SIZE" ]]; then
        # Fallback: human-readable from restic stats
        BACKUP_SIZE="$(restic stats 2>/dev/null | grep 'Total Size' | awk '{print $3, $4}' || echo "unknown")"
    fi

    ok "=== Backup complete (${duration}s) ==="
    echo ""
    restic snapshots --latest 1

    # Post-hook
    run_hook "$BACKUP_POST_HOOK" "post-hook"
    ping_healthcheck "ok"
}

cmd_list() {
    check_prereqs
    restic snapshots --tag "docker-stack-backup"
}

cmd_check() {
    check_prereqs
    info "Checking repository integrity..."
    restic check
    ok "Repository OK"
}

cmd_prune() {
    check_prereqs
    info "Pruning old snapshots (keep: ${BACKUP_KEEP_DAILY}d ${BACKUP_KEEP_WEEKLY}w ${BACKUP_KEEP_MONTHLY}m)..."
    restic forget \
        --keep-daily "$BACKUP_KEEP_DAILY" \
        --keep-weekly "$BACKUP_KEEP_WEEKLY" \
        --keep-monthly "$BACKUP_KEEP_MONTHLY" \
        --tag "docker-stack-backup" \
        --prune
    ok "Prune complete"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
docker-stack-backup v${VERSION} — Generic encrypted Docker Compose backup

Usage: $(basename "$0") <command> [options]

Commands:
  backup      Full backup (auto-discover + dump + export + restic)
  init        Initialize restic repository
  list        List snapshots
  check       Verify repository integrity
  prune       Remove old snapshots per retention policy
  discover    Dry-run: show what would be backed up
  restore     Restore from snapshot (use restore.sh directly)

Environment:
  STACK_DIR              Stack directory (default: pwd)
  RESTIC_REPOSITORY      Restic target (required)
                         Examples: /backup/mystack
                                   sftp:user@host:/backups/mystack
                                   s3:s3.amazonaws.com/bucket/mystack
  RESTIC_PASSWORD        Encryption password (required)
  RESTIC_PASSWORD_FILE   Alternative: path to password file
  BACKUP_STOP_STACK      Stop stack during backup (default: true, false=hot backup)
  BACKUP_KEEP_DAILY      Daily snapshots to keep (default: 7)
  BACKUP_KEEP_WEEKLY     Weekly snapshots to keep (default: 4)
  BACKUP_KEEP_MONTHLY    Monthly snapshots to keep (default: 3)
  BACKUP_EXCLUDE_VOLUMES Space-separated volume names to skip
  BACKUP_POST_HOOK       Command after successful backup (optional)
  BACKUP_FAIL_HOOK       Command after failed backup (optional)
  BACKUP_HEALTHCHECK_URL Ping URL on success, URL/fail on error

Hook variables (available in hook commands):
  \$BACKUP_DURATION \$BACKUP_SIZE \$BACKUP_SNAPSHOT \$BACKUP_HOSTNAME
  \$BACKUP_STACK \$BACKUP_TIMESTAMP \$BACKUP_ERROR (fail only)

Example:
  export RESTIC_REPOSITORY=/backup/mystack
  export RESTIC_PASSWORD=\$(cat /etc/backup-password)
  export STACK_DIR=/opt/myapp

  $(basename "$0") init        # First time only
  $(basename "$0") discover    # Preview what gets backed up
  $(basename "$0") backup      # Run backup
  $(basename "$0") list        # Show snapshots
  $(basename "$0") prune       # Cleanup old snapshots
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    local cmd="${1:-help}"

    case "$cmd" in
        backup)     cmd_backup ;;
        init)       cmd_init ;;
        list|snapshots) cmd_list ;;
        check)      cmd_check ;;
        prune)      cmd_prune ;;
        discover)   cmd_discover ;;
        restore)    exec "$SCRIPT_DIR/restore.sh" "${@:2}" ;;
        help|--help|-h) usage ;;
        *)
            err "Unknown command: $cmd"
            usage
            exit 1
            ;;
    esac
}

main "$@"
