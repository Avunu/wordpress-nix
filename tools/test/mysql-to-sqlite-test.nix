# Round-trip check for the mysql-to-sqlite converter.
#
# Converts a fixture MySQL dump that deliberately contains the things a naive
# converter gets wrong — semicolons inside string literals, escaped quotes,
# serialized PHP, "--" mid-content, emoji, and plugin-table types that SQLite
# affinity cannot round-trip (decimal/tinyint/unsigned) — then asserts both the
# data and the driver's recorded MySQL type metadata survived.
{
  pkgs,
  converter,
}:
pkgs.runCommand "mysql-to-sqlite-test"
  {
    nativeBuildInputs = [
      converter
      pkgs.sqlite
    ];
  }
  ''
    set -euo pipefail
    mysql-to-sqlite ${../test/fixture-dump.sql} ./out.sqlite --quiet

    fail() { echo "FAIL: $1" >&2; exit 1; }
    q() { sqlite3 ./out.sqlite "$1"; }

    # --- data fidelity: the statement splitter must not break on these ---
    [ "$(q "SELECT COUNT(*) FROM wp_options;")" = "4" ] \
      || fail "expected 4 option rows (multi-VALUES INSERT split wrongly?)"
    [ "$(q "SELECT option_value FROM wp_options WHERE option_name='blogname';")" = "Semi; colon's Test" ] \
      || fail "semicolon/escaped-quote inside a string literal was corrupted"
    q "SELECT option_value FROM wp_options WHERE option_name='tricky';" | grep -q 'still data' \
      || fail "multi-line value containing -- was truncated as a comment"
    q "SELECT option_value FROM wp_options WHERE option_name='widget_meta';" | grep -q 's:12:"_multiwidget"' \
      || fail "serialized PHP payload was corrupted"
    q "SELECT post_content FROM wp_posts WHERE ID=1;" | grep -q '🎉' \
      || fail "utf8mb4 content did not survive"
    [ "$(q "SELECT COUNT(*) FROM wp_posts;")" = "2" ] || fail "expected 2 posts"

    # --- schema fidelity: the reason we replay through the driver at all ---
    # A generic MySQL->SQLite converter produces the physical tables but NOT
    # these metadata tables, and SQLite affinity cannot recover the original
    # MySQL types (REAL -> decimal(10,4) is unrecoverable).
    types() {
      q "SELECT column_type FROM _wp_sqlite_mysql_information_schema_columns \
         WHERE table_name='$1' AND column_name='$2';"
    }
    [ "$(types custom_plugin_table ratio)" = "decimal(10,4)" ] \
      || fail "plugin-table decimal precision was lost"
    [ "$(types custom_plugin_table flag)" = "tinyint(1)" ] \
      || fail "plugin-table tinyint(1) was lost"
    [ "$(types wp_posts ID)" = "bigint unsigned" ] \
      || fail "unsigned bigint was lost"
    [ "$(types wp_posts post_content)" = "longtext" ] \
      || fail "longtext was lost"

    # The emulated information schema must know every migrated table.
    [ "$(q "SELECT COUNT(*) FROM _wp_sqlite_mysql_information_schema_tables \
            WHERE table_name IN ('wp_options','wp_posts','custom_plugin_table');")" = "3" ] \
      || fail "information schema is missing migrated tables"

    echo "mysql-to-sqlite: data and schema fidelity OK"
    touch $out
  ''
