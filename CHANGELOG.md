# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Live-bind-mount restore guard for external mounts (CW-W-258 follow-up),
  mirroring CW-W-243's Docker-volume guard.** Restoring an external bind
  mount (now a direct restic path, see below) had no protection against
  overwriting a path currently in live use — unlike named volumes, bind
  mounts have no separate "name" to isolate a `--verify` restore behind, so
  a naive port of the volume guard wasn't enough. `restore.sh`/`restore.py`
  now check every RUNNING container's bind-mount sources directly (docker's
  native `--filter volume=<name>` only resolves named volumes) before
  writing: `--verify` always restores under a `_restoretest`-suffixed
  sibling path instead of the real one; a genuine restore refuses to
  overwrite a path currently mounted by a running container unless
  `--force-live` is passed, exactly like the existing volume guard's
  messaging and semantics. Applies to both the new direct-path format and
  the legacy tar.gz format.

### Fixed
- **External bind mounts backed up as direct restic paths, not tar.gz (CW-W-258).**
  Both `backup.sh` and the Python port (`collect.py`/`commands.py`) tarred +
  gzipped every Compose bind mount declared outside `$STACK_DIR` before
  handing it to restic — unlike Docker volumes, which already got a direct
  restic path (block-level dedup) when the mountpoint was accessible. gzip
  defeats restic's content-defined chunking: any change anywhere in the
  source shifts every downstream compressed byte, so the whole archive
  re-stored as "new" on every single backup regardless of how little
  actually changed. Live-measured on `prod-poki`: 18.6G of bind-mounted data
  (`/var/poki/{db,mirror,logs,models,state}`) produced ~6.1GiB of "Added to
  the repository" on every daily run for weeks — 16 kept snapshots ≈ 97.6G
  on the storage box for what should have deduplicated to roughly the live
  data size. Bind mounts are, by Compose definition, always host-local paths
  (unlike named volumes, which can sit behind a non-local driver) — there
  was no dedup-safe fallback case here that justified tar.gz in the first
  place. Existing pre-fix snapshots stay restorable: `restore.sh`/`restore.py`
  keep the legacy `external-mounts/*.tar.gz` extraction path alongside the
  new direct-path lookup (meta.json `bind_mount_paths`), selected per
  snapshot via which format is actually present.
- **Legacy tar.gz external-mount restore double-nested directories**
  (pre-existing, found while adding regression coverage for the fix above).
  The archive was built via `tar czf ... -C "$(dirname "$mount")"
  "$(basename "$mount")"`, so it contains one top-level entry named after
  the mount's own basename — but restore extracted it with `-C "$mount_path"`
  instead of `-C "$(dirname "$mount_path")"`, landing the content at
  `$mount_path/$(basename "$mount_path")/...` instead of `$mount_path/...`.
  No existing test exercised a directory-type external-mount restore, so
  this shipped unnoticed. Fixed in both `restore.sh` and `restore.py`.
  Known unrelated gap in the same legacy path, not fixable after the fact:
  old-format *file* (not directory) external mounts were copied into
  `external-mounts/` using only their bare basename (no path-reconstructing
  information), and restore's loop only ever globbed `*.tar.gz` — such
  mounts were silently never restored by any pre-fix snapshot. The
  information needed to restore them correctly was already lost at backup
  time in every snapshot taken before this fix; nothing to reconstruct on
  the restore side.

### Added
- **`tools/backup-freshness-write` (DRAYVE-W-014)** — optional
  `BACKUP_POST_HOOK` building block that writes a
  `kedge_backup_last_success{repo="..."}` node-exporter textfile-collector
  metric after a successful backup. Generalizes a hand-placed, unversioned
  `/usr/local/sbin/backup-freshness-write` found on `prod-cloud` (host-specific
  `cloud_`-prefixed metric name, wired via a `&&` after the cron call instead
  of through kedge's own hook mechanism) into a proper repo-tracked tool.
- **DB engine registry (`kedge.engines`, KEDGE-W-004)** — one descriptor per
  database engine (image patterns, dump, import, healthcheck bash snippet)
  instead of the same DB type hand-defined in `discovery.py`/`hooks.py`/
  `restore.py`/`verify.py` separately. Postgres/MySQL+MariaDB/Valkey+Redis/
  MongoDB moved onto it with unchanged behavior; the MariaDB
  mysqladmin/mysql-vs-mariadb-admin/mariadb binary gap (KEDGE-W-003) is
  closed for restore-import and verify-healthcheck too (the dump hook
  already had the fallback, the other two never picked it up — exactly the
  bug this registry exists to prevent).
- **SQLite engine** (`prod-poki`) — `SQLITE_WAL_CHECKPOINT_PATHS` env var
  runs a real `PRAGMA wal_checkpoint(TRUNCATE)` (stdlib `sqlite3`, no
  external binary) before a WAL-mode SQLite bind-mount gets tarred, so the
  plain `.db` file is a standalone-consistent snapshot on its own.
- **InfluxDB engine** (`prod-multi01`, real fleet version 2.6) —
  `influx backup`/`influx restore --full` (v2), `influxd backup -portable`
  (v1, dump-only — v1 restore needs a stopped target with a clean data dir,
  documented as unsupported rather than faked). Verified live against real
  `influxdb:2.7` containers: backup one instance, restore into a fresh one,
  data byte-identical.
- 238 pytest tests (was 191), coverage 87-99% across the touched modules.

### Fixed
- **`verify` box defaults were all dead** — the hardcoded server-type
  fallback chain (`cpx23`→`cpx21`→`cax11`) was fully retired by Hetzner's
  2026 "standardization and price adjustment" (all `Available: no` for new
  orders at nbg1/fsn1/hel1), and the default hcloud context `kigulls-test`
  no longer exists. `kedge verify` could not have created a box. Now
  defaults to `cx23`@`hel1` with `cpx22`/`cpx12` fallbacks (all currently
  orderable x86 types) and context `greenfields`. All overridable via
  `VERIFY_SERVER_TYPE`/`VERIFY_LOCATION`/`HCLOUD_CONTEXT`. The pytest suite
  mocked the hcloud calls, so the dead values passed green — found by
  static review while prepping the live Hetzner test (KEDGE-W-003).
- **`kedge verify` box provisioning was incomplete** — `cmd_verify` never
  uploaded the `kedge` binary onto the box (`kedge restore` there would
  have hit `command not found`) nor synced local restic repos to it before
  restoring, both ported from `verify.sh:462-473` and now added (mocked
  tests had hidden the gap; `KEDGE_BINARY` overrides the `dist/kedge` path).
- **`kedge verify` restore step had three live-run risks** — the restic
  repository/password/target were interpolated straight into the remote
  bash script text instead of passed as positional args like
  `verify.sh:485-492` does, so a password containing `"`/`` ` ``/`$`/newline
  could break or silently corrupt the script on the box; a
  `RESTIC_PASSWORD_FILE` with a trailing newline (the common case) was sent
  unstripped, unlike bash's `$(cat ...)`; and `_BOOTSTRAP_SCRIPT` never
  installed `curl`, which the healthcheck's HTTP-endpoint checks need. All
  three fixed.
- **`kedge verify`'s remote ssh commands re-split unquoted args** — ssh
  joins trailing argv elements (repository/password/target/snapshot for
  the restore step, the local-repo `mkdir` path, the healthcheck's
  restore-target) with unquoted spaces into a single string handed to the
  remote login shell, which re-parses `$`/backtick/whitespace *before* the
  invoked `bash -s` ever sees its positional params — a live run showed an
  unquoted `$2026` in a restic password silently expand to empty on the
  box. Latent in both shell and python verify (`verify.sh:485` has the
  same underlying issue), surfaced by the first live run with a
  `$`-containing restic password. Now `shlex.quote`s every value into a
  single ssh command string; the local-repo rsync transfer additionally
  gets `--protect-args` (`-s`) for the same reason on its own remote hop.
- checksum verify could never pass for tar-fallback backups (macOS/Docker-
  Desktop, any host without direct volume mountpoints) — backup hashed the
  .tar.gz, restore hashed the unpacked tree; surfaced by the live roundtrip.

## [0.5.0] - 2026-07-26

First Python release meant to be run for real (Phase 1 + Phase 2 of the
Reforge roadmap together — `0.4.0` was never tagged/released, its
Phase-1-only content is folded into this release instead). Full
`backup` → `restore` → `verify` roundtrip now works end to end; this is
the "Python stable" milestone the shell version's migration path names
as the point where `v0.3.x` starts being phased out (still maintained
in parallel, not removed).

### Added
- **Python CLI (Phase 1)** — `kedge` command, drop-in replacement for
  `backup.sh`'s `init`/`backup`/`list`/`check`/`prune`/`discover` (same
  commands, same env vars, same cron line). Distributed as a single
  self-contained `shiv` zipapp (no venv/pip install needed on the target
  host — same "copy one file, chmod +x" story as the shell scripts) as
  well as an installable Python package (`pip install -e .` /
  `pyproject.toml`).
- Full port: auto-discovery (volumes/bind-mounts/services/hot-safety),
  restic wrapper (`--group-by tags` prune retained), volume + stack-file
  collection, DB pre-hooks (Postgres/MySQL/MariaDB/Valkey/Redis/MongoDB,
  including the KEDGE-W-002 hard-fail-without-password hardening), and
  BACKUP_PRE/POST/FAIL_HOOK + healthcheck ping.
- **`kedge restore` / `kedge verify` / `kedge burn` (Phase 2, KEDGE-W-003)** —
  Python port of `restore.sh`/`verify.sh`: bare-metal restore (stack files,
  external bind mounts, Docker volumes direct+tar, live-volume guard from
  CW-W-243), DB dump import (Postgres/MySQL/MariaDB/MongoDB), and the
  Hetzner ephemeral-box restore-verification roundtrip.
- **Checksum verify (issue #1)** — never built in the shell version. `kedge
  backup` now fingerprints every volume and DB dump it collects (sha256 over
  a sorted per-file manifest) into `meta.json`'s new `checksums` key. `kedge
  restore` recomputes the same fingerprint on the restored data and hard-fails
  (`KedgeError`) on any mismatch. A snapshot without a `checksums` key (e.g.
  one made by `backup.sh`) is treated as "nothing to verify", not an error —
  restore stays fully cross-compatible in both directions.
- 187 pytest tests, 93% line coverage.

### Fixed (found via live shell/python A-B testing on real Docker stacks + restic repos)
- Hostname resolution: `socket.getfqdn()` can return a garbage reverse-DNS
  name where `hostname -f` returns a clean one — replaced with a
  subprocess-based helper matching the shell's `hostname -f || hostname`.
- `BACKUP_SIZE` hook variable was always "unknown" — modern restic's
  `stats --json` has no `total_size_formatted` field; ported the shell's
  plain-text `restic stats` fallback.

### Verified
- Live cross-tool roundtrip on real Docker stacks + local restic repos:
  `backup.sh` snapshot restored with `kedge restore --verify` (tar-fallback
  volume, MySQL data byte-identical), and `kedge backup` snapshot restored
  with `restore.sh` (full restore incl. compose up, MySQL data byte-identical).
  Proves `meta.json` stays a shared format regardless of which tool wrote or
  read it.

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
