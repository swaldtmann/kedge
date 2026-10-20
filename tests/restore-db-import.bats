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
