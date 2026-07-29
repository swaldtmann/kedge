#!/usr/bin/env bats
# Tests fuer tools/backup-freshness-write (DRAYVE-W-014).

setup() {
  SCRIPT="${BATS_TEST_DIRNAME}/../tools/backup-freshness-write"
  export BACKUP_FRESHNESS_DIR="${BATS_TEST_TMPDIR}/textfile"
  mkdir -p "$BACKUP_FRESHNESS_DIR"
}

@test "writes a kedge_backup_last_success .prom file with the given repo label" {
  run "$SCRIPT" "myrepo"
  [ "$status" -eq 0 ]

  file="${BACKUP_FRESHNESS_DIR}/kedge_backup_myrepo.prom"
  [ -f "$file" ]

  content="$(cat "$file")"
  [[ "$content" =~ ^kedge_backup_last_success\{repo=\"myrepo\"\}\ [0-9]+$ ]]
}

@test "does not leak a cloud_-prefixed metric name" {
  run "$SCRIPT" "myrepo"
  [ "$status" -eq 0 ]
  content="$(cat "${BACKUP_FRESHNESS_DIR}/kedge_backup_myrepo.prom")"
  [[ "$content" != *cloud_* ]]
}

@test "overwrites a stale value on a second run" {
  "$SCRIPT" "myrepo"
  first="$(cat "${BACKUP_FRESHNESS_DIR}/kedge_backup_myrepo.prom")"
  sleep 1
  "$SCRIPT" "myrepo"
  second="$(cat "${BACKUP_FRESHNESS_DIR}/kedge_backup_myrepo.prom")"
  [ "$first" != "$second" ]
}
