# lib/mk-bedrock-site.nix over tests/fixtures/bedrock-site: stock Bedrock
# 1.31.9 whose composer.lock resolves from a local Composer `artifact`
# repository (tests/fixtures/bedrock-site/artifacts, make-artifacts.py), so
# nothing here reaches the network. The two Composer plugins and the
# roots/wordpress metapackage are the real upstream packages; core, the
# plugin, the theme and the PSR-4 library are small stand-ins.
#
#   bedrock-build          the package: autoloader files, InstalledVersions,
#                          web/wp/wp-includes/version.php, committed code beside
#                          Composer's (not nested), platform mu-plugins, the
#                          runtime symlinks, vendor-src; the autoloader resolves
#                          a vendor class; the vendor FOD lacks installed.php and
#                          InstalledVersions.php, so the install hook wrote them.
#   bedrock-build-rebuild  the vendor FOD built a second time under another
#                          name, against the same committed hash: a stable hash
#                          over two clean builds.
#   bedrock-build-collision  a committed plugin named like a Composer package
#                          fails the build.
#   bedrock-build-eval     passthru: the WordPress version from composer.lock.
{
  pkgs,
  checks,
  # mkBedrockSite over the fixture, with composerRepositoryPaths = [ "artifacts" ],
  # productionOnlyPlugins = [ "fixture-plugin" ], activeTheme = "fixture-theme".
  site,
}:
let
  inherit (pkgs) lib;

  # The FOD's store path is its name and its hash, so another name is another
  # build from scratch, which must land on the same committed hash.
  rebuilt = site.vendor.overrideAttrs { name = "bedrock-fixture-rebuild-composer-vendor-0"; };

  # The committed premium plugin step-aside (preBuild) must not paper over a
  # real clash: a committed directory named like a Composer-installed plugin.
  collision = site.overrideAttrs (old: {
    postConfigure = old.postConfigure + ''
      mkdir -p web/app/plugins/fixture-plugin
      echo '<?php' > web/app/plugins/fixture-plugin/fixture-plugin.php
    '';
  });

  php = lib.getExe pkgs.php84;
in
{
  bedrock-build = checks.script "bedrock-build" { tools = [ pkgs.diffutils ]; } ''
    fail() { echo "FAIL: $*" >&2; exit 1; }
    pkg=${site}
    vendor=${site.vendor}

    for f in vendor/autoload.php vendor/composer/autoload_real.php \
             vendor/composer/installed.php vendor/composer/InstalledVersions.php \
             web/wp/wp-includes/version.php web/index.php web/wp-config.php \
             config/application.php; do
      [ -f "$pkg/$f" ] || fail "$f is missing from the package"
    done
    grep -q "wp_version = '7.1.5'" "$pkg/web/wp/wp-includes/version.php" || fail "core is not 7.1.5"

    # The FOD drops both files; the package has them, so the install hook's
    # offline `composer install` wrote them (docs/reference/bedrock-build.md).
    [ ! -e "$vendor/vendor/composer/installed.php" ] || fail "the FOD kept installed.php"
    [ ! -e "$vendor/vendor/composer/InstalledVersions.php" ] || fail "the FOD kept InstalledVersions.php"
    [ -d "$vendor/web/wp" ] || fail "the FOD lost web/wp"

    # Composer's and the repository's code side by side, nothing nested.
    for d in web/app/plugins/fixture-plugin web/app/plugins/committed-premium \
             web/app/themes/fixture-theme web/app/mu-plugins/bedrock-disallow-indexing; do
      [ -d "$pkg/$d" ] || fail "$d is missing"
    done
    for d in web/app/plugins/plugins web/app/themes/themes web/app/mu-plugins/mu-plugins; do
      [ ! -e "$pkg/$d" ] || fail "$d: an installer path was nested into the committed one"
    done
    [ -f "$pkg/web/app/mu-plugins/bedrock-autoloader.php" ] || fail "Bedrock's own mu-plugin is gone"
    for m in platform-environment platform-nonce platform-user-enumeration; do
      [ -f "$pkg/web/app/mu-plugins/$m.php" ] || fail "platform mu-plugin $m.php was not injected"
    done

    # Runtime state outside the store; no secrets or VCS state in it.
    [ "$(readlink "$pkg/web/app/uploads")" = /var/lib/wordpress/uploads ] || fail "uploads is not the state link"
    [ "$(readlink "$pkg/.env")" = /run/wordpress/bedrock.env ] || fail ".env is not the runtime link"
    [ ! -e "$pkg/web/app/cache" ] && [ ! -e "$pkg/web/app/upgrade" ] || fail "cache/upgrade left in the store tree"
    [ ! -e "$pkg/.env.example" ] || fail ".env.example reached the package"
    [ ! -e "$pkg/nix" ] || fail "nix/ reached the package"

    # The FOD is a runtime reference of the package.
    [ "$(cat "$pkg/share/wordpress-nix/vendor-src")" = "$vendor" ] || fail "vendor-src does not name the FOD"

    # The autoloader the hook wrote resolves a vendor class, and Composer's
    # runtime API knows the locked core.
    said=$(cd "$pkg" && ${php} -n -r '
      require "vendor/autoload.php";
      echo \Fixture\Greeting\Greeting::hello(), "|";
      echo \Composer\InstalledVersions::getPrettyVersion("roots/wordpress-no-content");
    ')
    [ "$said" = "hello from the vendor tree|7.1.5" ] || fail "autoloader: got '$said'"
    echo "bedrock-build: ok"
  '';

  bedrock-build-rebuild = checks.script "bedrock-build-rebuild" { tools = [ pkgs.diffutils ]; } ''
    diff -r ${site.vendor} ${rebuilt}
    echo "bedrock-build-rebuild: two builds, one hash"
  '';

  bedrock-build-collision = checks.script "bedrock-build-collision" { } ''
    grep -q 'collides with a Composer package of the same name' \
      ${pkgs.testers.testBuildFailure collision}/testBuildFailure.log
  '';

  bedrock-build-eval = checks.eval "bedrock-build-eval" {
    expected = {
      wordpressVersion = "7.1.5";
      productionOnlyPlugins = [ "fixture-plugin" ];
      activeTheme = "fixture-theme";
      version = "7.1.5";
    };
    actual = {
      inherit (site.passthru.wordpressNix) wordpressVersion productionOnlyPlugins activeTheme;
      inherit (site) version;
    };
  };
}
