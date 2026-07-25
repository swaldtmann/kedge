# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-07-26

### Added
- **Python CLI (Phase 1 of the Reforge roadmap)** — `kedge` command, drop-in
  replacement for `backup.sh`'s `init`/`backup`/`list`/`check`/`prune`/
  `discover` (same commands, same env vars, same cron line). `restore` stays
  shell-only until Phase 2. Distributed as a single self-contained `shiv`
  zipapp (no venv/pip install needed on the target host — same "copy one
  file, chmod +x" story as the shell scripts) as well as an installable
  Python package (`pip install -e .` / `pyproject.toml`).
- Full port: auto-discovery (volumes/bind-mounts/services/hot-safety),
  restic wrapper (`--group-by tags` prune retained), volume + stack-file
  collection, DB pre-hooks (Postgres/MySQL/MariaDB/Valkey/Redis/MongoDB,
  including the KEDGE-W-002 hard-fail-without-password hardening), and
  BACKUP_PRE/POST/FAIL_HOOK + healthcheck ping.
- 114 pytest tests, 93% line coverage.

### Fixed (found via live shell/python A-B testing on real Docker stacks + restic repos)
- Hostname resolution: `socket.getfqdn()` can return a garbage reverse-DNS
  name where `hostname -f` returns a clean one — replaced with a
  subprocess-based helper matching the shell's `hostname -f || hostname`.
- `BACKUP_SIZE` hook variable was always "unknown" — modern restic's
  `stats --json` has no `total_size_formatted` field; ported the shell's
  plain-text `restic stats` fallback.

## [0.3.5] - 2026-07-18

### Fixed
- **`cmd_prune` never removed anything** — `restic forget` used the default
  group-by (`host,paths`). Before #18 (stable staging path), every backup run
  used a unique per-run staging dir as part of its backup paths, so each
  snapshot became its own group of one and `--keep-daily/weekly/monthly` kept
  every single one ("Prune complete" logged, 0 actually removed). Added
  `--group-by tags` — kedge tags every snapshot identically (`kedge` +
  `stack:<name>`) regardless of path, so grouping stays correct even if a
  staging path is ever unstable again. Found on prod-cloud (EWH), which was
  still running a pre-#18 build: 111 unpruned snapshots since 2026-04-01.
  Tests: `tests/backup-prune.bats` — real local restic repo (no mocking),
  proves both the fix (3 same-tag/different-path snapshots reduce to 2) and
  the original bug (same setup without `--group-by`: 0 removed).

## [0.3.4] - 2026-07-15

### Fixed
- **shellcheck cleanup in `backup.sh`/`restore.sh`** — `SCRIPT_DIR`/
  `KEDGE_VERSION` split into separate declare+assign (SC2155, masked
  `readlink`/`git describe` failures behind `readonly`'s own exit code).
  Unused loop counters in the Postgres/MySQL restore-wait retries renamed
  `_` (SC2034). `BACKUP_FORMAT_VERSION` in `restore.sh` is intentionally
  unused today (reserved for a future format guard, see v0.3.2) — annotated
  with `shellcheck disable=SC2034` instead of removed. Left the SC2001
  style suggestion in the dump-name regex alone: the alternation
  (`postgres|mysql|mongo`) has no clean bash-builtin equivalent and this
  touches live restore parsing, not worth the risk for a style nit.

## [0.3.3] - 2026-07-15

### Fixed
- **`KEDGE_VERSION` fell back to `dev` when installed via symlink** —
  `backup.sh`/`restore.sh` derived `SCRIPT_DIR` via `dirname
  "${BASH_SOURCE[0]}"`, which leaves a symlinked entry-point unresolved.
  drayve installs kedge as `/usr/local/bin/kedge -> /opt/kedge/backup.sh`;
  without `readlink -f`, `git -C "$SCRIPT_DIR" describe` ran against
  `/usr/local/bin` (not a git repo), so `KEDGE_VERSION` silently fell
  through to `dev` and `meta.json` stamped `"kedge_version": "dev"`
  instead of the real tag. Now resolves the symlink first. Surfaced by
  drayve-greenfield smoke (S327e/S328); confirmed live on prod-genua
  (CW-W-178, 2026-07-15) still showing `kedge dev` under the pinned
  `v0.3.2`.

## [0.3.2] - 2026-05-08

### Changed
- **Split tool version from backup format version.** The single `VERSION`
  constant in `backup.sh` and `restore.sh` (previously hardcoded
  `"1.0.0"`) was overloaded: it drove both the `--help` banner and the
  `meta.json` schema stamp written into every snapshot. Tool bugfixes
  ended up bumping the on-disk format version even when the format
  itself was unchanged.
- Now two constants:
  - `KEDGE_VERSION` — derived at runtime via `git describe --tags
    --always --dirty`, falls back to `dev` for non-git checkouts. Drives
    only the help banner.
  - `BACKUP_FORMAT_VERSION` — hardcoded `"1.0.0"`, bumped only when the
    `meta.json` schema or snapshot layout changes in a way an older
    `restore.sh` could not handle.
- `meta.json` now writes `format_version` and `kedge_version` as
  separate fields. The previous single `version` field is no longer
  emitted. **Restore-side compatibility:** existing `restore.sh` does
  not read either field, so old snapshots (with `version`) and new
  snapshots (with `format_version` + `kedge_version`) restore the same
  way. A future `restore.sh` may guard on `format_version`.

### Fixed
- `--help` banner now reflects the actual checked-out tag instead of a
  hardcoded `"1.0.0"` that drifted from reality.

## [0.3.1] - 2026-05-06

### Fixed
- Restic parent-snapshot detection: stable staging path `/var/lib/kedge/staging/<stack>` instead of random `mktemp`. Restic now finds the previous snapshot and walks only the diff, instead of re-scanning every file each run (#18). Configurable via `KEDGE_STAGING_BASE`. Restore matches both new and legacy paths — old snapshots remain restorable.
- `test.sh` server type: cpx22 → cpx23 (Hetzner retired cpx22).
- Valkey password extraction: prefer reading the mounted secret file at `/run/secrets/valkey_password` over the legacy `--requirepass` argument extraction. The newer Valkey deployment pattern (kigulls-ops AFKI-W-047) drops the inline argument; legacy fallback retained.

### Docs
- Public roadmap added (`ROADMAP.md`) — Reforge from Shell to Python, planned but unscheduled.
- Release checklist in `CONTRIBUTING.md`.
- `SECURITY.md` supported versions aligned with v0.3.0+.

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
- Correct server type in test.sh comments (`cpx23`)

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
