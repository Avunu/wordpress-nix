# restore-core-keys: a dump whose wp_options carries index-wp-mysql-for-speed's
# keys (PRIMARY KEY (option_name), UNIQUE KEY option_id) must come out with the
# standard ones, columns untouched, other tables untouched -- and then convert
# to a SQLite database where option_name is UNIQUE.
{
  pkgs,
  restoreCoreKeys,
  converter,
}:
pkgs.runCommand "restore-core-keys-test"
  {
    nativeBuildInputs = [
      restoreCoreKeys
      converter
      pkgs.sqlite
      pkgs.python3
    ];
  }
  ''
    set -euo pipefail
    fail() { echo "FAIL: $1" >&2; exit 1; }

    # Rewrite the fixture's wp_options keys the way the plugin does.
    python3 - <<'PY'
    import re
    s = open("${../test/fixture-dump.sql}", encoding="utf-8").read()
    s = s.replace("  PRIMARY KEY (`option_id`),\n  UNIQUE KEY `option_name` (`option_name`),\n  KEY `autoload` (`autoload`)",
                  "  PRIMARY KEY (`option_name`),\n  UNIQUE KEY `option_id` (`option_id`),\n  KEY `autoload` (`autoload`)")
    assert "PRIMARY KEY (`option_name`)" in s
    open("dump.sql", "w", encoding="utf-8").write(s)
    PY

    restore-core-keys dump.sql fixed.sql > report.txt 2> warnings.txt
    cat report.txt
    grep -q "^wp_options:" report.txt || fail "wp_options was not reported as rewritten"
    grep -q "warning: wp_posts: .*lacks" warnings.txt || fail "the trimmed wp_posts should have been left alone with a warning"
    [ "$(grep -c ' tables rewritten' report.txt)" = "1" ] && grep -q '^1 tables rewritten' report.txt \
      || fail "expected exactly one table rewritten (the trimmed wp_posts is skipped)"
    grep -A8 'CREATE TABLE `wp_options`' fixed.sql | grep -q 'PRIMARY KEY  (option_id)' || fail "standard PRIMARY KEY missing"
    grep -A8 'CREATE TABLE `wp_options`' fixed.sql | grep -q 'UNIQUE KEY option_name (option_name)' || fail "standard UNIQUE KEY missing"
    grep -A8 'CREATE TABLE `wp_options`' fixed.sql | grep -q '`option_value` longtext' || fail "column definitions were not kept"
    # Everything but that table's key lines is byte-identical.
    diff <(grep -v 'KEY' dump.sql) <(grep -v 'KEY' fixed.sql) || fail "something other than key lines changed"

    mysql-to-sqlite fixed.sql out.sqlite --quiet --continue-on-error 2>/dev/null || true
    sqlite3 out.sqlite "SELECT sql FROM sqlite_master WHERE name='wp_options__option_name'" | grep -q 'CREATE UNIQUE INDEX' \
      || fail "option_name is not UNIQUE after conversion"
    [ "$(sqlite3 out.sqlite "SELECT COUNT(*) FROM wp_options")" = "4" ] || fail "rows lost"
    echo ok > $out
  ''
