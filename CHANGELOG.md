# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- Renamed project from docker-stack-backup to Kedge

### Fixed
- Pass `--env-file` explicitly to all compose calls — fixes env var warnings via cron (#5, #6)

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
