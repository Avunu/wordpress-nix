# restore-core-keys: rewrite non-standard core-table keys in a MySQL dump
# (index-wp-mysql-for-speed and friends) back to wp_get_db_schema(), taken
# from the platform's pinned core so the keys match what the site will run.
#
#   nix run github:Avunu/wordpress#restore-core-keys -- dump.sql fixed.sql
{
  pkgs,
  # A PHP able to run wp-admin/includes/schema.php (any build will do).
  php,
}:
let
  wordpressCore = import ./wordpress-core.nix { inherit pkgs; };
  standardSchema = pkgs.runCommandLocal "wordpress-standard-schema.sql" { nativeBuildInputs = [ php ]; } ''
    php ${../tools/standard-schema.php} ${wordpressCore} wp_ > $out
    grep -q 'CREATE TABLE wp_options' $out
  '';
in
pkgs.writeShellApplication {
  name = "restore-core-keys";
  runtimeInputs = [ pkgs.python3 ];
  text = ''
    if [ "$#" -ne 2 ]; then
      echo "usage: restore-core-keys <dump.sql> <out.sql>" >&2
      exit 1
    fi
    exec python3 ${../tools/restore-core-keys.py} "$1" ${standardSchema} "$2"
  '';
}
