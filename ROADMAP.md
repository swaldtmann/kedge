# Roadmap

Kedge uses named "journeys" as roadmap milestones.

## Reforge — Shell to Python Rewrite (current)

**Goal:** Rewrite Kedge from Shell to Python. Same commands, same behavior, better foundation for integration and testing.

**Status:** Planning — v0.3.0 (Shell) is stable in production.

### Why Rewrite?

- YAML parsing (stack.yaml) is fragile in Shell, trivial in Python
- Multi-Compose support requires proper file merging
- Structured JSON output for monitoring and Drayve integration
- Shared toolchain with kigulls-core (Python)
- Testable with pytest instead of manual Bash testing

### Phases

| Phase | What | Target |
|-------|------|--------|
| 1 — Skeleton (v0.4.0) | Python CLI (click), port discover + backup + DB hooks + pre/post hooks | Drop-in replacement for `backup.sh` |
| 2 — Restore + Verify | Port restore and verify commands, add checksum verification | Full roundtrip (backup → restore → verify) |
| 3 — Integration | Read `stack.yaml`, multi-Compose support, JSON output, status reporting, SFTP auto-provisioning | Kedge as a Drayve-native tool |
| 4 — Monitoring Maturity | Prometheus metrics, cron-based restore tests, Grafana backup dashboard | Backups are monitored and tested, not just made |

### Migration Path

- **No breaking changes.** Same commands, same env vars, same cron entry.
- Shell version (v0.3.x) will be maintained until Python v0.5.0 is stable.
- Drayve backup role will switch from shell scripts to Python package.

### Related Issues

| Issue | Phase |
|-------|-------|
| [#1](https://codeberg.org/StephanWaldtmann/kedge/issues/1) Checksum Verify | Phase 2 |
| [#3](https://codeberg.org/StephanWaldtmann/kedge/issues/3) SFTP Provisioning | Phase 3 |
| [#4](https://codeberg.org/StephanWaldtmann/kedge/issues/4) Multi-Compose | Phase 3 |

## Not Planned

| Topic | Reason |
|-------|--------|
| Kubernetes support | Deliberately Docker Compose only |
| Web UI | CLI tool; dashboards come from Drayve |
| Custom scheduler | Cron is sufficient; Drayve deploys the cron job |
| Replace restic | restic is excellent, no reason to switch |
