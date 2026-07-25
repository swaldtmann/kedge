# Kedge

Generic encrypted backup & bare-metal restore for any Docker Compose stack. Your rescue anchor for Docker.

Auto-discovers volumes, bind mounts, and databases. No configuration files needed — just point it at a stack directory and a restic repository.

## Features

- **Auto-discovery**: Reads `docker-compose.yml`, finds all named volumes, bind mounts, and services automatically
- **Database hooks**: Detects PostgreSQL, MySQL/MariaDB, Valkey/Redis, MongoDB by image name — dumps before backup
- **Dynamic**: New containers added to the stack are automatically included in the next backup
- **Encrypted + deduplicated**: Powered by [restic](https://restic.net/) (AES-256)
- **Variable targets**: Local directory, SFTP (Hetzner Storage Box), S3, REST server
- **Bare-metal restore**: Full VPS recovery from a single snapshot
- **Tested**: Roundtrip test with Hetzner Cloud boxes (deploy → backup → fresh box → restore → verify)

## Quick Start

```bash
# On your server, in the stack directory:
export RESTIC_REPOSITORY=/backup/mystack        # or sftp:user@host:/path
export RESTIC_PASSWORD=$(openssl rand -hex 16)   # save this!
export STACK_DIR=/opt/myapp                      # where docker-compose.yml lives

# First time
./backup.sh init

# Preview what gets backed up
./backup.sh discover

# Run backup
./backup.sh backup

# List snapshots
./backup.sh list
```

## Restore (bare-metal)

On a fresh VPS with Docker + restic installed:

```bash
apt-get update && apt-get install -y docker.io docker-compose-v2 restic jq

export RESTIC_REPOSITORY=sftp:backup@storage:/mystack
export RESTIC_PASSWORD=<your-password>
export RESTORE_TARGET=/opt/myapp

./restore.sh latest
```

## Commands

| Command | Description |
|---------|-------------|
| `backup.sh init` | Initialize restic repository |
| `backup.sh discover` | Dry-run: show what would be backed up |
| `backup.sh backup` | Full backup (dumps + volumes + files → restic) |
| `backup.sh list` | List snapshots |
| `backup.sh check` | Verify repository integrity |
| `backup.sh prune` | Remove old snapshots per retention policy |
| `verify.sh [snapshot]` | Restore verification on ephemeral hcloud box |
| `verify.sh --burn` | Clean up leftover verify boxes |
| `restore.sh [snapshot]` | Full restore (default: latest) |
| `restore.sh --list` | List available snapshots |
| `restore.sh --verify` | Restore files only, don't start stack |
| `test.sh` | Full roundtrip test on Hetzner Cloud |
| `test.sh --keep` | Test without burning boxes after |
| `test.sh --burn` | Clean up leftover test boxes |

## Test-restores — never against the live backup source

`restore.sh --verify` restores Docker volumes under an isolated `*_restoretest` name — it
never writes into a volume that a running container has mounted, even when run on the same
host the backup was taken from (CW-W-243). A genuine, non-`--verify` restore still refuses to
overwrite a volume that already exists AND is mounted by a running container, unless you pass
`--force-live`.

That guard is defense in depth, not the primary safety mechanism. **The primary mechanism is:
don't manually run `restore.sh --verify` on a production host at all.** Use `verify.sh`
instead — it restores onto a fresh, ephemeral Hetzner Cloud box (never the backup source),
checks that the stack actually starts and is healthy, then burns the box. That's the tool
quarterly restore-test routines (`EWH-W-133`, `CW-W-223`) should call, not a manual SSH session
against `prod-*`.

## Backup Targets

restic supports multiple backends:

```bash
# Local directory
export RESTIC_REPOSITORY=/backup/mystack

# SFTP (Hetzner Storage Box, any SSH server)
export RESTIC_REPOSITORY=sftp:uXXXXXX@uXXXXXX.your-storagebox.de:/mystack

# S3 (AWS, MinIO, etc.)
export RESTIC_REPOSITORY=s3:s3.amazonaws.com/bucket/mystack

# REST server
export RESTIC_REPOSITORY=rest:http://backup-server:8000/mystack
```

## What Gets Backed Up

The backup script auto-discovers everything from `docker-compose.yml`:

| Category | What | How |
|----------|------|-----|
| **Database dumps** | PostgreSQL, MySQL, MariaDB, Valkey/Redis, MongoDB | Detected by image name, dumped before stack stop |
| **Named volumes** | All volumes defined in compose file | Exported as tar.gz |
| **Bind mounts** | All host directories mounted into containers | Copied (in-stack) or archived (external) |
| **Stack files** | docker-compose.yml, .env, override files | Direct copy |
| **Metadata** | Timestamp, hostname, container list, volume mapping | meta.json |

## Cron Setup

```bash
# /etc/cron.d/kedge
0 3 * * * root STACK_DIR=/opt/myapp RESTIC_REPOSITORY=/backup/myapp RESTIC_PASSWORD_FILE=/etc/backup-pw /opt/kedge/backup.sh backup >> /var/log/kedge-backup.log 2>&1
0 4 * * 0 root STACK_DIR=/opt/myapp RESTIC_REPOSITORY=/backup/myapp RESTIC_PASSWORD_FILE=/etc/backup-pw /opt/kedge/backup.sh prune >> /var/log/kedge-backup.log 2>&1
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STACK_DIR` | `pwd` | Docker Compose stack directory |
| `RESTIC_REPOSITORY` | — | restic repository (required) |
| `RESTIC_PASSWORD` | — | Encryption password (required) |
| `RESTIC_PASSWORD_FILE` | — | Alternative: file with password |
| `BACKUP_STOP_STACK` | `true` | Stop stack during backup (`false` = hot backup, see below) |
| `BACKUP_KEEP_DAILY` | `7` | Daily snapshots to keep |
| `BACKUP_KEEP_WEEKLY` | `4` | Weekly snapshots to keep |
| `BACKUP_KEEP_MONTHLY` | `3` | Monthly snapshots to keep |
| `BACKUP_EXCLUDE_VOLUMES` | — | Space-separated volume names to skip |
| `SYSTEM_PATHS` | — | Space-separated absolute paths backed up alongside the stack (host-level state outside Docker, e.g. `/etc /root`) |
| `SYSTEM_PATHS_EXCLUDE` | — | Space-separated restic `--exclude` patterns applied to `SYSTEM_PATHS` |
| `BACKUP_POST_HOOK` | — | Command to run after successful backup |
| `BACKUP_FAIL_HOOK` | — | Command to run after failed backup |
| `BACKUP_HEALTHCHECK_URL` | — | Ping on success, `/fail` on error |
| `RESTORE_TARGET` | `/opt/stack` | Where to restore the stack |

## Post-Backup Hooks

Run arbitrary commands after backup completes. Hook commands have access to context variables:

| Variable | Content |
|----------|---------|
| `$BACKUP_DURATION` | Duration in seconds |
| `$BACKUP_SIZE` | Repository size (human-readable) |
| `$BACKUP_SNAPSHOT` | Snapshot ID |
| `$BACKUP_HOSTNAME` | Server hostname |
| `$BACKUP_STACK` | Stack directory basename |
| `$BACKUP_TIMESTAMP` | ISO 8601 UTC timestamp |
| `$BACKUP_ERROR` | Error message (fail hook only) |

Examples:

```bash
# Healthchecks.io ping
BACKUP_POST_HOOK='curl -sf https://hc-ping.com/your-uuid'
BACKUP_FAIL_HOOK='curl -sf https://hc-ping.com/your-uuid/fail'

# MQTT (e.g., KIgulls swarm monitoring)
BACKUP_POST_HOOK='mosquitto_pub -h localhost -t health/backup -m "{\"status\":\"ok\",\"snapshot\":\"$BACKUP_SNAPSHOT\",\"duration\":$BACKUP_DURATION,\"size\":\"$BACKUP_SIZE\",\"ts\":\"$BACKUP_TIMESTAMP\"}"'

# Slack webhook
BACKUP_POST_HOOK='curl -sf -X POST -H "Content-Type: application/json" -d "{\"text\":\"Backup OK: $BACKUP_STACK ($BACKUP_SIZE, ${BACKUP_DURATION}s)\"}" https://hooks.slack.com/services/xxx'

# Log to file
BACKUP_POST_HOOK='echo "$BACKUP_TIMESTAMP ok $BACKUP_STACK $BACKUP_SNAPSHOT $BACKUP_SIZE ${BACKUP_DURATION}s" >> /var/log/kedge-results.log'
```

Or skip hooks entirely and just set a URL — works with Healthchecks.io, Uptime Kuma, Cronitor:

```bash
BACKUP_HEALTHCHECK_URL=https://hc-ping.com/your-uuid
```

Pings the URL on success (with duration + size in body), appends `/fail` on error. No hook config needed.

## Hot Backup (Zero Downtime)

By default, `backup.sh` stops the stack during volume export for full consistency. Set `BACKUP_STOP_STACK=false` for zero-downtime backups where the stack keeps running.

```bash
BACKUP_STOP_STACK=false ./backup.sh backup
```

### How it works

- **Phase 1 (database dumps)** already runs while the stack is up — `pg_dumpall`, `mysqldump`, `BGSAVE` produce consistent snapshots by design
- **Phase 2 (volume export)** runs without stopping containers — volume data must be crash-consistent
- Restic's block-level dedup handles changing files gracefully

### Safety classification

Run `discover` to see the safety classification for each service:

```
$ ./backup.sh discover
--- Services ---
  postgres  (postgres:16)  [pre-hook: postgres]
  valkey    (valkey/valkey:8)  [pre-hook: valkey]
  grafana   (grafana/grafana:11)  [hot-safe]
  traefik   (traefik:v3)  [hot-safe]
  myapp     (myapp:latest)

--- Hot Backup Safety ---
  wrn  Service 'myapp' (myapp:latest) has no pre-hook and is not known to be crash-consistent
  Some services may not be safe for hot backup (see warnings above)
  Review before setting BACKUP_STOP_STACK=false
```

| Classification | Meaning |
|----------------|---------|
| `[pre-hook: X]` | Database dump runs before backup — always consistent |
| `[hot-safe]` | Known crash-consistent (WAL, append-only, stateless, or config-driven) |
| `[build — verify manually]` | Custom image, can't auto-classify |
| *(no tag)* | Unknown — review before enabling hot backup |

### Known hot-safe service types

Services with built-in crash recovery or no mutable state:

- **Monitoring**: Prometheus, Grafana, Loki, Alertmanager, VictoriaMetrics
- **Reverse proxies**: Traefik, Nginx, Caddy, HAProxy
- **Auth/SSO**: Authelia, LLDAP, Keycloak, Dex
- **Security**: CrowdSec
- **Bookmarks/Read-later**: Readeck, Wallabag, Linkding (SQLite WAL)
- **MQTT brokers**: Mosquitto, EMQX, VerneMQ
- **Message queues**: RabbitMQ, NATS
- **Wikis**: XWiki, BookStack, Wiki.js
- **Mail**: Dovecot, Stalwart, Mailcow
- **Password managers**: Vaultwarden (SQLite WAL)
- **Misc**: Listmonk, n8n, Gitea, Forgejo, Miniflux, FreshRSS

### When to use hot backup

- Stacks where all services have pre-hooks or are known hot-safe
- Uptime-critical deployments where even brief downtime is unacceptable
- When `discover` shows no unclassified services (or you've verified them manually)

### When NOT to use hot backup

- Services writing multi-file transactions without journaling
- Custom applications without crash recovery
- If in doubt: keep the default (`BACKUP_STOP_STACK=true`)

## Roundtrip Test

Tests the full backup → restore cycle on Hetzner Cloud:

```bash
export HCLOUD_TOKEN=<your-token>
./test.sh
```

This creates two ephemeral VPS boxes, deploys a sample stack (Postgres + Valkey + Nginx), seeds test data, backs up, restores on a fresh box, verifies data integrity, and burns both.

## Restore Verification

Monthly automated proof that your backups actually work. Spins up a fresh Hetzner Cloud box, restores the latest snapshot, runs health checks, burns the box.

```bash
# Verify latest backup
. /etc/kedge-backup.env && ./verify.sh

# Cron: monthly on the 1st at 05:00 UTC
0 5 1 * * root . /etc/kedge-backup.env && /usr/local/bin/kedge-verify latest >> /var/log/kedge-verify.log 2>&1
```

Health checks:
- All containers from `docker-compose.yml` are running
- Database containers accept connections (PostgreSQL, MySQL, Valkey, MongoDB)
- Services with exposed ports respond to HTTP
- `.env` configuration file is present

## Documentation

| Doc | Content |
|-----|---------|
| [Auto-Discovery](docs/auto-discovery.md) | How volumes, bind mounts, and databases are detected automatically |
| [Bare-Metal Restore](docs/restore.md) | Full VPS recovery step by step |
| [Integration Guide](docs/integration.md) | Embedding backup into your provisioning pipeline (cron, config, scripts) |

## Prerequisites

- `docker` (with compose v2 plugin)
- `restic`
- `jq`
- `rsync`

Install on Ubuntu/Debian:
```bash
apt-get install -y docker.io docker-compose-v2 restic jq rsync
```

## Built with

This project is developed with [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (Anthropic Claude Opus 4.6).

## License

Apache 2.0
