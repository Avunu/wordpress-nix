# The sqlite-to-turso loader: copies a SQLite database produced by
# mysql-to-sqlite into a Turso database over its SQL-over-HTTP pipeline.
#
# Rows travel as typed pipeline arguments (never as SQL text), so quoting,
# binary content and 64-bit integers are never an issue. Standard-library
# Python only.
#
#   TURSO_AUTH_TOKEN=... nix run github:Avunu/wordpress#sqlite-to-turso -- site.sqlite libsql://<db>-<org>.turso.io
{ pkgs }:
pkgs.writeShellApplication {
  name = "sqlite-to-turso";
  runtimeInputs = [ pkgs.python3 ];
  text = ''
    exec python3 ${../tools/sqlite-to-turso.py} "$@"
  '';
}
