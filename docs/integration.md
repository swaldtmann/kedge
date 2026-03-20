# Integration Guide

How to integrate docker-stack-backup into a provisioning pipeline so every new server gets automated backup out of the box.

## Overview

The typical flow:

1. Provisioning script creates a server and deploys a Docker Compose stack
2. After deploy: upload `dsb-backup` + `dsb-restore` scripts
3. Configure restic target + encryption password
4. Initialize restic repository
5. Set up cron for daily backup + weekly prune
6. Run initial backup

## Server Prerequisites

Add to your bootstrap/provisioning step:

```bash
apt-get install -y docker.io docker-compose-v2 restic jq rsync
```

## Script Upload

Copy the scripts from your local checkout to the server:

```bash
scp backup.sh  "root@$SERVER_IP:/usr/local/bin/dsb-backup"
scp restore.sh "root@$SERVER_IP:/usr/local/bin/dsb-restore"
ssh "root@$SERVER_IP" "chmod +x /usr/local/bin/dsb-backup /usr/local/bin/dsb-restore"
```

## Configuration

Create a config file on the server that cron will source:

```bash
ssh "root@$SERVER_IP" bash -s <<'SETUP'
# Write password file
echo "$BACKUP_PASSWORD" > /etc/backup-password
chmod 600 /etc/backup-password

# Write backup config
cat > /etc/dsb-backup.env <<EOF
STACK_DIR=/opt/myapp
RESTIC_REPOSITORY=sftp:uXXXXXX@uXXXXXX.your-storagebox.de:/myapp
RESTIC_PASSWORD_FILE=/etc/backup-password
BACKUP_KEEP_DAILY=7
BACKUP_KEEP_WEEKLY=4
BACKUP_KEEP_MONTHLY=3
EOF
chmod 600 /etc/dsb-backup.env
SETUP
```

## Repository Initialization

```bash
ssh "root@$SERVER_IP" ". /etc/dsb-backup.env && dsb-backup init"
```

## Cron Setup

```bash
ssh "root@$SERVER_IP" bash -s <<'CRON'
cat > /etc/cron.d/dsb-backup <<'CRONFILE'
# docker-stack-backup — automated backup
SHELL=/bin/bash
0 3 * * * root . /etc/dsb-backup.env && /usr/local/bin/dsb-backup backup >> /var/log/dsb-backup.log 2>&1
0 4 * * 0 root . /etc/dsb-backup.env && /usr/local/bin/dsb-backup prune >> /var/log/dsb-backup.log 2>&1
CRONFILE
chmod 644 /etc/cron.d/dsb-backup

cat > /etc/logrotate.d/dsb-backup <<'LOGR'
/var/log/dsb-backup.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
LOGR
CRON
```

## Initial Backup

```bash
ssh "root@$SERVER_IP" ". /etc/dsb-backup.env && dsb-backup backup"
```

## Complete Example

Here's a minimal provisioning function that sets up backup after deploying a stack:

```bash
setup_backup() {
    local ip="$1"
    local name="$2"
    local backup_target="$3"      # e.g., sftp:user@host:/path
    local backup_password="$4"

    # Upload scripts
    scp backup.sh  "root@$ip:/usr/local/bin/dsb-backup"
    scp restore.sh "root@$ip:/usr/local/bin/dsb-restore"
    ssh "root@$ip" "chmod +x /usr/local/bin/dsb-backup /usr/local/bin/dsb-restore"

    # Configure
    ssh "root@$ip" bash -s -- "$backup_target" "$backup_password" <<'SETUP'
set -euo pipefail
REPO="$1"
PASS="$2"

echo "$PASS" > /etc/backup-password
chmod 600 /etc/backup-password

cat > /etc/dsb-backup.env <<EOF
STACK_DIR=/opt/myapp
RESTIC_REPOSITORY=$REPO
RESTIC_PASSWORD_FILE=/etc/backup-password
BACKUP_KEEP_DAILY=7
BACKUP_KEEP_WEEKLY=4
BACKUP_KEEP_MONTHLY=3
EOF
chmod 600 /etc/dsb-backup.env

# Initialize repo
export RESTIC_REPOSITORY="$REPO"
export RESTIC_PASSWORD="$PASS"
restic init 2>/dev/null || true

# Cron
cat > /etc/cron.d/dsb-backup <<'CRONFILE'
SHELL=/bin/bash
0 3 * * * root . /etc/dsb-backup.env && /usr/local/bin/dsb-backup backup >> /var/log/dsb-backup.log 2>&1
0 4 * * 0 root . /etc/dsb-backup.env && /usr/local/bin/dsb-backup prune >> /var/log/dsb-backup.log 2>&1
CRONFILE
chmod 644 /etc/cron.d/dsb-backup

# Logrotate
cat > /etc/logrotate.d/dsb-backup <<'LOGR'
/var/log/dsb-backup.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
}
LOGR
SETUP

    # Initial backup
    ssh "root@$ip" ". /etc/dsb-backup.env && dsb-backup backup"
}
```

## Files on Server After Setup

| Path | Purpose |
|------|---------|
| `/usr/local/bin/dsb-backup` | Backup script |
| `/usr/local/bin/dsb-restore` | Restore script |
| `/etc/dsb-backup.env` | Config: target, password file, retention |
| `/etc/backup-password` | Restic encryption password (mode 600) |
| `/etc/cron.d/dsb-backup` | Cron schedule |
| `/etc/logrotate.d/dsb-backup` | Log rotation config |
| `/var/log/dsb-backup.log` | Backup log |

## Backup Targets

restic supports multiple backends — set `RESTIC_REPOSITORY` accordingly:

```bash
# Local directory
RESTIC_REPOSITORY=/backup/myapp

# SFTP (Hetzner Storage Box, any SSH server)
RESTIC_REPOSITORY=sftp:uXXXXXX@uXXXXXX.your-storagebox.de:/myapp

# S3 (AWS, MinIO, Wasabi, etc.)
RESTIC_REPOSITORY=s3:s3.amazonaws.com/bucket/myapp

# REST server
RESTIC_REPOSITORY=rest:http://backup-server:8000/myapp
```

For SFTP targets, ensure the server's SSH key is in `/root/.ssh/known_hosts` and an SSH key for authentication is available.

## Monitoring

Check if backups are running:

```bash
# Latest snapshot
ssh root@server ". /etc/dsb-backup.env && dsb-backup list"

# Last log entries
ssh root@server "tail -20 /var/log/dsb-backup.log"

# Repo integrity
ssh root@server ". /etc/dsb-backup.env && dsb-backup check"
```

For alerting, wrap the cron command with your monitoring tool (e.g., Healthchecks.io, Uptime Kuma):

```cron
0 3 * * * root . /etc/dsb-backup.env && /usr/local/bin/dsb-backup backup >> /var/log/dsb-backup.log 2>&1 && curl -s https://hc-ping.com/your-uuid
```
