#!/usr/bin/env bats
# Tests fuer backup.sh -- SYSTEM_PATHS / SYSTEM_PATHS_EXCLUDE (AFKI-W-157 Vorbereitung).
#
# Echter lokaler restic-Repo (kein Argv-Mock), analog backup-prune.bats: der Beweis
# ist, dass restic die zusaetzlichen Pfade tatsaechlich im Snapshot hat -- nicht nur
# dass backup.sh die Flags richtig zusammenbaut.

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../backup.sh"
  export RESTIC_REPOSITORY="${BATS_TEST_TMPDIR}/repo"
  export RESTIC_PASSWORD="bats-test-password"
  export BACKUP_STOP_STACK=false
  export STACK_DIR="${BATS_TEST_TMPDIR}/stack"
  # Default staging base is /var/lib/kedge (root-only on the real deploy target) --
  # redirect into the test tmpdir so this runs unprivileged too.
  export KEDGE_STAGING_BASE="${BATS_TEST_TMPDIR}/staging"
  mkdir -p "$STACK_DIR"
  source "$SUITE"

  # Leerer Stack -- kein Docker noetig fuer diesen Test, nur die
  # SYSTEM_PATHS-Mechanik in cmd_backup wird geprueft.
  check_prereqs() { :; }
  COMPOSE_CMD="mock_compose"
  mock_compose() { echo '{}'; }
  export -f mock_compose

  restic init >/dev/null

  SP_DIR="${BATS_TEST_TMPDIR}/system"
  mkdir -p "$SP_DIR/etc" "$SP_DIR/var-log"
  echo "system-config-content" > "$SP_DIR/etc/kept.conf"
  echo "noisy-log-content" > "$SP_DIR/var-log/app.log"
}

_restored_files() {
  local snap="$1"
  restic ls "$snap" --json 2>/dev/null | jq -r 'select(.type=="file") | .path'
}

@test "SYSTEM_PATHS: konfigurierter Pfad landet im Snapshot" {
  export SYSTEM_PATHS="$SP_DIR/etc"
  run cmd_backup
  [ "$status" -eq 0 ]

  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  files="$(_restored_files "$snap")"
  [[ "$files" == *"kept.conf"* ]]
}

@test "SYSTEM_PATHS: mehrere Pfade, einer davon fehlt -- Backup laeuft trotzdem durch, warnt nur" {
  export SYSTEM_PATHS="$SP_DIR/etc $SP_DIR/does-not-exist"
  run cmd_backup
  [ "$status" -eq 0 ]
  [[ "$output" == *"SYSTEM_PATHS entry not found"*"does-not-exist"* ]]

  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  files="$(_restored_files "$snap")"
  [[ "$files" == *"kept.conf"* ]]
}

@test "SYSTEM_PATHS_EXCLUDE: gematchter Pfad taucht nicht im Snapshot auf" {
  export SYSTEM_PATHS="$SP_DIR/etc $SP_DIR/var-log"
  export SYSTEM_PATHS_EXCLUDE="*/var-log/*"
  run cmd_backup
  [ "$status" -eq 0 ]

  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  files="$(_restored_files "$snap")"
  [[ "$files" == *"kept.conf"* ]]
  [[ "$files" != *"app.log"* ]]
}

@test "SYSTEM_PATHS unset (Default): kein zusaetzlicher Pfad, Backup laeuft wie zuvor" {
  run cmd_backup
  [ "$status" -eq 0 ]
  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  files="$(_restored_files "$snap")"
  [[ "$files" != *"kept.conf"* ]]
}

@test "cmd_discover zeigt konfigurierte SYSTEM_PATHS inkl. NOT-FOUND-Markierung" {
  export SYSTEM_PATHS="$SP_DIR/etc $SP_DIR/does-not-exist"
  export SYSTEM_PATHS_EXCLUDE="*/var-log/*"
  run cmd_discover
  [ "$status" -eq 0 ]
  [[ "$output" == *"$SP_DIR/etc  [exists]"* ]]
  [[ "$output" == *"$SP_DIR/does-not-exist  [NOT FOUND]"* ]]
  [[ "$output" == *"excludes: */var-log/*"* ]]
}

@test "cmd_discover ohne SYSTEM_PATHS zeigt (none configured)" {
  run cmd_discover
  [ "$status" -eq 0 ]
  [[ "$output" == *"--- System Paths ---"*"(none configured)"* ]]
}
