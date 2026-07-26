#!/usr/bin/env bats
# Tests fuer verify.sh -- ssh_box Argv-Injection (Brieftaube 26.07., E1 Item 9):
# ssh haengt mehrere Trailing-Argv-Elemente (das Remote-Kommando + dessen
# Argumente) unquoted mit einfachen Leerzeichen aneinander und uebergibt den
# resultierenden String der Remote-Login-Shell zur Neu-Interpretation, BEVOR
# `bash -s` je $1..$N sieht. Ein `$(...)`/Backtick in z.B. einem restic-
# Passwort wurde dort remote expandiert/ausgefuehrt statt literal anzukommen
# -- betraf verify.sh:485 (restic_pass_arg) und test.sh (BACKUP_PASSWORD),
# war vor dem ersten Live-Restore-Test in beiden latent.
#
# Mock-ssh statt echtem Host: repliziert exakt den Reparse-Schritt eines
# echten sshd (mehrere Trailing-Argv-Elemente IFS-space-join, dann durch
# `bash -c` jagen) -- kein Hetzner-Host noetig, beweist trotzdem die reale
# ssh-Mechanik statt nur "wir haben den String selbst richtig hingeschrieben".

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../verify.sh"

  MOCKBIN="${BATS_TEST_TMPDIR}/mockbin"
  mkdir -p "$MOCKBIN"
  cat > "$MOCKBIN/ssh" <<'MOCK'
#!/usr/bin/env bash
# Findet die Destination (erstes Argv-Element mit '@', z.B. root@10.0.0.9);
# alles danach ist das Remote-Kommando. Realer ssh-Client haengt diese
# Trailing-Elemente unquoted mit Leerzeichen aneinander und schickt das der
# Login-Shell des Remote-Hosts -- exakt das simulieren wir hier.
args=("$@")
dest_idx=-1
for i in "${!args[@]}"; do
  case "${args[$i]}" in
    *@*) dest_idx=$i; break ;;
  esac
done
cmd_args=("${args[@]:$((dest_idx+1))}")
joined="${cmd_args[*]}"
exec bash -c "$joined"
MOCK
  chmod +x "$MOCKBIN/ssh"
  export PATH="$MOCKBIN:$PATH"

  source "$SUITE"
}

@test "ssh_box: Passwort mit \$(...) und Backticks kommt beim Remote-Reparse literal an" {
  injection='pre$(whoami)mid`id`post'
  run ssh_box "10.0.0.9" bash -s -- "repo-path" "$injection" "target" "snap1" <<'REMOTE'
set -euo pipefail
printf 'REPO=%s\n' "$1"
printf 'PASS=%s\n' "$2"
printf 'TARGET=%s\n' "$3"
printf 'SNAP=%s\n' "$4"
REMOTE

  [ "$status" -eq 0 ]
  [[ "$output" == *"REPO=repo-path"* ]]
  [[ "$output" == *"PASS=${injection}"* ]]
  [[ "$output" == *"TARGET=target"* ]]
  [[ "$output" == *"SNAP=snap1"* ]]
}

@test "ssh_box: einzelnes Kommando-Argument geht unveraendert durch (kein Doppel-Quoting)" {
  run ssh_box "10.0.0.9" "echo plain-output"
  [ "$status" -eq 0 ]
  [ "$output" = "plain-output" ]
}

@test "ssh_box: Semikolon im Passwort haengt keinen Zweitbefehl an" {
  injection='pw; echo PWNED'
  run ssh_box "10.0.0.9" bash -s -- "repo" "$injection" "target" "snap1" <<'REMOTE'
printf 'PASS=%s\n' "$2"
REMOTE

  # Der Marker "PWNED" gehoert bereits legitim zum Passwort-Text (Teil von
  # "; echo PWNED") -- ein `;` das als Befehlstrenner durchschlaegt zeigt sich
  # nicht am Fehlen des Markers, sondern an einer ZUSAETZLICHEN Zeile/Ausgabe
  # vor der eigentlichen PASS-Zeile. Deshalb exakter Ein-Zeilen-Vergleich.
  [ "$status" -eq 0 ]
  [ "$output" = "PASS=${injection}" ]
}
