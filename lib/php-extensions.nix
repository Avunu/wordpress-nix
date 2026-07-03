# Native PHP extensions of the SQLite Database Integration project, built
# from source against the exact PHP this flake ships (ZTS, FrankenPHP-ready).
#
#   - wp_mysql_parser: accelerates the MySQL lexer/parser of the SQLite
#     driver (~15x parser speedup over pure PHP).
#   - wp_d1_client: a native HTTP client for the Cloudflare D1 proxy
#     protocol, holding a connection pool that persists across requests.
#
# Returns an attrset with the two extension derivations and `iniDir`, a
# directory with an .ini file loading both — point PHP_INI_SCAN_DIR at it
# (alongside the PHP buildEnv's own lib directory).
#
#   mkPhpExtensions { pkgs; php = phpBuild; src = sqlite-database-integration; }
{
  pkgs,
  php,
  src,
  # The Rust toolchain can come from a newer package set than the PHP build.
  rustPkgs ? pkgs,
}:
let
  mkExtension =
    {
      pname,
      dir,
    }:
    rustPkgs.rustPlatform.buildRustPackage {
      inherit pname;
      version = "0.1.0";
      src = "${src}/packages/${dir}";
      cargoLock.lockFile = "${src}/packages/${dir}/Cargo.lock";

      # ext-php-rs generates bindings against the PHP headers at build time.
      nativeBuildInputs = [ rustPkgs.rustPlatform.bindgenHook ];
      env = {
        PHP_CONFIG = "${php.unwrapped.dev}/bin/php-config";
        PHP = "${php.unwrapped}/bin/php";
      };

      # The crates' tests require a live PHP runtime; extension correctness
      # is verified by the driver test suites instead.
      doCheck = false;
    };

  wp-mysql-parser = mkExtension {
    pname = "wp_mysql_parser";
    dir = "php-ext-wp-mysql-parser";
  };

  wp-d1-client = mkExtension {
    pname = "wp_d1_client";
    dir = "php-ext-wp-d1-client";
  };
in
{
  inherit wp-mysql-parser wp-d1-client;

  iniDir = pkgs.writeTextDir "wp-native-extensions.ini" ''
    extension=${wp-mysql-parser}/lib/libwp_mysql_parser.so
    extension=${wp-d1-client}/lib/libwp_d1_client.so
  '';
}
