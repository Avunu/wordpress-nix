# Round-trip check for the sqlite-to-turso loader.
#
# Converts the fixture dump with mysql-to-sqlite, loads the result into a
# local `tursodb --sync-server` primary, then reads it back through the same
# pipeline endpoint and compares with the source file: every table, every
# row, the recorded MySQL type metadata, and the refuse/--replace behaviour
# on a non-empty target.
{
  pkgs,
  converter,
  loader,
}:
pkgs.runCommand "sqlite-to-turso-test"
  {
    nativeBuildInputs = [
      converter
      loader
      pkgs.sqlite
      pkgs.turso
      pkgs.python3
      pkgs.curl
    ];
  }
  ''
    set -euo pipefail
    fail() { echo "FAIL: $1" >&2; exit 1; }

    mysql-to-sqlite ${../test/fixture-dump.sql} ./source.sqlite --quiet
    # A row the converter never produces: binary content and a large integer.
    sqlite3 ./source.sqlite "CREATE TABLE blobs (id INTEGER PRIMARY KEY AUTOINCREMENT, body BLOB, big INTEGER, ratio REAL);"
    sqlite3 ./source.sqlite "INSERT INTO blobs (body, big, ratio) VALUES (X'00ff10', 9007199254740993, 0.1), (NULL, -1, NULL);"
    sqlite3 ./source.sqlite "DELETE FROM blobs WHERE id = 2;"   # leaves the AUTOINCREMENT counter above MAX(id)

    mkdir primary
    tursodb ./primary/primary.db --sync-server 127.0.0.1:18080 > tursodb.log 2>&1 &
    server=$!
    trap 'kill $server 2>/dev/null || true' EXIT
    for _ in $(seq 1 100); do
      curl -sf -o /dev/null -X POST -H 'Content-Type: application/json' \
        --data '{"requests":[{"type":"execute","stmt":{"sql":"SELECT 1"}}]}' \
        http://127.0.0.1:18080/v2/pipeline && break
      sleep 0.1
    done

    sqlite-to-turso ./source.sqlite http://127.0.0.1:18080 > load.log 2>&1 || { cat load.log; fail "first load failed"; }
    cat load.log

    # A second load must refuse a non-empty target, and --replace must start over.
    if sqlite-to-turso ./source.sqlite http://127.0.0.1:18080 --quiet 2> refuse.log; then
      fail "a non-empty target was not refused"
    fi
    grep -q -- '--replace' refuse.log || { cat refuse.log; fail "the refusal did not mention --replace"; }
    sqlite-to-turso ./source.sqlite http://127.0.0.1:18080 --replace --quiet || fail "--replace load failed"

    # Compare every table's contents with the source, read back over the pipeline.
    python3 ${./compare-with-turso.py} ./source.sqlite http://127.0.0.1:18080 || fail "the loaded database differs from the source"

    # The AUTOINCREMENT counter survived (2, not MAX(id) = 1).
    seq=$(python3 ${./compare-with-turso.py} ./source.sqlite http://127.0.0.1:18080 --query "SELECT seq FROM sqlite_sequence WHERE name = 'blobs'")
    [ "$seq" = "2" ] || fail "AUTOINCREMENT counter for blobs is $seq, expected 2"

    echo ok > $out
  ''
