#!/usr/bin/env bats
# Roundtrip backup.sh -> restore.sh fuer externe Bind-Mounts (Datei und
# Verzeichnis ausserhalb STACK_DIR): am Originalpfad, sha256-gleich.
# Host-los: lokaler restic-Repo, Docker/Compose gestubbt. backup.sh und
# restore.sh laufen in getrennten bash-Prozessen (gleichnamige Funktionen).

setup() {
  export RESTIC_REPOSITORY="${BATS_TEST_TMPDIR}/repo"
  export RESTIC_PASSWORD="bats-test-password"
  export BACKUP_STOP_STACK=false
  export STACK_DIR="${BATS_TEST_TMPDIR}/stack"
  export KEDGE_STAGING_BASE="${BATS_TEST_TMPDIR}/staging"
  export RESTORE_TARGET="${BATS_TEST_TMPDIR}/target"
  mkdir -p "$STACK_DIR"
  ROOT="${KEDGE_ROOT:-${BATS_TEST_DIRNAME}/..}"

  A="${BATS_TEST_TMPDIR}/ext/a"; B="${BATS_TEST_TMPDIR}/ext/b"
  mkdir -p "$A" "$B" "${BATS_TEST_TMPDIR}/ext/viewer-html/sub"
  echo "conf-from-a" > "$A/nginx.conf"
  echo "conf-from-b" > "$B/nginx.conf"
  echo "<html>" > "${BATS_TEST_TMPDIR}/ext/viewer-html/index.html"
  echo "deep" > "${BATS_TEST_TMPDIR}/ext/viewer-html/sub/x.js"
  HASHES_BEFORE="$(cd "${BATS_TEST_TMPDIR}/ext" && find . -type f | sort | xargs shasum -a 256)"

  restic init >/dev/null
}

_stub_compose() {
  cat > "${BATS_TEST_TMPDIR}/stubs.sh" <<STUB
mock_compose() {
  cat <<EOF2
{"services": {"app": {"image": "nginx", "volumes": [
 {"type": "bind", "source": "$A/nginx.conf", "target": "/etc/a.conf"},
 {"type": "bind", "source": "$B/nginx.conf", "target": "/etc/b.conf"},
 {"type": "bind", "source": "${BATS_TEST_TMPDIR}/ext/viewer-html", "target": "/usr/share/nginx/html"}
]}}}
EOF2
}
export -f mock_compose
check_prereqs() { :; }
docker() { [[ "\$1" == "ps" ]] && echo "" || echo "[]"; }
export -f docker
STUB
}

_roundtrip_backup() {
  _stub_compose
  run bash -c "source '$ROOT/backup.sh'; source '${BATS_TEST_TMPDIR}/stubs.sh'; COMPOSE_CMD=mock_compose; cmd_backup"
  [ "$status" -eq 0 ]
}

@test "Roundtrip: Datei- und Verzeichnis-Mount landen nach Restore am Originalpfad, sha256-gleich, gleicher Basename kollidiert nicht" {
  _roundtrip_backup
  rm -rf "${BATS_TEST_TMPDIR}/ext"
  run bash -c "source '$ROOT/restore.sh'; source '${BATS_TEST_TMPDIR}/stubs.sh'; STACK=\$(basename '$STACK_DIR'); COMPOSE_CMD=true; cmd_restore"
  echo "$output"
  [ "$status" -eq 0 ]
  [ -f "$A/nginx.conf" ]
  [ -f "$B/nginx.conf" ]
  [ ! -e "${BATS_TEST_TMPDIR}/ext/viewer-html/viewer-html" ]
  HASHES_AFTER="$(cd "${BATS_TEST_TMPDIR}/ext" && find . -type f | sort | xargs shasum -a 256)"
  [ "$HASHES_BEFORE" = "$HASHES_AFTER" ]
}
