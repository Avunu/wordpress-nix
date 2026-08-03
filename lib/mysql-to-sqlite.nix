# The mysql-to-sqlite converter: replays a MySQL dump through the
# MySQL-on-SQLite driver to produce a SQLite database with the exact schema
# (and emulated INFORMATION_SCHEMA) the site will use at runtime.
#
# Packaged with the platform PHP build and the pinned driver source, so a
# migration never depends on whatever PHP happens to be on the operator's
# machine.
#
#   nix run github:Avunu/wordpress#mysql-to-sqlite -- dump.sql out.sqlite
{
  pkgs,
  # A PHP build with pdo_sqlite (the platform PHP targets D1 and does not
  # bundle it; the converter writes a local SQLite file).
  php,
  d1DriverSrc,
}:
let
  driverSrc = "${d1DriverSrc}/packages/mysql-on-sqlite/src";
in
pkgs.writeShellApplication {
  name = "mysql-to-sqlite";
  runtimeInputs = [ php ];
  text = ''
    export WP_MYSQL_ON_SQLITE_SRC=${driverSrc}
    exec php ${../tools/mysql-to-sqlite.php} "$@"
  '';
}
