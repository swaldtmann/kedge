# Bare-Metal Restore

Full VPS/server recovery from a restic backup snapshot. Designed to work on a completely fresh server with only Docker and restic installed.

## Prerequisites

On the new server:

```bash
apt-get update && apt-get install -y docker.io docker-compose-v2 restic jq rsync
```

## Quick Restore

```bash
# Set credentials
export RESTIC_REPOSITORY=sftp:user@host:/path/to/repo
export RESTIC_PASSWORD=<your-password>
export RESTORE_TARGET=/opt/myapp

# Restore latest snapshot
./restore.sh latest
```

## Step by Step

### 1. Get the restore script

From a local checkout, another server, or download:

```bash
scp local-machine:docker-stack-backup/restore.sh /usr/local/bin/dsb-restore
chmod +x /usr/local/bin/dsb-restore
```

### 2. Set credentials

```bash
export RESTIC_REPOSITORY=sftp:uXXXXXX@uXXXXXX.your-storagebox.de:/myapp
export RESTIC_PASSWORD=<from-your-password-manager>
export RESTORE_TARGET=/opt/myapp
```

For SFTP targets, ensure SSH access works:
```bash
ssh uXXXXXX@uXXXXXX.your-storagebox.de ls /myapp
```

### 3. List available snapshots

```bash
dsb-restore --list
```

Output:
```
ID        Time                 Host        Tags
------------------------------------------------------
ad7f04f3  2026-03-20 03:00:06  myserver    docker-stack-backup
bf9e2c11  2026-03-19 03:00:04  myserver    docker-stack-backup
...
```

### 4. Restore

```bash
# Latest snapshot
dsb-restore latest

# Specific snapshot by ID
dsb-restore ad7f04f3
```

### 5. Verify

```bash
# Check containers are running
docker ps

# Check service health
curl -s http://localhost:8080/
```

## What the Restore Does

1. **Restic restore** — pulls snapshot to a temporary staging directory
2. **Stack files** — restores `docker-compose.yml`, `.env`, all configs and bind mounts to `RESTORE_TARGET`
3. **External bind mounts** — restores paths outside the stack directory to their original locations
4. **Docker volumes** — creates named volumes and imports data from archived tar.gz files
5. **Database imports** — starts DB containers first, then:
   - PostgreSQL: `gunzip | psql` from `pg_dumpall` dump
   - MySQL/MariaDB: `gunzip | mysql` from `mysqldump` dump
   - MongoDB: `mongorestore --archive` from `mongodump` archive
   - Valkey/Redis: data restored via volume (AOF/RDB files)
6. **Full stack start** — `docker compose up -d`

## Verify-Only Mode

Restore files without starting the stack (for inspection):

```bash
dsb-restore --verify latest
```

This restores everything to `RESTORE_TARGET` but does not run `docker compose up`. You can inspect the files and start manually:

```bash
cd /opt/myapp
docker compose up -d
```

## Volume Name Mapping

The backup stores a `meta.json` with the original Docker volume names (e.g., `myproject_pg_data`). The restore script uses this mapping to recreate volumes with the correct names so that `docker compose up` finds them.

If the compose project name differs on the new server, volumes may need manual renaming. Check `meta.json` in the staging dir if you encounter issues.

## Troubleshooting

**"No meta.json found"** — The snapshot was not created by docker-stack-backup. Check with `restic ls <snapshot-id>`.

**DB import errors** — PostgreSQL often reports harmless errors about existing roles during `pg_dumpall` restore. Check if the data is actually there:
```bash
docker exec <postgres-container> psql -U postgres -c "\l"
```

**Volume "already exists" warning** — Docker Compose warns when it finds pre-created volumes. This is expected during restore and harmless.

**SFTP connection fails** — Ensure SSH key is available and `known_hosts` has the server's key:
```bash
ssh-keyscan -t ed25519 uXXXXXX.your-storagebox.de >> /root/.ssh/known_hosts
```

**Wrong RESTORE_TARGET** — The restore script uses the original `STACK_DIR` from `meta.json` for context, but places files in `RESTORE_TARGET`. If your new path differs from the original, bind mounts with absolute paths may need adjustment in `docker-compose.yml`.
