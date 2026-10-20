#!/usr/bin/env bats
# restore.sh DB import must not report success when the import failed.
# Uses a docker stub on PATH (no real Docker): it logs its argv and exits
# with STUB_EXIT, except that psql without "-d postgres" always exits 2
# (models POSTGRES_DB != POSTGRES_USER: connection to a non-existent DB).

setup() {
  STUB_DIR="${BATS_TEST_TMPDIR}/bin"
  mkdir -p "$STUB_DIR"
  export DOCKER_LOG="${BATS_TEST_TMPDIR}/docker.log"
  : > "$DOCKER_LOG"
  cat > "$STUB_DIR/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$DOCKER_LOG"
cat > /dev/null
echo "stub output line"
if [[ "$*" == *" psql "* && "$*" != *"-d postgres"* ]]; then
  exit 2
fi
exit "${STUB_EXIT:-0}"
STUB
  chmod +x "$STUB_DIR/docker"
  export PATH="$STUB_DIR:$PATH"

  DUMP="${BATS_TEST_TMPDIR}/dump.gz"
  echo "SELECT 1;" | gzip > "$DUMP"

  source "${BATS_TEST_DIRNAME}/../restore.sh"
  IMPORT_FAILURES=0
}

@test "postgres import calls psql with -d postgres" {
  run import_postgres_dump cont pguser "$DUMP"
  grep -q 'psql -U pguser -d postgres' "$DOCKER_LOG"
}

@test "postgres import succeeds with exit 0" {
  STUB_EXIT=0 run import_postgres_dump cont pguser "$DUMP"
  [ "$status" -eq 0 ]
  [[ "$output" == *"PostgreSQL dump imported"* ]]
  [[ "$output" != *"failed"* ]]
}

@test "postgres import failure: warning, no 'dump imported'" {
  STUB_EXIT=1 run import_postgres_dump cont pguser "$DUMP"
  [ "$status" -eq 0 ]
  [[ "$output" == *"PostgreSQL import failed (exit 1)"* ]]
  [[ "$output" != *"dump imported"* ]]
}

@test "mysql import succeeds with exit 0" {
  STUB_EXIT=0 run import_mysql_dump cont "$DUMP" mysql
  [ "$status" -eq 0 ]
  [[ "$output" == *"MySQL dump imported"* ]]
}

@test "mysql import failure: warning, no 'dump imported'" {
  STUB_EXIT=3 run import_mysql_dump cont "$DUMP" mysql -e MYSQL_PWD=x
  [ "$status" -eq 0 ]
  [[ "$output" == *"MySQL import failed (exit 3)"* ]]
  [[ "$output" != *"dump imported"* ]]
}

@test "mongo import succeeds with exit 0" {
  STUB_EXIT=0 run import_mongo_dump cont "$DUMP"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MongoDB dump imported"* ]]
}

@test "mongo import failure: warning, no 'dump imported'" {
  STUB_EXIT=4 run import_mongo_dump cont "$DUMP"
  [ "$status" -eq 0 ]
  [[ "$output" == *"MongoDB import failed (exit 4)"* ]]
  [[ "$output" != *"dump imported"* ]]
}

@test "failed import increments IMPORT_FAILURES, success does not" {
  STUB_EXIT=0 import_mongo_dump cont "$DUMP" >/dev/null
  [ "$IMPORT_FAILURES" -eq 0 ]
  STUB_EXIT=1 import_mongo_dump cont "$DUMP" >/dev/null
  [ "$IMPORT_FAILURES" -eq 1 ]
}

# --- skipped dumps (container not running) and final exit status ----------

make_dumps() {
  BACKUP_ROOT="${BATS_TEST_TMPDIR}/backup"
  mkdir -p "$BACKUP_ROOT/dumps"
  for n in db_postgres.sql.gz db_mysql.sql.gz db_mongo.archive.gz; do
    echo x | gzip > "$BACKUP_ROOT/dumps/$n"
  done
  # compose stub: `ps -q <svc>` prints nothing -> container not running
  printf '#!/usr/bin/env bash\nexit 0\n' > "${BATS_TEST_TMPDIR}/bin/compose-none"
  chmod +x "${BATS_TEST_TMPDIR}/bin/compose-none"
  COMPOSE_CMD="${BATS_TEST_TMPDIR}/bin/compose-none"
}

@test "skipped mysql dump (container not running): warning and counted" {
  make_dumps
  rm "$BACKUP_ROOT/dumps/db_postgres.sql.gz" "$BACKUP_ROOT/dumps/db_mongo.archive.gz"
  import_dumps "$BACKUP_ROOT" > "${BATS_TEST_TMPDIR}/out"
  grep -q "Container for .* not running" "${BATS_TEST_TMPDIR}/out"
  [ "$IMPORT_FAILURES" -eq 1 ]
}

@test "skipped mongo dump (container not running): warning and counted" {
  make_dumps
  rm "$BACKUP_ROOT/dumps/db_postgres.sql.gz" "$BACKUP_ROOT/dumps/db_mysql.sql.gz"
  import_dumps "$BACKUP_ROOT" > "${BATS_TEST_TMPDIR}/out"
  grep -q "Container for .* not running" "${BATS_TEST_TMPDIR}/out"
  [ "$IMPORT_FAILURES" -eq 1 ]
}

@test "skipped postgres dump is counted too" {
  make_dumps
  rm "$BACKUP_ROOT/dumps/db_mysql.sql.gz" "$BACKUP_ROOT/dumps/db_mongo.archive.gz"
  import_dumps "$BACKUP_ROOT" > "${BATS_TEST_TMPDIR}/out"
  [ "$IMPORT_FAILURES" -eq 1 ]
}

@test "_finish_restore: counter 0 -> 'Restore complete', exit 0" {
  IMPORT_FAILURES=0
  run _finish_restore 7
  [ "$status" -eq 0 ]
  [[ "$output" == *"Restore complete (7s)"* ]]
}

@test "_finish_restore: counter > 0 -> warning 'failed or skipped', exit 1" {
  IMPORT_FAILURES=2
  run _finish_restore 7
  [ "$status" -eq 1 ]
  [[ "$output" == *"2 database import(s) failed or skipped"* ]]
  [[ "$output" != *"Restore complete"* ]]
}
