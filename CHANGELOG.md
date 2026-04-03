# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-04-03

### Breaking
- **Renamed project from docker-stack-backup to Kedge.** CLI names, restic tags, env files, tmp directories, log files, labels — all changed. See [Migration](#migration-from-docker-stack-backup) below.

### Added
- `BACKUP_PRE_HOOK` for maintenance mode, service stops, etc. before backup (#9)
- `BACKUP_EXCLUDE_MOUNTS` to skip system bind-mounts (#7)
- `CONTRIBUTING.md` + `SECURITY.md` for OSS release

### Fixed
- Pass `--env-file` explicitly to all compose calls — fixes env var warnings via cron (#5, #6)
- Scope loop variable in `is_excluded_mount` / `is_excluded_volume` (#8)
- MariaDB 11+ dump compatibility + image-tag false positive
- Correct config filename in verify.sh cron example (`kedge-backup.env`)
- Correct server type in test.sh comments (`cpx22`)

## Migration from docker-stack-backup

v0.3.0 renames everything. Existing installations need these steps:

1. **Update scripts:** Replace `dsb-backup`, `dsb-restore`, `dsb-verify` with `kedge-backup`, `kedge-restore`, `kedge-verify` in `/usr/local/bin/`
2. **Rename env file:** `/etc/dsb-backup.env` → `/etc/kedge-backup.env` (or `/etc/docker-stack-backup.env` → `/etc/kedge-backup.env`)
3. **Update cron jobs:** Change all `dsb-*` references to `kedge-*`. **Remove old cron entries** — running both causes duplicate backups and stale locks.
4. **Retag snapshots:** `restic tag --add kedge --remove docker-stack-backup` (all snapshots, safe — metadata only)
5. **Clean up:** Remove old binaries, env files, log files (`/var/log/dsb-*.log`)

## [0.2.0] - 2026-03-21

### Added
- Auto-discovery of volumes, bind mounts, databases
- Database hooks: PostgreSQL, MySQL/MariaDB, Valkey/Redis, MongoDB
- Post-backup hooks (`BACKUP_POST_HOOK` / `BACKUP_FAIL_HOOK`)
- `verify.sh` — automated restore verification on hcloud
- Direct volume path backup (skip tar.gz, restic dedup on blocks)
- `BACKUP_HEALTHCHECK_URL` — one-line monitoring setup
- Hot backup safety classification and guardrails
- Bare-metal restore from single snapshot
- Integration guide, restore guide, auto-discovery docs

### Fixed
- `set -e` compatibility, server type fallback
- Skip sockets in external bind mount backup
- Pipefail crash when `POSTGRES_USER` is not set

### Security
- DB credentials via env vars, not CLI arguments

## [0.1.0] - 2026-03-20

### Added
- Generic Docker Compose backup/restore with restic
- Auto-discovery of volumes, bind mounts, databases
- Encrypted + deduplicated backups (AES-256 via restic)
- Basic restore to fresh VPS
- `test.sh` for roundtrip testing on Hetzner Cloud

### Fixed
- Support hcloud CLI contexts in test.sh
