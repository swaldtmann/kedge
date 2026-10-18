#!/usr/bin/env bats
# verify.sh / test.sh have no built-in hcloud context or SSH key name: when a
# value is missing they must abort before the first hcloud call.
# hcloud is a stub on PATH that logs every invocation.

setup() {
  MOCKBIN="${BATS_TEST_TMPDIR}/mockbin"
  mkdir -p "$MOCKBIN"
  HCLOUD_LOG="${BATS_TEST_TMPDIR}/hcloud.log"
  : > "$HCLOUD_LOG"

  cat > "$MOCKBIN/hcloud" <<MOCK
#!/usr/bin/env bash
echo "\$@" >> "$HCLOUD_LOG"
exit 1
MOCK
  for t in ssh scp restic; do
    printf '#!/usr/bin/env bash\nexit 1\n' > "$MOCKBIN/$t"
  done
  chmod +x "$MOCKBIN"/*

  VERIFY="${BATS_TEST_DIRNAME}/../verify.sh"
  TESTSH="${BATS_TEST_DIRNAME}/../test.sh"
}

# Run a script with a clean environment apart from PATH and the given VAR=val args.
run_clean() {
  local script="$1"; shift
  local args=()
  while [[ $# -gt 0 && "$1" != "--" ]]; do args+=("$1"); shift; done
  [[ "${1:-}" == "--" ]] && shift
  run env -i PATH="$MOCKBIN:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin" HOME="$BATS_TEST_TMPDIR" \
    RESTIC_REPOSITORY=/nonexistent RESTIC_PASSWORD=x "${args[@]}" bash "$script" "$@"
}

@test "verify.sh: no HCLOUD_CONTEXT and no HCLOUD_TOKEN -> abort, no hcloud call" {
  run_clean "$VERIFY" SSH_KEY_NAME=mykey
  [ "$status" -ne 0 ]
  [[ "$output" == *"set HCLOUD_CONTEXT to the hcloud CLI context for throwaway test servers"* ]]
  [ ! -s "$HCLOUD_LOG" ]
}

@test "verify.sh: no SSH_KEY_NAME -> abort, no hcloud call" {
  run_clean "$VERIFY" HCLOUD_CONTEXT=myctx
  [ "$status" -ne 0 ]
  [[ "$output" == *"set SSH_KEY_NAME to the name of the hcloud SSH key"* ]]
  [ ! -s "$HCLOUD_LOG" ]
}

@test "verify.sh --burn: no HCLOUD_CONTEXT and no HCLOUD_TOKEN -> abort, no hcloud call" {
  run_clean "$VERIFY" -- --burn
  [ "$status" -ne 0 ]
  [[ "$output" == *"set HCLOUD_CONTEXT"* ]]
  [ ! -s "$HCLOUD_LOG" ]
}

@test "verify.sh: HCLOUD_TOKEN replaces HCLOUD_CONTEXT as requirement" {
  run_clean "$VERIFY" HCLOUD_TOKEN=t SSH_KEY_NAME=mykey
  [[ "$output" != *"set HCLOUD_CONTEXT"* ]]
}

@test "test.sh: no HCLOUD_CONTEXT and no HCLOUD_TOKEN -> abort, no hcloud call" {
  run_clean "$TESTSH" SSH_KEY_NAME=mykey
  [ "$status" -ne 0 ]
  [[ "$output" == *"set HCLOUD_CONTEXT to the hcloud CLI context for throwaway test servers"* ]]
  [ ! -s "$HCLOUD_LOG" ]
}

@test "test.sh: no SSH_KEY_NAME -> abort, no hcloud call" {
  run_clean "$TESTSH" HCLOUD_CONTEXT=myctx
  [ "$status" -ne 0 ]
  [[ "$output" == *"set SSH_KEY_NAME to the name of the hcloud SSH key"* ]]
  [ ! -s "$HCLOUD_LOG" ]
}

@test "test.sh --burn: no HCLOUD_CONTEXT and no HCLOUD_TOKEN -> abort, no hcloud call" {
  run_clean "$TESTSH" -- --burn
  [ "$status" -ne 0 ]
  [[ "$output" == *"set HCLOUD_CONTEXT"* ]]
  [ ! -s "$HCLOUD_LOG" ]
}
