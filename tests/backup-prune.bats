#!/usr/bin/env bats
# Tests fuer backup.sh -- Fokus: cmd_prune --group-by-Fix (EWH-W-135).
#
# Nutzt einen echten lokalen restic-Repo (kein Mock) -- reine Argv-Mocks wuerden
# nur beweisen dass wir den Flag-String selbst hingeschrieben haben, nicht dass
# restic damit tatsaechlich gruppiert. Der Bug + der Fix leben in restics
# Gruppierungslogik, also muss der Test die auch wirklich durchlaufen.

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../backup.sh"
  export RESTIC_REPOSITORY="${BATS_TEST_TMPDIR}/repo"
  export RESTIC_PASSWORD="bats-test-password"
  export BACKUP_KEEP_DAILY=7
  export BACKUP_KEEP_WEEKLY=4
  export BACKUP_KEEP_MONTHLY=3
  source "$SUITE"
  # check_prereqs will look for docker/compose-file -- irrelevant fuer einen
  # reinen Prune-Test, deshalb nach dem Sourcen als No-Op ueberschrieben.
  check_prereqs() { :; }
  restic init >/dev/null
}

_backup_unique_path() {
  # Simuliert den Ur-Bug: jeder Lauf sichert einen EIGENEN, einmaligen Pfad
  # (frueher /tmp/dsb-staging.XXXXXX per mktemp), aber mit denselben Tags,
  # die kedge auch real setzt (kedge + stack:<name>).
  dir="$(mktemp -d "${BATS_TEST_TMPDIR}/staging.XXXXXX")"
  echo "content-$$-$RANDOM" > "$dir/file.txt"
  restic backup "$dir" --tag "kedge" --tag "stack:test" >/dev/null
}

@test "cmd_prune reduziert mehrere gleich-getaggte Snapshots (unterschiedliche Pfade) auf die keep-Policy" {
  _backup_unique_path
  _backup_unique_path
  _backup_unique_path
  before="$(restic snapshots --tag kedge --json | jq 'length')"
  [ "$before" -eq 3 ]

  run cmd_prune
  [ "$status" -eq 0 ]

  after="$(restic snapshots --tag kedge --json | jq 'length')"
  # Alle 3 Snapshots sind "heute", gleiche Tags. restic haelt den neuesten als
  # "daily" UND den aeltesten zusaetzlich als Anker fuer "oldest weekly/monthly"
  # (kein anderer Kandidat in der Gruppe) -> 2 bleiben, 1 (der mittlere) geht.
  # Das ist die eigentliche Beweisstelle: eine REDUKTION findet ueberhaupt statt.
  # UND NUR WEIL --group-by tags alle drei als EINE Gruppe behandelt. Ohne den
  # Fix waere jeder sein eigenes Grueppchen von 1 (Default-Gruppierung
  # host+paths, Pfade sind hier bewusst unterschiedlich) -> after waere
  # weiterhin 3, der exakte Ur-Bug aus EWH-W-135 ("Prune complete", 0 geloescht).
  [ "$after" -eq 2 ]
}

@test "cmd_prune laesst eine einzelne Gruppe unangetastet, wenn sie die keep-Policy nicht ueberschreitet" {
  _backup_unique_path
  run cmd_prune
  [ "$status" -eq 0 ]
  after="$(restic snapshots --tag kedge --json | jq 'length')"
  [ "$after" -eq 1 ]
}

@test "cmd_prune ruft restic forget mit --group-by tags auf (Regressions-Schutz gegen erneutes Entfernen)" {
  grep -q -- '--group-by tags' "$SUITE"
}

@test "Kontrolle: ohne --group-by (Ur-Bug-Verhalten) wird bei unterschiedlichen Pfaden NICHTS entfernt" {
  _backup_unique_path
  _backup_unique_path
  _backup_unique_path
  before="$(restic snapshots --tag kedge --json | jq 'length')"
  [ "$before" -eq 3 ]

  # Exakt der Aufruf aus cmd_prune VOR dem EWH-W-135-Fix -- Default-Gruppierung
  # (host,paths), jeder Snapshot hat einen eigenen Pfad -> jeder ist seine
  # eigene Gruppe von 1 -> keep-daily 7 haelt jede Ein-Snapshot-Gruppe komplett.
  run restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 3 --tag kedge --prune
  [ "$status" -eq 0 ]

  after="$(restic snapshots --tag kedge --json | jq 'length')"
  [ "$after" -eq 3 ]
}
