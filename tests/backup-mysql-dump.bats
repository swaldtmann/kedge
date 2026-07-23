#!/usr/bin/env bats
# Tests fuer backup.sh -- MySQL/MariaDB-Dump-Passwort-Discovery (KEDGE-W-002).
#
# `docker`/`$COMPOSE_CMD` werden als Bash-Funktionen gemockt (kein echter
# Container noetig) -- der Bug lebt in backup.shs eigener Env-Var-Erkennung +
# fehlendem Exit-Code-Check, nicht in mysqldump/docker selbst.

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../backup.sh"
  source "$SUITE"
  check_prereqs() { :; }

  STACK_DIR="${BATS_TEST_TMPDIR}"
  DUMP_DIR="${BATS_TEST_TMPDIR}/dump"
  MOCK_ENV=()
  MOCK_DUMP_EXIT=0

  COMPOSE_CMD="mock_compose"
  mock_compose() { echo "fake-container-id"; }

  # Ueberschreibt das echte `docker` fuer den Testlauf.
  docker() {
    if [[ "$1" == "inspect" ]]; then
      case "$3" in
        '{{.Name}}') echo "/fake-container" ;;
        '{{range .Config.Env}}{{println .}}{{end}}')
          local line
          for line in "${MOCK_ENV[@]}"; do echo "$line"; done
          ;;
        *) echo "" ;;
      esac
      return 0
    fi
    if [[ "$1" == "exec" ]]; then
      shift
      local joined=" $* "
      if [[ "$joined" == *" which mariadb-dump "* ]]; then
        return 1   # nicht vorhanden -> Fallback auf mysqldump
      fi
      if [[ "$joined" == *" mysqldump "* || "$joined" == *" mariadb-dump "* ]]; then
        if [[ "$MOCK_DUMP_EXIT" -eq 0 ]]; then
          echo "-- fake sql dump --"
        fi
        return "$MOCK_DUMP_EXIT"
      fi
      return 0
    fi
    return 0
  }
  export -f docker mock_compose

  config_json() {
    printf '{"services": {"db": {"image": "%s"}}}' "$1"
  }
}

@test "KEDGE-W-002: Passwort ueber Standard-Var (MYSQL_ROOT_PASSWORD) gefunden -> Dump laeuft durch" {
  MOCK_ENV=("MYSQL_ROOT_PASSWORD=geheim123")
  run run_pre_hooks "$(config_json mariadb:11)" "$DUMP_DIR"
  [ "$status" -eq 0 ]
  [[ "$output" == *" ok "*"MySQL dump"* ]]
  [ -f "$DUMP_DIR/db_mysql.sql.gz" ]
}

@test "KEDGE-W-002: Passwort ueber Mailcow-Var DBROOT gefunden -> Dump laeuft durch (vorher: silent unauthenticated dump)" {
  MOCK_ENV=("DBROOT=mailcow-geheim")
  run run_pre_hooks "$(config_json mariadb:10.6)" "$DUMP_DIR"
  [ "$status" -eq 0 ]
  [[ "$output" == *" ok "*"MySQL dump"* ]]
  [ -f "$DUMP_DIR/db_mysql.sql.gz" ]
}

@test "KEDGE-W-002: keine bekannte Passwort-Var gefunden -> harter Fehlschlag, kein 'ok'-Log, kein leerer Dump" {
  MOCK_ENV=("SOME_OTHER_VAR=unrelated" "DB_NAME=mailcow")
  run run_pre_hooks "$(config_json mariadb:11)" "$DUMP_DIR"
  [ "$status" -ne 0 ]
  [[ "$output" == *"no root password found"* ]]
  [[ "$output" == *"db"* ]]
  [[ "$output" != *" ok "*"MySQL dump"* ]]
  [ ! -f "$DUMP_DIR/db_mysql.sql.gz" ]
}

@test "KEDGE-W-002: Passwort gefunden, aber mysqldump selbst schlaegt fehl -> harter Fehlschlag statt 'ok'" {
  MOCK_ENV=("MYSQL_ROOT_PASSWORD=geheim123")
  MOCK_DUMP_EXIT=1
  run run_pre_hooks "$(config_json mariadb:11)" "$DUMP_DIR"
  [ "$status" -ne 0 ]
  [[ "$output" == *"failed"* ]]
  [[ "$output" != *" ok "*"MySQL dump"* ]]
}
