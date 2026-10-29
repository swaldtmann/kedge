#!/usr/bin/env bats
# Version/status file per backup/prune run: $KEDGE_STATE_DIR/<stack>.version

setup() {
  SUITE="${BATS_TEST_DIRNAME}/../backup.sh"
  export KEDGE_STATE_DIR="${BATS_TEST_TMPDIR}/state"
  export RESTIC_REPOSITORY="${BATS_TEST_TMPDIR}/repo"
  export RESTIC_PASSWORD="bats-test-password"
  mkdir -p "${BATS_TEST_TMPDIR}/stackA" "${BATS_TEST_TMPDIR}/calcom"
  export STACK_DIR="${BATS_TEST_TMPDIR}/stackA"
  export SUITE
}

# Fresh bash per call: bats runs `run` in an errexit-ignoring context, which
# would mask `set -e` inside backup.sh (production runs it as a plain script).
_main() {
  run bash -c 'source "$SUITE"; check_prereqs() { :; }; restic() { return "${FAKE_RESTIC_RC:-0}"; }; main "$@"' _ "$@"
}

_get() { grep "^$2=" "$1" | cut -d= -f2-; }

@test "prune success writes status file with all keys" {
  _main prune
  [ "$status" -eq 0 ]
  f="$KEDGE_STATE_DIR/stackA.version"
  [ -f "$f" ]
  [ "$(cut -d= -f1 "$f" | paste -sd, -)" = "kedge,stack,run,started,finished,result" ]
  [ "$(_get "$f" stack)" = "stackA" ]
  [ "$(_get "$f" run)" = "prune" ]
  [ "$(_get "$f" result)" = "ok" ]
  [[ "$(_get "$f" started)" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$ ]]
  [[ "$(_get "$f" finished)" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z$ ]]
  [ -n "$(_get "$f" kedge)" ]
  [ "$(_get "$f" kedge)" != "unknown" ]
  [ "$(stat -c %a "$f" 2>/dev/null || stat -f %Lp "$f")" = "644" ]
  [ "$(ls "$KEDGE_STATE_DIR" | wc -l | tr -d ' ')" = "1" ]
}

@test "kedge version matches pyproject version (no v prefix)" {
  _main prune
  [ "$status" -eq 0 ]
  pv="$(sed -n 's/^version = "\(.*\)"/\1/p' "${BATS_TEST_DIRNAME}/../pyproject.toml" | head -1)"
  [[ "$(_get "$KEDGE_STATE_DIR/stackA.version" kedge)" == "$pv"* ]]
}

@test "failed prune writes result=failed + exit and keeps exit code" {
  export FAKE_RESTIC_RC=3
  _main prune
  [ "$status" -eq 3 ]
  f="$KEDGE_STATE_DIR/stackA.version"
  [ "$(_get "$f" result)" = "failed" ]
  [ "$(_get "$f" exit)" = "3" ]
}

@test "success file has no exit key" {
  _main prune
  [ "$status" -eq 0 ]
  [ "$(grep -c '^exit=' "$KEDGE_STATE_DIR/stackA.version" || true)" = "0" ]
}

@test "backup run is recorded (failing early: repo not initialized)" {
  export FAKE_RESTIC_RC=1
  _main backup
  [ "$status" -eq 1 ]
  f="$KEDGE_STATE_DIR/stackA.version"
  [ "$(_get "$f" run)" = "backup" ]
  [ "$(_get "$f" result)" = "failed" ]
  [ "$(_get "$f" exit)" = "1" ]
}

@test "unwritable state dir: warning, run continues, exit unchanged" {
  : > "${BATS_TEST_TMPDIR}/blocker"
  export KEDGE_STATE_DIR="${BATS_TEST_TMPDIR}/blocker/sub"
  _main prune
  [ "$status" -eq 0 ]
  [[ "$output" == *"version file"* ]]
  export FAKE_RESTIC_RC=4
  _main prune
  [ "$status" -eq 4 ]
}

@test "two stacks produce two files" {
  _main prune
  export STACK_DIR="${BATS_TEST_TMPDIR}/calcom"
  _main prune
  [ -f "$KEDGE_STATE_DIR/stackA.version" ]
  [ -f "$KEDGE_STATE_DIR/calcom.version" ]
  [ "$(_get "$KEDGE_STATE_DIR/calcom.version" stack)" = "calcom" ]
}

@test "other commands (list) write no file" {
  _main list
  [ ! -e "$KEDGE_STATE_DIR/stackA.version" ]
}
