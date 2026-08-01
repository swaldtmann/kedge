#!/usr/bin/env bats
# Tests fuer restore.sh -- CW-W-258: externe Bind-Mounts, die backup.sh jetzt
# als Direct-Path sichert (statt tar.gz), muessen beim Restore wieder an ihrem
# urspruenglichen absoluten Pfad landen -- UND alte, noch tar.gz-basierte
# Snapshots (vor dem Fix) muessen weiterhin restorable bleiben.
#
# Echter lokaler restic-Repo, Fixture-Snapshot von Hand gebaut -- analog
# restore-live-volume-guard.bats. Kein Docker noetig (Bind-Mount-Restore
# beruehrt keine Docker-Volumes), check_prereqs gestubbt wie in den
# backup.sh-Tests.

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../restore.sh"
  export RESTIC_REPOSITORY="${BATS_TEST_TMPDIR}/repo"
  export RESTIC_PASSWORD="bats-test-password"
  export RESTORE_TARGET="${BATS_TEST_TMPDIR}/target"
  source "$SUITE"

  check_prereqs() { :; }
  STACK="teststack"

  restic init >/dev/null
}

@test "direktes Format (bind_mount_paths in meta.json): Mount landet wieder am Original-Pfad" {
  BACKUP_SRC="${BATS_TEST_TMPDIR}/backup-src"
  MOUNT_SRC_DIR="${BATS_TEST_TMPDIR}/orig-mount"
  mkdir -p "$BACKUP_SRC/staging/$STACK" "$MOUNT_SRC_DIR"
  echo "restored-mail-content" > "$MOUNT_SRC_DIR/msg1.eml"

  cat > "$BACKUP_SRC/staging/$STACK/meta.json" <<EOF
{
  "stack_dir": "/opt/$STACK",
  "volume_mapping": {},
  "volume_paths": {},
  "bind_mount_paths": ["$MOUNT_SRC_DIR"]
}
EOF

  restic backup "$BACKUP_SRC" "$MOUNT_SRC_DIR" --tag kedge >/dev/null

  # Datenverlust auf dem "Original-Host" simulieren -- das ist der Fall, den
  # Restore beheben muss.
  rm -rf "$MOUNT_SRC_DIR"
  [ ! -e "$MOUNT_SRC_DIR" ]

  run cmd_restore --verify
  [ "$status" -eq 0 ]
  [[ "$output" == *"$MOUNT_SRC_DIR"*"[direct]"* ]]

  [ -f "$MOUNT_SRC_DIR/msg1.eml" ]
  [ "$(cat "$MOUNT_SRC_DIR/msg1.eml")" = "restored-mail-content" ]
}

@test "Legacy-Format (external-mounts/*.tar.gz, pre-CW-W-258): bleibt restorable" {
  BACKUP_SRC="${BATS_TEST_TMPDIR}/backup-src"
  MOUNT_SRC_DIR="${BATS_TEST_TMPDIR}/legacy-mount"
  mkdir -p "$BACKUP_SRC/staging/$STACK/external-mounts" "$MOUNT_SRC_DIR"
  echo "legacy-restored-content" > "$MOUNT_SRC_DIR/msg1.eml"

  # Encoded Name exakt wie die ALTE collect_stack_files ihn gebaut hat:
  # tr '/' '_' | sed 's/^_//' auf dem absoluten Pfad.
  mount_name="$(echo "$MOUNT_SRC_DIR" | tr '/' '_' | sed 's/^_//')"
  tar czf "$BACKUP_SRC/staging/$STACK/external-mounts/${mount_name}.tar.gz" \
    -C "$(dirname "$MOUNT_SRC_DIR")" "$(basename "$MOUNT_SRC_DIR")"

  cat > "$BACKUP_SRC/staging/$STACK/meta.json" <<EOF
{
  "stack_dir": "/opt/$STACK",
  "volume_mapping": {},
  "volume_paths": {}
}
EOF

  restic backup "$BACKUP_SRC" --tag kedge >/dev/null

  rm -rf "$MOUNT_SRC_DIR"
  [ ! -e "$MOUNT_SRC_DIR" ]

  run cmd_restore --verify
  [ "$status" -eq 0 ]
  [[ "$output" == *"legacy tar.gz"* ]]

  [ -f "$MOUNT_SRC_DIR/msg1.eml" ]
  [ "$(cat "$MOUNT_SRC_DIR/msg1.eml")" = "legacy-restored-content" ]
}

@test "kein bind_mount_paths und kein external-mounts-Verzeichnis: meldet 'nichts zu restaurieren'" {
  BACKUP_SRC="${BATS_TEST_TMPDIR}/backup-src"
  mkdir -p "$BACKUP_SRC/staging/$STACK"
  cat > "$BACKUP_SRC/staging/$STACK/meta.json" <<EOF
{"stack_dir": "/opt/$STACK", "volume_mapping": {}, "volume_paths": {}}
EOF
  restic backup "$BACKUP_SRC" --tag kedge >/dev/null

  run cmd_restore --verify
  [ "$status" -eq 0 ]
  [[ "$output" == *"No external bind mounts to restore"* ]]
}

@test "bind_mount_paths verweist auf einen im Snapshot fehlenden Pfad: warnt, bricht nicht ab" {
  BACKUP_SRC="${BATS_TEST_TMPDIR}/backup-src"
  mkdir -p "$BACKUP_SRC/staging/$STACK"
  cat > "$BACKUP_SRC/staging/$STACK/meta.json" <<EOF
{
  "stack_dir": "/opt/$STACK",
  "volume_mapping": {},
  "volume_paths": {},
  "bind_mount_paths": ["${BATS_TEST_TMPDIR}/never-backed-up"]
}
EOF
  restic backup "$BACKUP_SRC" --tag kedge >/dev/null

  run cmd_restore --verify
  [ "$status" -eq 0 ]
  [[ "$output" == *"Bind mount not found in snapshot"* ]]
}
