# The mysql-to-sqlite converter: replays a MySQL dump through the
# MySQL-on-SQLite driver to produce a SQLite database with the exact schema
# (and emulated INFORMATION_SCHEMA) the site will use at runtime.
#
# Packaged with the platform PHP build and the pinned, assembled driver source
# (wordpress-sqlite-anywhere's `driver` package: upstream plus the plugin's
# patches), so a migration never depends on whatever PHP happens to be on the
# operator's machine. The driver's native parser extension is loaded when
# given: a real dump is a gigabyte of INSERTs, and the Rust parser is ~15x
# faster than the pure-PHP one.
#
#   nix run github:Avunu/wordpress#mysql-to-sqlite -- dump.sql out.sqlite
{
  pkgs,
  # A PHP build with pdo_sqlite (the platform PHP targets D1 and does not
  # bundle it; the converter writes a local SQLite file).
  php,
  # The driver's src/ directory (contains load.php).
  driverSrc,
  # The wp_mysql_parser extension built against `php`, or null for the
  # pure-PHP parser.
  parserExtension ? null,
}:
pkgs.writeShellApplication {
  name = "mysql-to-sqlite";
  runtimeInputs = [ php ];
  text = ''
    export WP_MYSQL_ON_SQLITE_SRC=${driverSrc}
    exec php -d memory_limit=-1 ${
      pkgs.lib.optionalString (parserExtension != null) "-d extension=${parserExtension}/lib/libwp_mysql_parser.so"
    } ${../tools/mysql-to-sqlite.php} "$@"
  '';
}
