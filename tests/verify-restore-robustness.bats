#!/usr/bin/env bats
# Tests fuer verify.sh -- Robustheit beim Quartals-Restore prod-cloud (2026-10-28):
#   1. latest-Pruefung nutzt Port 23 + Key fuer sftp:-Repos und zeigt restic-Fehler
#   2. Box-Typliste enthaelt nur Typen mit >= 160 GB Platte, Abbruch mit hcloud-Fehler
#   3. ttl-Label portabel (BSD date) + owner/created_by_session-Label
#   4. beendeter Einmal-Container (Exit 0) ist kein Fehler
#   5. Nextcloud-Check via `php occ status`

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../verify.sh"

  MOCKBIN="${BATS_TEST_TMPDIR}/mockbin"
  mkdir -p "$MOCKBIN"
  LOG="${BATS_TEST_TMPDIR}/calls.log"
  : > "$LOG"

  # --- hcloud: context/list ok; server create gelingt nur fuer $CREATE_OK_TYPE ---
  cat > "$MOCKBIN/hcloud" <<MOCK
#!/usr/bin/env bash
if [[ "\$1" == "context" && "\$2" == "active" ]]; then echo "\${HCLOUD_CONTEXT:-ctx}"; exit 0; fi
if [[ "\$1" == "server" && "\$2" == "list" ]]; then echo "kedge-verify-test 10.0.0.9"; exit 0; fi
if [[ "\$1" == "server" && "\$2" == "delete" ]]; then exit 0; fi
if [[ "\$1" == "server" && "\$2" == "create" ]]; then
  echo "\$@" >> "$LOG"
  type=""
  while [[ \$# -gt 0 ]]; do [[ "\$1" == "--type" ]] && type="\$2"; shift; done
  if [[ -n "\${CREATE_OK_TYPE:-}" && "\$type" == "\$CREATE_OK_TYPE" ]]; then exit 0; fi
  echo "mock: server type \$type: resource_unavailable" >&2
  exit 1
fi
exit 1
MOCK

  # --- restic: loggt Argv; scheitert mit Host-key-Meldung wenn RESTIC_FAIL=1 ---
  cat > "$MOCKBIN/restic" <<MOCK
#!/usr/bin/env bash
echo "restic \$*" >> "$LOG"
if [[ "\${RESTIC_FAIL:-}" == "1" ]]; then
  echo "Host key verification failed." >&2
  exit 1
fi
echo '[{"short_id":"abc12345"}]'
MOCK

  # --- date: GNU-Form kaputt (wie macOS), BSD-Form -v funktioniert ---
  cat > "$MOCKBIN/date" <<'MOCK'
#!/usr/bin/env bash
case "$*" in
  "+%Y%m%d-%H%M") echo "20260101-1200" ;;
  "-u -v+1d +%Y-%m-%d") echo "2026-01-02" ;;
  "-u -d +1 day +%Y-%m-%d") echo "date: illegal option -- d" >&2; exit 1 ;;
  "+%H:%M:%S") echo "12:00:00" ;;
  *) exit 1 ;;
esac
MOCK

  cat > "$MOCKBIN/hostname" <<'MOCK'
#!/usr/bin/env bash
echo "WooAir"
MOCK

  # --- ssh: wie in verify-ssh-argv-injection.bats (Remote-Reparse nachstellen) ---
  cat > "$MOCKBIN/ssh" <<'MOCK'
#!/usr/bin/env bash
args=("$@"); dest_idx=-1
for i in "${!args[@]}"; do case "${args[$i]}" in *@*) dest_idx=$i; break ;; esac; done
cmd_args=("${args[@]:$((dest_idx+1))}")
joined="${cmd_args[*]}"
exec bash -c "$joined"
MOCK
  chmod +x "$MOCKBIN"/*
  export PATH="$MOCKBIN:$PATH"

  export HCLOUD_CONTEXT="ctx"
  export SSH_KEY_NAME="k"
  export RESTIC_PASSWORD="pw"
  export RESTIC_REPOSITORY="sftp:u1-sub1@u1-sub1.your-storagebox.de:."
  export RESTIC_SFTP_KEY="${BATS_TEST_TMPDIR}/key"
  export RESTIC_NO_LOCK=1
  unset CLAUDE_CODE_SESSION_ID CREATE_OK_TYPE RESTIC_FAIL || true
  export RESTORE_TARGET="${BATS_TEST_TMPDIR}/stack"
  mkdir -p "$RESTORE_TARGET"; : > "$RESTORE_TARGET/.env"

  set --  # bats uebergibt den Testnamen als $1 -> SNAPSHOT_ID waere nicht "latest"
  source "$SUITE"
}

# ---------------------------------------------------------------- 1. Port 23

@test "latest-Pruefung: sftp-Repo bekommt Port 23 und Key" {
  run check_prereqs
  [ "$status" -eq 0 ]
  grep -q -- "-p 23" "$LOG"
  grep -q -- "-i ${RESTIC_SFTP_KEY}" "$LOG"
  grep -q -- "IdentitiesOnly=yes" "$LOG"
}

@test "latest-Pruefung: restic-Fehler wird gezeigt statt als 'No snapshots found' verschluckt" {
  export RESTIC_FAIL=1
  run check_prereqs
  [ "$status" -eq 1 ]
  [[ "$output" == *"Host key verification failed"* ]]
}

@test "latest-Pruefung: lokales Repo bekommt keine sftp.args" {
  export RESTIC_REPOSITORY="/some/local/repo"
  run check_prereqs
  [ "$status" -eq 0 ]
  ! grep -q "sftp.args" "$LOG"
}

# ------------------------------------------------------------ 2. Typliste

@test "create_box: probiert nur Typen mit >= 160 GB Platte (kein cx23/cpx22/cax11)" {
  run create_box "kedge-verify-test"
  [ "$status" -eq 1 ]
  grep -q -- "--type cx43" "$LOG"
  ! grep -Eq -- "--type (cx23|cpx22|cax11)( |$)" "$LOG"
}

@test "create_box: Ausweich auf cpx32 wenn cx43 nicht verfuegbar" {
  export CREATE_OK_TYPE=cpx32
  run create_box "kedge-verify-test"
  [ "$status" -eq 0 ]
  [[ "$output" == *"10.0.0.9"* ]]
  grep -q -- "--type cpx32" "$LOG"
}

@test "create_box: Abbruch nennt die letzte hcloud-Fehlermeldung" {
  run create_box "kedge-verify-test"
  [ "$status" -eq 1 ]
  [[ "$output" == *"resource_unavailable"* ]]
}

# ------------------------------------------------------------ 3. Labels

@test "create_box: ttl-Label auch wenn date -d nicht geht (macOS/BSD)" {
  export CREATE_OK_TYPE=cx43
  run create_box "kedge-verify-test"
  [ "$status" -eq 0 ]
  grep -q -- "--label ttl=2026-01-02" "$LOG"
}

@test "create_box: owner- und created_by_session-Label wie hcloud-ephemeral" {
  export CREATE_OK_TYPE=cx43
  export CLAUDE_CODE_SESSION_ID="abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"
  run create_box "kedge-verify-test"
  [ "$status" -eq 0 ]
  grep -q -- "--label owner=wooair_abcdef12" "$LOG"
  grep -q -- "--label created_by_session=abcdef12-3456-7890-aaaa-bbbbbbbbbbbb" "$LOG"
}

@test "create_box: ohne Session-ID owner=<host>_hand und kein created_by_session" {
  export CREATE_OK_TYPE=cx43
  run create_box "kedge-verify-test"
  [ "$status" -eq 0 ]
  grep -q -- "--label owner=wooair_hand" "$LOG"
  ! grep -q "created_by_session" "$LOG"
}

# ------------------------------------------------- 4./5. Health-Check (Mock-docker)

mock_docker() {
  # $1 = ps -a (alle), $2 = ps (nur running), $3 = Services-JSON
  printf '%s\n' "$1" > "${BATS_TEST_TMPDIR}/ps_all.json"
  printf '%s\n' "$2" > "${BATS_TEST_TMPDIR}/ps_running.json"
  printf '%s\n' "$3" > "${BATS_TEST_TMPDIR}/services.json"
  cat > "$MOCKBIN/docker" <<MOCK
#!/usr/bin/env bash
T="${BATS_TEST_TMPDIR}"
case "\$*" in
  "compose version") exit 0 ;;
  "compose config --format json") cat "\$T/services.json" ;;
  "compose ps -a --format json") cat "\$T/ps_all.json" ;;
  "compose ps --format json") cat "\$T/ps_running.json" ;;
  "compose ps -q nextcloud") [[ -f "\$T/occ.txt" ]] && echo nc123 ;;
  "exec -u www-data nc123 php occ status") cat "\$T/occ.txt" ;;
  "compose ps -q "*) exit 0 ;;
  *) exit 1 ;;
esac
MOCK
  chmod +x "$MOCKBIN/docker"
}

APP='{"Name":"app","State":"running","ExitCode":0}'
INIT0='{"Name":"init","State":"exited","ExitCode":0}'
INIT1='{"Name":"init","State":"exited","ExitCode":1}'
WEB_RESTART='{"Name":"web","State":"restarting","ExitCode":1}'
NC='{"Name":"nextcloud","State":"running","ExitCode":0}'

@test "Health: beendeter Einmal-Container mit Exit 0 ist kein Fehler" {
  mock_docker "$APP"$'\n'"$INIT0" "$APP" \
    '{"services":{"app":{"image":"x:1"},"init":{"image":"y:1"}}}'
  run run_health_checks "10.0.0.9"
  [ "$status" -eq 0 ]
  [[ "$output" == *"FAILURES=0"* ]]
  [[ "$output" == *"CONTAINERS=2/2"* ]]
}

@test "Health: Container mit Exit != 0 bleibt Fehler" {
  mock_docker "$APP"$'\n'"$INIT1" "$APP" \
    '{"services":{"app":{"image":"x:1"},"init":{"image":"y:1"}}}'
  run run_health_checks "10.0.0.9"
  [ "$status" -eq 1 ]
  [[ "$output" == *"init: exited"* ]]
}

@test "Health: restarting bleibt Fehler" {
  mock_docker "$APP"$'\n'"$WEB_RESTART" "$APP" \
    '{"services":{"app":{"image":"x:1"},"web":{"image":"y:1"}}}'
  run run_health_checks "10.0.0.9"
  [ "$status" -eq 1 ]
}

@test "Nextcloud: installed true + maintenance false -> PASS" {
  mock_docker "$NC" "$NC" '{"services":{"nextcloud":{"image":"nextcloud:32-apache"}}}'
  printf '  - installed: true\n  - version: 32.0.15\n  - maintenance: false\n' > "${BATS_TEST_TMPDIR}/occ.txt"
  run run_health_checks "10.0.0.9"
  [ "$status" -eq 0 ]
  [[ "$output" == *"PASS: Nextcloud"* ]]
}

@test "Nextcloud: maintenance true -> FAIL" {
  mock_docker "$NC" "$NC" '{"services":{"nextcloud":{"image":"nextcloud:32-apache"}}}'
  printf '  - installed: true\n  - maintenance: true\n' > "${BATS_TEST_TMPDIR}/occ.txt"
  run run_health_checks "10.0.0.9"
  [ "$status" -eq 1 ]
  [[ "$output" == *"FAIL: Nextcloud"* ]]
}

@test "Nextcloud: installed false -> FAIL" {
  mock_docker "$NC" "$NC" '{"services":{"nextcloud":{"image":"nextcloud:32-apache"}}}'
  printf '  - installed: false\n  - maintenance: false\n' > "${BATS_TEST_TMPDIR}/occ.txt"
  run run_health_checks "10.0.0.9"
  [ "$status" -eq 1 ]
}

@test "Nextcloud: ohne Nextcloud-Container uebersprungen, kein Fehler" {
  mock_docker "$APP" "$APP" '{"services":{"app":{"image":"x:1"}}}'
  run run_health_checks "10.0.0.9"
  [ "$status" -eq 0 ]
  [[ "$output" != *"FAIL:"* ]]
}
