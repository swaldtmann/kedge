# docker-stack-backup

Generic encrypted backup & bare-metal restore for any Docker Compose stack.

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
| `restore.sh [snapshot]` | Full restore (default: latest) |
| `restore.sh --list` | List available snapshots |
| `restore.sh --verify` | Restore files only, don't start stack |
| `test.sh` | Full roundtrip test on Hetzner Cloud |
| `test.sh --keep` | Test without burning boxes after |
| `test.sh --burn` | Clean up leftover test boxes |

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
# /etc/cron.d/docker-stack-backup
0 3 * * * root STACK_DIR=/opt/myapp RESTIC_REPOSITORY=/backup/myapp RESTIC_PASSWORD_FILE=/etc/backup-pw /opt/docker-stack-backup/backup.sh backup >> /var/log/dsb-backup.log 2>&1
0 4 * * 0 root STACK_DIR=/opt/myapp RESTIC_REPOSITORY=/backup/myapp RESTIC_PASSWORD_FILE=/etc/backup-pw /opt/docker-stack-backup/backup.sh prune >> /var/log/dsb-backup.log 2>&1
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STACK_DIR` | `pwd` | Docker Compose stack directory |
| `RESTIC_REPOSITORY` | — | restic repository (required) |
| `RESTIC_PASSWORD` | — | Encryption password (required) |
| `RESTIC_PASSWORD_FILE` | — | Alternative: file with password |
| `BACKUP_STOP_STACK` | `true` | Stop stack during volume export |
| `BACKUP_KEEP_DAILY` | `7` | Daily snapshots to keep |
| `BACKUP_KEEP_WEEKLY` | `4` | Weekly snapshots to keep |
| `BACKUP_KEEP_MONTHLY` | `3` | Monthly snapshots to keep |
| `BACKUP_EXCLUDE_VOLUMES` | — | Space-separated volume names to skip |
| `BACKUP_POST_HOOK` | — | Command to run after successful backup |
| `BACKUP_FAIL_HOOK` | — | Command to run after failed backup |
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
BACKUP_POST_HOOK='echo "$BACKUP_TIMESTAMP ok $BACKUP_STACK $BACKUP_SNAPSHOT $BACKUP_SIZE ${BACKUP_DURATION}s" >> /var/log/dsb-results.log'
```

## Roundtrip Test

Tests the full backup → restore cycle on Hetzner Cloud:

```bash
export HCLOUD_TOKEN=<your-token>
./test.sh
```

This creates two ephemeral VPS boxes, deploys a sample stack (Postgres + Valkey + Nginx), seeds test data, backs up, restores on a fresh box, verifies data integrity, and burns both.

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

## License

MIT
