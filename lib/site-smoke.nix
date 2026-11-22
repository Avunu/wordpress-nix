# `checks.smoke` of a site flake (moonshot v3 §6.2, the `ci / smoke` gate): the
# site's own package, booted in a NixOS VM by nixosModules.default with its
# MariaDB, as the cluster runs it. tests/module.nix tests the module; this tests
# the site: its plugins and theme load on this platform.
#
#   - `wp core install`, `wp plugin activate --all` (premium plugins unlicensed),
#     the site's activeTheme;
#   - `/`, `/wp/wp-login.php` and `/wp-json/` answer 200;
#   - the code tree cannot be written; `/app/uploads/2024/01/x.php` is 403;
#   - no PHP fatal or warning in wp-cli's output (stdout and stderr) or the
#     journal, and no permission error in the journal.
#
# Needs KVM (the `kvm` system feature): CI runs it on the kvm runners.
{
  pkgs,
  lib ? pkgs.lib,
  wordpressModule,
  # The site's package (lib.mkBedrockSite); its passthru.wordpressNix carries
  # activeTheme.
  package,
  name ? package.pname,
}:
let
  inherit (package.passthru.wordpressNix or { }) activeTheme;
  # A statement or nothing: the test driver's type check refuses a branch on a
  # constant.
  activateTheme = lib.optionalString (
    activeTheme != null
  ) "wp(${builtins.toJSON "theme activate ${lib.escapeShellArg activeTheme}"})";

  # The eight Bedrock salts, as the cluster's generated secret holds them.
  secrets = pkgs.writeText "${name}-smoke-env" (
    lib.concatMapStrings (k: "${k}='smoke-${lib.toLower k}-0123456789abcdef'\n") [
      "AUTH_KEY"
      "SECURE_AUTH_KEY"
      "LOGGED_IN_KEY"
      "NONCE_KEY"
      "AUTH_SALT"
      "SECURE_AUTH_SALT"
      "LOGGED_IN_SALT"
      "NONCE_SALT"
    ]
  );
in
pkgs.testers.runNixOSTest {
  name = "${name}-smoke";

  nodes.site =
    { ... }:
    {
      imports = [ wordpressModule ];
      virtualisation.memorySize = 2048;
      environment.etc."wordpress-secrets.env" = {
        source = secrets;
        mode = "0400";
      };
      services.wordpress-nix = {
        enable = true;
        inherit package;
        phpOptimize = false; # the clang/LTO build is not what this checks
        socketPath = "/run/wordpress/wp.sock";
        environmentFiles = [ "/etc/wordpress-secrets.env" ];
      };
    };

  testScript = ''
    import re
    import shlex

    PKG = "${package}"

    # PHP's own reports, logged ("PHP Warning:  …", stderr) or displayed ("Warning: … on
    # line N", stdout); wp-cli's own "Warning: …" lines carry no "on line".
    PHP_PROBLEM = re.compile(
        r"PHP (?:Fatal error|Warning|Parse error)"
        r"|^(?:Fatal error|Warning|Parse error): .* on line [0-9]+",
        re.MULTILINE,
    )

    def wp(args):
        out = site.succeed(f"runuser -u wordpress -- wp {args} 2>&1")
        found = PHP_PROBLEM.search(out)
        if found:
            raise AssertionError(f"wp {args}: {found.group(0)}\n{out}")
        return out.strip()

    def request(path):
        return site.succeed(
            "curl -sS -o /dev/null -w '%{http_code}' --max-time 60"
            f" --unix-socket /run/wordpress/wp.sock http://localhost{shlex.quote(path)}"
        ).strip()

    start_all()
    for unit in ("wordpress-init", "mysql", "wordpress-migrate", "frankenphp"):
        site.wait_for_unit(f"{unit}.service")
    site.wait_for_file("/run/wordpress/wp.sock")

    with subtest("install, activate every plugin and the theme"):
        wp("core install --url=http://localhost --title=Smoke --admin_user=admin"
           " --admin_password=smoke_pw_123 --admin_email=admin@example.org --skip-email")
        wp("plugin activate --all")
        ${activateTheme}
        wp("rewrite structure /%postname%/")

    with subtest("it serves"):
        for path in ("/", "/wp/wp-login.php", "/wp-json/"):
            code = request(path)
            assert code == "200", f"{path}: {code}"

    with subtest("read-only code, no PHP under uploads"):
        site.fail(f"runuser -u wordpress -- touch {PKG}/web/app/smoke-write")
        site.succeed(
            "install -d -o wordpress -g wordpress /var/lib/wordpress/uploads/2024/01"
            " && echo '<?php echo 1;' > /var/lib/wordpress/uploads/2024/01/x.php"
        )
        assert request("/app/uploads/2024/01/x.php") == "403"

    with subtest("no PHP fatal or warning, no permission error"):
        journal = site.succeed(
            "journalctl -o cat --no-pager -u frankenphp -u wordpress-init -u wordpress-migrate -u wordpress-cron"
        )
        for bad in ("PHP Fatal", "PHP Warning", "Permission denied", "Read-only file system", "EACCES", "EROFS"):
            assert bad not in journal, f"{bad} in the journal:\n{journal}"
  '';
}
