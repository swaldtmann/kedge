#!/usr/bin/env bash
# =============================================================================
# restore.sh — Full bare-metal restore of a Docker Compose stack from restic
#
# Designed to run on a fresh VPS with only Docker + restic installed.
# Restores: compose files, .env, named volumes, bind mounts, DB dumps.
#
# Usage:
#   restore.sh [snapshot-id]           Restore specific snapshot (default: latest)
#   restore.sh --list                  List available snapshots
#   restore.sh --verify                Restore + verify only (don't start stack)
#
# Environment:
#   RESTIC_REPOSITORY     restic repo path (required)
#   RESTIC_PASSWORD       restic encryption password (required)
#   RESTIC_PASSWORD_FILE  Alternative: file containing the password
#   RESTORE_TARGET        Where to restore the stack (default: /opt/stack)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
readonly SCRIPT_DIR

# Tool version — derived from git tag at runtime (see backup.sh for rationale).
KEDGE_VERSION="$(git -C "$SCRIPT_DIR" describe --tags --always --dirty 2>/dev/null || echo dev)"
readonly KEDGE_VERSION

# Backup format version this restore.sh can handle. Bump in lockstep with
# backup.sh BACKUP_FORMAT_VERSION when the meta.json schema changes. Not read
# yet (see v0.3.2 changelog) — reserved for a future guard on restore.
# shellcheck disable=SC2034
readonly BACKUP_FORMAT_VERSION="1.0.0"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

RESTIC_REPOSITORY="${RESTIC_REPOSITORY:-}"
RESTIC_PASSWORD="${RESTIC_PASSWORD:-}"
RESTORE_TARGET="${RESTORE_TARGET:-/opt/stack}"
SNAPSHOT_ID="${1:-latest}"

COMPOSE_CMD=""
STAGING_DIR=""

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

    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD="docker-compose"
    else
        die "Neither 'docker compose' nor 'docker-compose' found"
    fi

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
# Cleanup
# ---------------------------------------------------------------------------

cleanup() {
    if [[ -n "$STAGING_DIR" && -d "$STAGING_DIR" ]]; then
        rm -rf "$STAGING_DIR"
    fi
}

# ---------------------------------------------------------------------------
# Main restore logic
# ---------------------------------------------------------------------------

cmd_list() {
    check_prereqs
    restic snapshots --tag "kedge"
}

cmd_restore() {
    check_prereqs

    local verify_only=false
    local force_live=false
    while [[ "${1:-}" == "--verify" || "${1:-}" == "--force-live" ]]; do
        case "$1" in
            --verify)     verify_only=true ;;
            --force-live) force_live=true ;;
        esac
        shift
    done
    SNAPSHOT_ID="${1:-latest}"

    info "=== Restore started ==="
    info "Repository: $RESTIC_REPOSITORY"
    info "Snapshot: $SNAPSHOT_ID"
    info "Target: $RESTORE_TARGET"

    local start_time
    start_time="$(date +%s)"

    STAGING_DIR="$(mktemp -d /tmp/kedge-restore.XXXXXX)"
    trap cleanup EXIT

    # Phase 1: Restic restore to staging
    info "--- Phase 1: Restic restore ---"
    restic restore "$SNAPSHOT_ID" --target "$STAGING_DIR" --tag "kedge"

    # Find the actual backup root (restic preserves full path).
    # Match both new stable paths (*/staging/<stack>/) and legacy mktemp paths
    # (*/kedge-staging.XXXXXX) for backward compatibility with old snapshots (#18).
    local backup_root
    backup_root="$(find "$STAGING_DIR" -name "meta.json" \( -path "*/staging/*" -o -path "*/kedge-staging*" \) -print -quit 2>/dev/null)"
    if [[ -z "$backup_root" ]]; then
        die "No meta.json found in snapshot — is this a kedge snapshot?"
    fi
    backup_root="$(dirname "$backup_root")"
    ok "Backup data found at: $backup_root"

    # Read metadata
    local orig_stack_dir
    orig_stack_dir="$(jq -r '.stack_dir' "$backup_root/meta.json")"
    info "Original stack was at: $orig_stack_dir"

    if [[ -f "$backup_root/meta.json" ]]; then
        echo ""
        echo "--- Backup metadata ---"
        jq '.' "$backup_root/meta.json"
        echo ""
    fi

    # Phase 2: Restore stack files
    info "--- Phase 2: Restore stack files ---"
    mkdir -p "$RESTORE_TARGET"

    if [[ -d "$backup_root/stack-dir" ]]; then
        # The stack-dir contains the full directory tree
        # Find the actual content (rsync --relative creates nested structure)
        local stack_content
        stack_content="$backup_root/stack-dir"

        # rsync with --relative creates the path structure, navigate to leaf
        # The content is at stack-dir/<relative-path>/
        if [[ -d "$stack_content/./" ]]; then
            rsync -a "$stack_content/./" "$RESTORE_TARGET/"
        else
            rsync -a "$stack_content/" "$RESTORE_TARGET/"
        fi
        ok "Stack files restored to $RESTORE_TARGET"
    fi

    # Restore compose files and env files (may be separate copies)
    for f in docker-compose.yml docker-compose.yaml compose.yml compose.yaml \
             docker-compose.override.yml docker-compose.override.yaml \
             .env .env.local .env.production; do
        if [[ -f "$backup_root/$f" ]]; then
            cp "$backup_root/$f" "$RESTORE_TARGET/"
            ok "  Restored: $f"
        fi
    done

    # Phase 3: External bind mounts
    info "--- Phase 3: Restore external bind mounts ---"
    if [[ -d "$backup_root/external-mounts" ]]; then
        for archive in "$backup_root/external-mounts/"*.tar.gz; do
            [[ -f "$archive" ]] || continue
            local mount_name
            mount_name="$(basename "$archive" .tar.gz)"
            # Convert encoded path back: _opt_data → /opt/data
            local mount_path
            mount_path="/$(echo "$mount_name" | tr '_' '/')"
            info "Restoring external mount: $mount_path"
            mkdir -p "$mount_path"
            tar xzf "$archive" -C "$mount_path"
            ok "  $mount_path restored"
        done
    else
        info "No external bind mounts to restore"
    fi

    # Phase 4: Create Docker volumes and import data
    info "--- Phase 4: Restore Docker volumes ---"

    # Read volume mappings from metadata
    local vol_mapping vol_paths
    vol_mapping="$(jq -r '.volume_mapping // {}' "$backup_root/meta.json" 2>/dev/null || echo '{}')"
    vol_paths="$(jq -r '.volume_paths // {}' "$backup_root/meta.json" 2>/dev/null || echo '{}')"

    local vol_count=0
    for vol_name in $(echo "$vol_mapping" | jq -r 'keys[]' 2>/dev/null); do
        local real_vol_name
        real_vol_name="$(echo "$vol_mapping" | jq -r --arg v "$vol_name" '.[$v]')"

        if [[ -z "$real_vol_name" || "$real_vol_name" == "null" ]]; then
            local project_name
            project_name="$(basename "$RESTORE_TARGET" | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]//g')"
            real_vol_name="${project_name}_${vol_name}"
        fi

        # CW-W-243: --verify must never write into the real, potentially-live
        # volume — meta.json's vol_mapping says nothing about whether this
        # host IS the backup source, so a --verify restore on the same host
        # as the backup used to land straight in the live volume's mountpoint.
        # --verify always restores under an isolated name instead. A genuine
        # (non-verify) restore keeps using the real name — that's the normal
        # disaster-recovery-onto-a-fresh-host case — but only after the
        # live-volume guard below clears it.
        local restore_vol_name
        if $verify_only; then
            restore_vol_name="${real_vol_name}_restoretest"
        else
            restore_vol_name="$real_vol_name"
            if docker volume inspect "$real_vol_name" >/dev/null 2>&1; then
                local mounted_by
                mounted_by="$(docker ps -q --filter "volume=$real_vol_name")"
                if [[ -n "$mounted_by" ]]; then
                    if ! $force_live; then
                        die "Volume '$real_vol_name' already exists AND is mounted by a running container — this restore target looks like the live backup source host. Refusing to overwrite live data. Re-run with --force-live if this is really intended."
                    fi
                    warn "  --force-live set: overwriting '$real_vol_name' while mounted by a running container"
                fi
            fi
        fi

        # Create volume
        docker volume create "$restore_vol_name" >/dev/null 2>&1 || true
        local new_vol_path
        new_vol_path="$(docker volume inspect --format '{{.Mountpoint}}' "$restore_vol_name" 2>/dev/null)"

        # Source 1: Direct volume path in restic snapshot (block-level dedup backup)
        local orig_vol_path
        orig_vol_path="$(echo "$vol_paths" | jq -r --arg v "$vol_name" '.[$v] // empty' 2>/dev/null)"
        local restored_vol_dir=""
        if [[ -n "$orig_vol_path" ]]; then
            # Find the restored volume data in the staging dir (restic preserves full paths)
            restored_vol_dir="$(find "$STAGING_DIR" -path "*${orig_vol_path}" -type d 2>/dev/null | head -1)"
        fi

        if [[ -n "$restored_vol_dir" && -d "$restored_vol_dir" ]]; then
            info "Restoring volume: $vol_name -> $restore_vol_name [direct]"
            rsync -a --delete "$restored_vol_dir/" "$new_vol_path/"
            ok "  $restore_vol_name restored [direct]"

        # Source 2: tar.gz fallback (from collect_volumes fallback path)
        elif [[ -f "$backup_root/volumes/${vol_name}.tar.gz" ]]; then
            info "Restoring volume: $vol_name -> $restore_vol_name [tar]"
            docker run --rm \
                -v "$restore_vol_name":/data \
                -v "$backup_root/volumes":/backup:ro \
                alpine sh -c "rm -rf /data/* /data/..?* /data/.[!.]* 2>/dev/null; tar xzf /backup/${vol_name}.tar.gz -C /data"
            ok "  $restore_vol_name restored [tar]"

        else
            warn "  No data found for volume $vol_name — skipping"
            continue
        fi

        vol_count=$((vol_count + 1))
    done

    if [[ $vol_count -eq 0 ]]; then
        info "No volumes to restore"
    else
        ok "$vol_count volume(s) restored"
    fi

    if $verify_only; then
        ok "=== Verify complete — stack NOT started ==="
        info "Files restored to: $RESTORE_TARGET"
        if [[ $vol_count -gt 0 ]]; then
            info "Docker volumes restored under isolated *_restoretest names — no live volume was touched."
        fi
        info "To start: cd $RESTORE_TARGET && $COMPOSE_CMD up -d"
        cleanup
        return 0
    fi

    # Phase 5: Start the stack (DB containers first for dump import)
    info "--- Phase 5: Start stack ---"
    cd "$RESTORE_TARGET"

    # First, try to start just DB services to import dumps
    if [[ -d "$backup_root/dumps" ]] && ls "$backup_root/dumps/"* >/dev/null 2>&1; then
        info "Starting database containers for dump import..."

        # Detect DB services from compose config
        local config
        config="$($COMPOSE_CMD config --format json 2>/dev/null)"
        local db_services=()

        while IFS=$'\t' read -r svc image; do
            if [[ -z "$svc" ]]; then continue; fi
            case "$image" in
                *postgres*|*postgis*|*mariadb*|*mysql*|*mongo*)
                    db_services+=("$svc")
                    ;;
            esac
        done < <(echo "$config" | jq -r '.services | to_entries[] | "\(.key)\t\(.value.image // "build")"')

        if [[ ${#db_services[@]} -gt 0 ]]; then
            $COMPOSE_CMD up -d "${db_services[@]}"
            info "Waiting for databases to be ready..."
            sleep 15

            # Import dumps
            for dump_file in "$backup_root/dumps/"*; do
                [[ -f "$dump_file" ]] || continue
                local dump_name
                dump_name="$(basename "$dump_file")"
                local svc_name
                svc_name="$(echo "$dump_name" | sed 's/_\(postgres\|mysql\|mongo\).*$//')"

                case "$dump_name" in
                    *_postgres.sql.gz)
                        info "Importing PostgreSQL dump: $dump_name -> $svc_name"
                        local container
                        container="$($COMPOSE_CMD ps -q "$svc_name" 2>/dev/null | head -1)"
                        if [[ -n "$container" ]]; then
                            local pg_user
                            pg_user="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
                                | grep '^POSTGRES_USER=' | cut -d= -f2)"
                            pg_user="${pg_user:-postgres}"
                            # Wait for postgres to accept connections
                            for _ in $(seq 1 30); do
                                if docker exec "$container" pg_isready -U "$pg_user" >/dev/null 2>&1; then
                                    break
                                fi
                                sleep 2
                            done
                            gunzip -c "$dump_file" | docker exec -i "$container" psql -U "$pg_user" 2>&1 \
                                | tail -3 || warn "PostgreSQL import reported errors (may be harmless)"
                            ok "  PostgreSQL dump imported"
                        else
                            warn "  Container for $svc_name not running — skip dump import"
                        fi
                        ;;

                    *_mysql.sql.gz)
                        info "Importing MySQL dump: $dump_name -> $svc_name"
                        local container
                        container="$($COMPOSE_CMD ps -q "$svc_name" 2>/dev/null | head -1)"
                        if [[ -n "$container" ]]; then
                            # Pass password via MYSQL_PWD env var (not visible in ps)
                            local mysql_pass=""
                            mysql_pass="$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container" \
                                | grep -E '^(MYSQL_ROOT_PASSWORD|MARIADB_ROOT_PASSWORD)=' | head -1 | cut -d= -f2 || true)"
                            local mysql_exec_args=()
                            if [[ -n "$mysql_pass" ]]; then
                                mysql_exec_args=(-e "MYSQL_PWD=$mysql_pass")
                            fi
                            # Wait for mysql
                            for _ in $(seq 1 30); do
                                if docker exec "${mysql_exec_args[@]}" "$container" mysqladmin ping -uroot >/dev/null 2>&1; then
                                    break
                                fi
                                sleep 2
                            done
                            gunzip -c "$dump_file" | docker exec -i "${mysql_exec_args[@]}" "$container" mysql -uroot 2>&1 \
                                | tail -3 || warn "MySQL import reported errors (may be harmless)"
                            ok "  MySQL dump imported"
                        fi
                        ;;

                    *_mongo.archive.gz)
                        info "Importing MongoDB dump: $dump_name -> $svc_name"
                        local container
                        container="$($COMPOSE_CMD ps -q "$svc_name" 2>/dev/null | head -1)"
                        if [[ -n "$container" ]]; then
                            docker exec -i "$container" mongorestore --archive --gzip < "$dump_file" 2>&1 \
                                | tail -3 || warn "MongoDB import reported errors"
                            ok "  MongoDB dump imported"
                        fi
                        ;;
                esac
            done
        fi
    fi

    # Start full stack
    info "Starting full stack..."
    $COMPOSE_CMD up -d

    # Brief wait, then show status
    sleep 5
    echo ""
    echo "--- Container status ---"
    $COMPOSE_CMD ps
    echo ""

    # Cleanup staging
    cleanup
    STAGING_DIR=""
    trap - EXIT

    local end_time duration
    end_time="$(date +%s)"
    duration="$((end_time - start_time))"

    ok "=== Restore complete (${duration}s) ==="
    info "Stack running at: $RESTORE_TARGET"
}

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
kedge restore ${KEDGE_VERSION} — Bare-metal Docker Compose restore

Usage: $(basename "$0") [options] [snapshot-id]

Options:
  --list        List available snapshots
  --verify      Restore files only, don't start stack. Docker volumes are
                always restored under an isolated *_restoretest name — this
                never touches a live volume, even when run on the same host
                the backup was taken from.
  --force-live  Only relevant for a real (non --verify) restore: skip the
                safety check that refuses to overwrite a volume which
                already exists AND is mounted by a running container. Use
                only when you deliberately intend to overwrite live data.
  --help        Show this help

Arguments:
  snapshot-id  Restic snapshot to restore (default: latest)

Environment:
  RESTIC_REPOSITORY      Restic repository (required)
  RESTIC_PASSWORD        Encryption password (required)
  RESTIC_PASSWORD_FILE   Alternative: path to password file
  RESTORE_TARGET         Where to restore (default: /opt/stack)

Example (bare-metal VPS restore):
  # Install prerequisites
  apt-get update && apt-get install -y docker.io restic jq

  # Set credentials
  export RESTIC_REPOSITORY=sftp:backup@storage:/kigulls-kunde01
  export RESTIC_PASSWORD=\$(cat /etc/backup-password)
  export RESTORE_TARGET=/opt/kigulls

  # Restore
  $(basename "$0") --list              # Pick a snapshot
  $(basename "$0") latest              # Or restore latest
  $(basename "$0") abc12345            # Or restore specific snapshot
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    case "${1:-}" in
        --list|-l)      cmd_list ;;
        --help|-h|help) usage ;;
        --verify)       cmd_restore --verify "${@:2}" ;;
        *)              cmd_restore "$@" ;;
    esac
}

# Nur ausfuehren, wenn direkt aufgerufen -- beim Sourcen (bats-Tests) still bleiben.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
