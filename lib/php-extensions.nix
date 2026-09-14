# Native PHP extensions from the WordPress SQLite Anywhere plugin flake, built
# from source against the exact PHP this flake ships (ZTS, FrankenPHP-ready).
#
#   - wp_mysql_parser: accelerates the MySQL lexer/parser of the SQLite
#     driver (~15x parser speedup over pure PHP). Upstream's crate, reached
#     through the plugin's driver submodule.
#   - wp_d1_client: a native HTTP client for the Cloudflare D1 proxy
#     protocol, holding a connection pool that persists across requests.
#   - wp_turso: the Turso backend's pooled HTTP client, and the embedded
#     replica the PHP process holds open for `database.turso.embedded`.
#
# Returns an attrset with the extension derivations and `iniDir`, a directory
# with an .ini file loading all of them — point PHP_INI_SCAN_DIR at it
# (alongside the PHP buildEnv's own lib directory). Each backend's PHP side
# picks up its own extension and ignores the others.
#
#   mkPhpExtensions { pkgs; php = phpBuild; sqliteAnywhere = wordpress-sqlite-anywhere; }
{
  pkgs,
  php,
  # The wordpress-sqlite-anywhere flake (its `lib` builds the extensions).
  sqliteAnywhere,
  # The Rust toolchain can come from a newer package set than the PHP build.
  rustPkgs ? pkgs,
}:
let
  wp-mysql-parser = sqliteAnywhere.lib.mkMysqlParserExtension { inherit pkgs php rustPkgs; };
  wp-d1-client = sqliteAnywhere.lib.mkD1ClientExtension { inherit pkgs php rustPkgs; };
  wp-turso = sqliteAnywhere.lib.mkTursoExtension { inherit pkgs php rustPkgs; };
in
{
  inherit wp-mysql-parser wp-d1-client wp-turso;

  iniDir = pkgs.writeTextDir "wp-native-extensions.ini" ''
    extension=${wp-mysql-parser}/lib/libwp_mysql_parser.so
    extension=${wp-d1-client}/lib/libwp_d1_client.so
    extension=${wp-turso}/lib/libwp_turso.so
  '';
}
