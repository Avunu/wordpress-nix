# `wordpress-init --migrate` over a synthetic classic WordPress install (§8.7).
#
# A script check, not a VM test: it is offline by construction. The catalog
# (wordpress.org, WP Packages, the published files) is WORDPRESS_NIX_CATALOG, the
# conversion runs with --skip-lock, and the lock step's ordering is checked against
# stand-in `composer` and `nix` commands that assert what the git index holds.
{
  pkgs,
  checks,
  wordpress-init,
}:
checks.script "migrate-classic"
  {
    tools = with pkgs; [
      wordpress-init
      git
      jq
      python3
      diffutils
      findutils
      coreutils
      gnugrep
    ];
  }
  ''
    bash ${./migrate-classic.sh} \
      ${pkgs.lib.getExe wordpress-init} \
      ${./make-classic-wp.sh} 2>&1 | tee "$out"
  ''
