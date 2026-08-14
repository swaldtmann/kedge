#!/usr/bin/env bats
# Tests fuer verify.sh -- TTL-Labels + kollisionsfreier Box-Name (CW-W-296 Teil 2).
# Anlass: Beifund aus der CW-W-295-Diagnose (kedge-verify-1600 lief ~1 Monat
# unbemerkt weiter). create_box() bekommt class=ephemeral + ttl=<heute+1d>
# Labels, damit der Hausmeister-Sweep (CW-W-296 Teil 1) verwaiste Boxen fangen
# kann. BOX_NAME wechselt von reiner Uhrzeit (kollidiert ueber Tage) auf
# Datum+Uhrzeit.

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../verify.sh"

  MOCKBIN="${BATS_TEST_TMPDIR}/mockbin"
  mkdir -p "$MOCKBIN"

  CREATE_ARGS_FILE="${BATS_TEST_TMPDIR}/create_args.txt"
  cat > "$MOCKBIN/hcloud" <<MOCK
#!/usr/bin/env bash
if [[ "\$1" == "server" && "\$2" == "create" ]]; then
  echo "\$@" > "$CREATE_ARGS_FILE"
  exit 0
fi
if [[ "\$1" == "server" && "\$2" == "list" ]]; then
  echo "kedge-verify-test 10.0.0.9"
  exit 0
fi
exit 1
MOCK
  chmod +x "$MOCKBIN/hcloud"

  # `date` mocken statt an GNU (Linux, verify.sh' echtes Zielsystem) vs. BSD
  # (macOS-Dev/CI-Runner) haengen zu bleiben -- deterministisch, hermetisch.
  cat > "$MOCKBIN/date" <<'MOCK'
#!/usr/bin/env bash
case "$*" in
  "+%Y%m%d-%H%M") echo "20260101-1200" ;;
  "-u -d +1 day +%Y-%m-%d") echo "2026-01-02" ;;
  *) exit 1 ;;
esac
MOCK
  chmod +x "$MOCKBIN/date"

  export PATH="$MOCKBIN:$PATH"

  source "$SUITE"
}

@test "create_box: setzt class=ephemeral und ttl=<heute+1d> Labels" {
  run create_box "kedge-verify-test"
  [ "$status" -eq 0 ]

  args="$(cat "$CREATE_ARGS_FILE")"
  [[ "$args" == *"--label purpose=kedge-verify"* ]]
  [[ "$args" == *"--label class=ephemeral"* ]]
  [[ "$args" == *"--label ttl=2026-01-02"* ]]
}

@test "BOX_NAME: enthaelt Datum, nicht nur Uhrzeit (kollisionsfrei ueber Tage)" {
  [ "$BOX_NAME" = "kedge-verify-20260101-1200" ]
}
