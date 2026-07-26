# Roadmap

Kedge uses named "journeys" as roadmap milestones.

## Reforge — Shell to Python Rewrite (current)

**Goal:** Rewrite Kedge from Shell to Python. Same commands, same behavior, better foundation for integration and testing.

**Status:** Phase 1 + Phase 2 done (**v0.5.0**, 2026-07-26) — full
`backup` → `restore` → `verify` roundtrip verified, including live
bash↔python cross-restore in both directions. This is the "Python
stable" milestone the Migration Path below names. v0.3.x (Shell) stays
maintained in parallel — nothing forces an immediate cutover; a shadow
test on `prod-genua` is the next step before switching any real cron job.

### Why Rewrite?

- YAML parsing (stack.yaml) is fragile in Shell, trivial in Python
- Multi-Compose support requires proper file merging
- Structured JSON output for monitoring and Drayve integration
- Shared toolchain with kigulls-core (Python)
- Testable with pytest instead of manual Bash testing

### Phases

| Phase | What | Target |
|-------|------|--------|
| 1 — Skeleton (folded into v0.5.0) ✅ | Python CLI (click), port discover + backup + DB hooks + pre/post hooks | Drop-in replacement for `backup.sh` — verified via live A/B runs against real Docker stacks + restic repos |
| 2 — Restore + Verify (KEDGE-W-003) ✅ | Port restore and verify commands, add checksum verification | Full roundtrip (backup → restore → verify) — verified live, including bash↔python cross-restore (snapshot made by one tool, restored by the other, both directions) |
| 3 — Integration | Read `stack.yaml`, multi-Compose support, JSON output, status reporting, SFTP auto-provisioning | Kedge as a Drayve-native tool |
| 4 — Monitoring Maturity | Prometheus metrics, cron-based restore tests, Grafana backup dashboard | Backups are monitored and tested, not just made |

### Distribution (decided during Phase 1)

Single-file `shiv` zipapp, not `pip install` or PyInstaller. Keeps the
exact "scp one file to `/usr/local/bin`, `chmod +x`, done" install story
that solo (non-Drayve) hosts rely on today — no venv, no pip on the
target host, just a compatible system Python3 (>=3.10). Works identically
whether the target is a Drayve-managed host (which needs Python3 anyway
for Ansible), a KIgulls host (already Python-heavy but via its own venv
convention that kedge, as a generic public tool, doesn't need to join),
or a plain solo box.

### Migration Path

- **No breaking changes.** Same commands, same env vars, same cron entry.
- Python is now at v0.5.0 ("stable" per this roadmap) — Shell version
  (v0.3.x) keeps running in parallel until real-host cutovers happen one
  at a time (shadow test first, see KEDGE-W-003), not removed on a fixed date.
- Drayve backup role will switch from shell scripts to the Python package.

### Related Issues

| Issue | Phase |
|-------|-------|
| [#1](https://codeberg.org/StephanWaldtmann/kedge/issues/1) Checksum Verify | Phase 2 |
| [#3](https://codeberg.org/StephanWaldtmann/kedge/issues/3) SFTP Provisioning | Phase 3 |
| [#4](https://codeberg.org/StephanWaldtmann/kedge/issues/4) Multi-Compose | Phase 3 |

## DB Engine Registry (KEDGE-W-004) ✅

**Goal:** one descriptor per DB engine instead of the same DB type hand-defined
in up to four places (discovery pattern, dump hook, restore import, verify
healthcheck) — the gap between them was exactly how the MariaDB
mysql/mysqladmin-vs-mariadb/mariadb-admin binary split (KEDGE-W-003) slipped
through: the dump hook got a fallback, restore's import and verify's
healthcheck never did.

**Status:** done. `kedge.engines` is now the single registry consumed by
`discovery.py`/`hooks.py`/`restore.py`/`verify.py`. Postgres/MySQL(+MariaDB)/
Valkey(+Redis)/MongoDB moved onto it unchanged in behavior (238 pytest tests
green, including the moved ones); the MariaDB import/healthcheck gap is
closed as part of the move (regression-tested against a synthetic
mariadb:11-shaped container with no mysql/mysqladmin binaries).

Two new engines added as the registry's first real test of scaling to a
new DB type:
- **SQLite** (`prod-poki`, bind-mount, WAL mode) — no container image to
  auto-discover (SQLite is an embedded library inside poki's own app image),
  so no `image_patterns`; instead a `SQLITE_WAL_CHECKPOINT_PATHS` env var
  (config.py, same shape as the existing `SYSTEM_PATHS`) runs a WAL
  checkpoint (`PRAGMA wal_checkpoint(TRUNCATE)`, real stdlib `sqlite3`, no
  external binary) before the bind-mount gets tarred like any other external
  mount — makes the plain `.db` file standalone-consistent without depending
  on capturing the `-wal`/`-shm` sidecar files atomically.
- **InfluxDB** (`prod-multi01`, real fleet version 2.6 per `/Users/sw/claudes-welt/cowork/servers.yaml`)
  — full v2 dump/restore (`influx backup`/`influx restore --full`, token from
  `DOCKER_INFLUXDB_INIT_ADMIN_TOKEN`/`INFLUX_TOKEN`), verified live against
  real `influxdb:2.7` containers (backup one instance, restore into a
  completely fresh one, data byte-identical). v1 (`influxd backup -portable`)
  is dump-only — v1 OSS restore needs the target stopped with a clean data
  dir first, which doesn't fit this integration's "docker-exec while
  running" shape for any engine, so v1 import raises a clear error instead
  of silently doing the wrong thing.

Backlog, explicitly not done here (registry design proven to scale with two
real new engines first — Elasticsearch/OpenSearch and ClickHouse are a
separate follow-up, not blocked on anything technical here):

| Issue | Note |
|-------|------|
| Elasticsearch/OpenSearch | Real (Zammad, currently disabled) |
| ClickHouse | Growing fleet presence H1/2026 |

## Not Planned

| Topic | Reason |
|-------|--------|
| Kubernetes support | Deliberately Docker Compose only |
| Web UI | CLI tool; dashboards come from Drayve |
| Custom scheduler | Cron is sufficient; Drayve deploys the cron job |
| Replace restic | restic is excellent, no reason to switch |
