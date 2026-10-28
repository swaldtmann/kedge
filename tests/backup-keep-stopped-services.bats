#!/usr/bin/env bats
# Tests fuer backup.sh -- stop_stack/start_stack starten nur Dienste neu, die
# vor dem Backup liefen (von Hand gestoppte bleiben gestoppt).
#
# `docker compose` wird durch eine Funktion gemockt, die den Zustand je Dienst
# in Dateien unter $STATE haelt (eine Datei pro Dienst, Inhalt up|down) und
# jeden Aufruf in $STATE/calls protokolliert.

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../backup.sh"
  export STACK_DIR="${BATS_TEST_TMPDIR}/stack"
  export STATE="${BATS_TEST_TMPDIR}/state"
  export BACKUP_STOP_STACK=true
  mkdir -p "$STACK_DIR" "$STATE"
  : > "$STATE/calls"
  source "$SUITE"
  COMPOSE_CMD="mock_compose"
  mock_compose() {
    echo "$*" >> "$STATE/calls"
    case "$1" in
      ps)
        [[ -f "$STATE/ps-fails" ]] && return 1
        for f in "$STATE"/svc-*; do
          [[ -e "$f" ]] || continue
          [[ "$(cat "$f")" == "up" ]] || continue
          n="${f##*/svc-}"
          if [[ "$*" == *"--services"* ]]; then echo "$n"; else echo "c-$n"; fi
        done ;;
      stop)
        shift
        if [[ $# -eq 0 ]]; then for f in "$STATE"/svc-*; do echo down > "$f"; done
        else for n in "$@"; do echo down > "$STATE/svc-$n"; done; fi ;;
      start)
        shift
        if [[ $# -eq 0 ]]; then for f in "$STATE"/svc-*; do echo up > "$f"; done
        else for n in "$@"; do echo up > "$STATE/svc-$n"; done; fi ;;
    esac
  }
  export -f mock_compose
}

_set() { echo "$2" > "$STATE/svc-$1"; }
_state() { cat "$STATE/svc-$1"; }

@test "von Hand gestoppter Dienst b bleibt nach Stop/Start gestoppt, a laeuft wieder" {
  _set a up; _set b down
  stop_stack
  [ "$(_state a)" = "down" ]
  start_stack
  [ "$(_state a)" = "up" ]
  [ "$(_state b)" = "down" ]
}

@test "alle liefen: alle laufen wieder" {
  _set a up; _set b up
  stop_stack
  start_stack
  [ "$(_state a)" = "up" ]
  [ "$(_state b)" = "up" ]
}

@test "keiner lief: weder stop noch start" {
  _set a down; _set b down
  stop_stack
  start_stack
  ! grep -qE '^(stop|start)' "$STATE/calls"
}

@test "ps schlaegt fehl: Abbruch vor stop (fail-closed)" {
  _set a up
  touch "$STATE/ps-fails"
  run stop_stack
  [ "$status" -ne 0 ]
  ! grep -qE '^stop' "$STATE/calls"
  [ "$(_state a)" = "up" ]
}
