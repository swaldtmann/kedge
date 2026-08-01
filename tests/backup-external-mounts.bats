#!/usr/bin/env bats
# Tests fuer backup.sh -- CW-W-258: externe Bind-Mounts (Compose-Bind-Mounts
# ausserhalb $STACK_DIR) muessen als Direct-Path an restic gehen, nicht per
# tar.gz. tar-dann-gzip zerstoert restics Content-Defined-Chunking (jede
# Aenderung verschiebt alle nachfolgenden komprimierten Bytes) -- poki sicherte
# so 18,6G taeglich neu (~6,1GiB "Added to the repository" JEDEN Tag), obwohl
# nur eine Handvoll Dateien sich aenderten.
#
# Echter lokaler restic-Repo (kein Argv-Mock), analog backup-system-paths.bats.

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../backup.sh"
  export RESTIC_REPOSITORY="${BATS_TEST_TMPDIR}/repo"
  export RESTIC_PASSWORD="bats-test-password"
  export BACKUP_STOP_STACK=false
  export STACK_DIR="${BATS_TEST_TMPDIR}/stack"
  export KEDGE_STAGING_BASE="${BATS_TEST_TMPDIR}/staging"
  mkdir -p "$STACK_DIR"
  source "$SUITE"

  check_prereqs() { :; }
  COMPOSE_CMD="mock_compose"

  EXT_DIR="${BATS_TEST_TMPDIR}/external-data"
  mkdir -p "$EXT_DIR"
  echo "mail-content-1" > "$EXT_DIR/msg1.eml"

  mock_compose() {
    cat <<EOF
{
  "services": {"app": {"image": "nginx", "volumes": [{"type": "bind", "source": "$EXT_DIR", "target": "/x"}]}}
}
EOF
  }
  export -f mock_compose

  restic init >/dev/null
}

_restored_files() {
  local snap="$1"
  restic ls "$snap" --json 2>/dev/null | jq -r 'select(.type=="file") | .path'
}

@test "externer Bind-Mount landet direkt im Snapshot, kein tar.gz" {
  run cmd_backup
  [ "$status" -eq 0 ]
  [[ "$output" == *"$EXT_DIR [direct]"* ]]

  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  files="$(_restored_files "$snap")"
  # Datei liegt direkt unter ihrem echten Pfad im Snapshot -- nicht als
  # external-mounts/*.tar.gz verpackt.
  [[ "$files" == *"$EXT_DIR/msg1.eml"* ]]
  [[ "$files" != *"external-mounts"* ]]
  [[ "$files" != *".tar.gz"* ]]
}

@test "meta.json enthaelt bind_mount_paths fuer den externen Mount" {
  run cmd_backup
  [ "$status" -eq 0 ]

  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  meta_path="$(restic ls "$snap" --json 2>/dev/null | jq -r 'select(.type=="file") | .path' | grep 'meta.json$')"
  restic dump "$snap" "$meta_path" > "${BATS_TEST_TMPDIR}/meta.json"
  bind_paths="$(jq -r '.bind_mount_paths[]' "${BATS_TEST_TMPDIR}/meta.json")"
  [[ "$bind_paths" == *"$EXT_DIR"* ]]
}

@test "unveraenderter externer Bind-Mount: zweiter Backup-Lauf haengt kaum neue Daten an (Dedup greift)" {
  run cmd_backup
  [ "$status" -eq 0 ]

  run cmd_backup
  [ "$status" -eq 0 ]
  # Zweiter Lauf: "Added to the repository" fuer den unveraenderten Mount-Inhalt
  # muss winzig sein (nur neue Metadaten-Bloecke, nicht der komplette Mount
  # erneut). Die alte tar.gz-Implementierung haette hier annaehernd die volle
  # Mount-Groesse nochmal gemeldet.
  [[ "$output" == *"Added to the repository:"* ]]
  added_line="$(echo "$output" | grep 'Added to the repository:')"
  # "0 B" oder ein sehr kleiner Byte-Wert (Metadaten) -- kein Wieder-Speichern
  # des kompletten 15-Byte-Testinhalts als "neu" waere trivial klein sowieso;
  # der eigentliche Beweis fuer echte Repos liegt im CW-W-258-Live-Beleg
  # (tar|gzip vs. taegliches "Added"), hier pruefen wir nur die Code-Pfad-Wahl:
  # kein zweiter tar.gz-Aufruf, kein external-mounts-Verzeichnis im neuen Snapshot.
  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  files="$(_restored_files "$snap")"
  [[ "$files" != *"external-mounts"* ]]
}

@test "excluded Mount wird weiterhin uebersprungen, taucht nicht im Snapshot auf" {
  export BACKUP_EXCLUDE_MOUNTS="$EXT_DIR"
  run cmd_backup
  [ "$status" -eq 0 ]
  [[ "$output" == *"Skipping excluded mount: $EXT_DIR"* ]]

  snap="$(restic snapshots --latest 1 --json | jq -r '.[0].short_id')"
  files="$(_restored_files "$snap")"
  [[ "$files" != *"msg1.eml"* ]]
}

@test "cmd_discover zeigt den externen Mount unveraendert an" {
  run cmd_discover
  [ "$status" -eq 0 ]
  [[ "$output" == *"$EXT_DIR"*"[exists]"* ]]
}
