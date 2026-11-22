# restore-core-keys: rewrite non-standard core-table keys in a MySQL dump
# (index-wp-mysql-for-speed and friends) back to wp_get_db_schema().
#
# The schema comes from the core the site will run: `--core DIR`, or a Bedrock
# site's web/wp when run from its root. Without either, it falls back to the
# schema of nixpkgs' WordPress, baked in at build time.
#
#   nix run github:Avunu/wordpress#restore-core-keys -- dump.sql fixed.sql
#   restore-core-keys --core result/web/wp dump.sql fixed.sql
{
  pkgs,
  # A PHP able to run wp-admin/includes/schema.php (any build will do).
  php,
}:
let
  wordpressCore = "${pkgs.wordpress}/share/wordpress";
  standardSchema =
    pkgs.runCommandLocal "wordpress-standard-schema.sql" { nativeBuildInputs = [ php ]; }
      ''
        php ${../tools/standard-schema.php} ${wordpressCore} wp_ > $out
        grep -q 'CREATE TABLE wp_options' $out
      '';
in
pkgs.writeShellApplication {
  name = "restore-core-keys";
  runtimeInputs = [
    pkgs.python3
    php
  ];
  text = ''
    usage() { echo "usage: restore-core-keys [--core DIR] <dump.sql> <out.sql>" >&2; exit 1; }
    core=""
    if [ "''${1:-}" = "--core" ]; then
      [ "$#" -ge 2 ] || usage
      core=$2
      shift 2
    elif [ -f web/wp/wp-admin/includes/schema.php ]; then
      core=web/wp
    fi
    [ "$#" -eq 2 ] || usage

    schema=${standardSchema}
    if [ -n "$core" ]; then
      schema=$(mktemp)
      trap 'rm -f "$schema"' EXIT
      php ${../tools/standard-schema.php} "$core" wp_ > "$schema"
      grep -q 'CREATE TABLE wp_options' "$schema" || { echo "restore-core-keys: no schema from $core" >&2; exit 1; }
      echo "restore-core-keys: schema from $core" >&2
    fi
    exec python3 ${../tools/restore-core-keys.py} "$1" "$schema" "$2"
  '';
}
