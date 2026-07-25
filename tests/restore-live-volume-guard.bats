#!/usr/bin/env bats
# Tests fuer restore.sh -- CW-W-243: --verify darf niemals in ein lebendes
# Docker-Volume schreiben, ein echter Restore ohne --force-live muss sich
# weigern, ein bereits von einem laufenden Container gemountetes Volume zu
# ueberschreiben.
#
# Echter Docker + echter lokaler restic-Repo (kein Mock), analog
# backup-system-paths.bats. Braucht einen echten Linux-Docker-Host, weil
# restore.sh per rsync direkt in den von `docker volume inspect
# --format {{.Mountpoint}}` gemeldeten Host-Pfad schreibt -- auf Docker
# Desktop (macOS) ist dieser Pfad nicht vom Host aus erreichbar. Tests
# skippen sich selbst, wenn kein (Linux-)Docker verfuegbar ist.

setup() {
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    skip "docker not available"
  fi
  docker volume create cw243-probe >/dev/null
  PROBE_MP="$(docker volume inspect --format '{{.Mountpoint}}' cw243-probe)"
  if [[ ! -d "$PROBE_MP" ]]; then
    docker volume rm cw243-probe >/dev/null
    skip "Docker volume Mountpoint vom Host aus nicht erreichbar (z.B. Docker Desktop/macOS) -- Test braucht echten Linux-Docker-Host"
  fi
  docker volume rm cw243-probe >/dev/null

  SUITE="${BATS_TEST_DIRNAME}/../restore.sh"
  export RESTIC_REPOSITORY="${BATS_TEST_TMPDIR}/repo"
  export RESTIC_PASSWORD="bats-test-password"
  export RESTORE_TARGET="${BATS_TEST_TMPDIR}/target"
  source "$SUITE"

  REAL_VOL="cw243_data"
  STACK="teststack"

  # Aufraeumen von evtl. Vorlauf-Leichen aus einem abgebrochenen Lauf.
  docker rm -f cw243-live-container >/dev/null 2>&1 || true
  docker volume rm "$REAL_VOL" "${REAL_VOL}_restoretest" >/dev/null 2>&1 || true

  # Das "lebende" Volume -- so wie es auf dem echten Backup-Quell-Host aussieht:
  # existiert bereits, ist bei einem laufenden Container gemountet, traegt
  # Inhalt der NICHT ueberschrieben werden darf.
  docker volume create "$REAL_VOL" >/dev/null
  LIVE_MP="$(docker volume inspect --format '{{.Mountpoint}}' "$REAL_VOL")"
  echo "live-data-must-survive" > "$LIVE_MP/canary.txt"
  docker run -d --name cw243-live-container -v "$REAL_VOL":/data alpine sleep 300 >/dev/null

  # Fixture-Backup bauen: ein restic-Snapshot mit demselben Layout, das
  # backup.sh produziert -- meta.json unter staging/<stack>/, volume_mapping
  # zeigt auf REAL_VOL (Normalfall bei einem Backup aus dem laufenden Stack),
  # volume_paths zeigt auf einen Fixture-Pfad mit dem zu restaurierenden
  # Inhalt (entkoppelt von echten Docker-Mountpoint-Pfaden -- restore.sh matcht
  # nur den Pfad-Suffix, nicht den echten Host-Pfad).
  BACKUP_SRC="${BATS_TEST_TMPDIR}/backup-src"
  mkdir -p "$BACKUP_SRC/staging/$STACK/fixture-vol"
  echo "restored-content" > "$BACKUP_SRC/staging/$STACK/fixture-vol/data.txt"
  cat > "$BACKUP_SRC/staging/$STACK/meta.json" <<EOF
{
  "stack_dir": "/opt/$STACK",
  "volume_mapping": {"data": "$REAL_VOL"},
  "volume_paths": {"data": "/staging/$STACK/fixture-vol"}
}
EOF

  restic init >/dev/null
  restic backup "$BACKUP_SRC" --tag kedge >/dev/null
}

teardown() {
  command -v docker >/dev/null 2>&1 || return 0
  docker rm -f cw243-live-container >/dev/null 2>&1 || true
  docker volume rm cw243_data cw243_data_restoretest >/dev/null 2>&1 || true
}

@test "--verify restauriert unter isoliertem Namen, lebendes Volume bleibt unangetastet" {
  run cmd_restore --verify
  [ "$status" -eq 0 ]
  restore_output="$output"

  # Der lebende Canary-Inhalt muss unveraendert sein.
  [ "$(cat "$LIVE_MP/canary.txt")" = "live-data-must-survive" ]

  # Restauriert wurde unter dem isolierten Namen, mit dem Backup-Inhalt.
  run docker volume inspect "${REAL_VOL}_restoretest"
  [ "$status" -eq 0 ]
  restoretest_mp="$(docker volume inspect --format '{{.Mountpoint}}' "${REAL_VOL}_restoretest")"
  [ "$(cat "$restoretest_mp/data.txt")" = "restored-content" ]

  [[ "$restore_output" == *"restoretest"* ]]
  [[ "$restore_output" == *"no live volume was touched"* ]]
}

@test "echter Restore ohne --force-live bricht ab, wenn das Ziel-Volume schon lebt" {
  run cmd_restore
  [ "$status" -ne 0 ]
  [[ "$output" == *"Refusing to overwrite live data"* ]]
  [[ "$output" == *"--force-live"* ]]

  # Canary unveraendert -- der Abbruch muss VOR dem rsync passiert sein.
  [ "$(cat "$LIVE_MP/canary.txt")" = "live-data-must-survive" ]
}

@test "echter Restore MIT --force-live ueberschreibt das lebende Volume bewusst" {
  run cmd_restore --force-live
  [ "$status" -eq 0 ]
  [[ "$output" == *"--force-live set: overwriting"* ]]
  [ "$(cat "$LIVE_MP/data.txt")" = "restored-content" ]
}

@test "--verify auf einem frischen Host (Volume existiert noch nicht) funktioniert unveraendert" {
  docker rm -f cw243-live-container >/dev/null 2>&1
  docker volume rm "$REAL_VOL" >/dev/null 2>&1

  run cmd_restore --verify
  [ "$status" -eq 0 ]
  restoretest_mp="$(docker volume inspect --format '{{.Mountpoint}}' "${REAL_VOL}_restoretest")"
  [ "$(cat "$restoretest_mp/data.txt")" = "restored-content" ]
}
