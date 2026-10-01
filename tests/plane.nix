# Split-plane posture for services.wordpress-nix, checked without a VM.
#
# Run: nix build .#checks.<system>.plane
#
# The public plane's security posture is almost entirely declarative — a set of
# Caddy matchers and a set of PHP constants — so it can be asserted by reading
# what the module generates. The Caddyfiles are adapted to JSON by FrankenPHP
# (nixpkgs' stock build, so no custom PHP is compiled), which proves the new
# matchers parse at all and lets the routes be inspected.
#
# Live behaviour that needs a running site — that an administrator cannot
# authenticate on the public plane while a subscriber can, over one shared
# database — is in tests/module.nix instead.
{
  pkgs,
  nixosSystem,
  wordpressModule,
}:
let
  inherit (pkgs) lib;

  # Composed from nixpkgs' WordPress rather than the pinned download, so the
  # check never reaches wordpress.org.
  docroot = import ../lib/mk-site-docroot.nix {
    inherit pkgs;
    name = "plane-check-docroot";
    core = "${pkgs.wordpress}/share/wordpress";
  };

  eval =
    settings:
    (nixosSystem {
      modules = [
        wordpressModule
        {
          nixpkgs.pkgs = pkgs;
          boot.isContainer = true;
          system.stateVersion = "25.11";
          services.wordpress-nix.enable = true;
        }
        { services.wordpress-nix = settings; }
      ];
    }).config;

  sharedDb = {
    name = "site";
    user = "site";
    host = "10.20.1.9";
    createLocally = false;
    passwordFile = "/run/secrets/db-password";
  };

  publicSettings = {
    plane = {
      role = "public";
      publicUrl = "https://example.com";
      adminUrl = "https://example.avunu.io";
    };
    source = {
      type = "git";
      path = docroot;
    };
    database = sharedDb;
  };

  public = eval publicSettings;
  # Same plane with visitor logins kept open (membership, social login, a
  # WooCommerce "my account" password reset).
  publicLogin = eval (lib.recursiveUpdate publicSettings { plane.allowLogin = true; });

  admin = eval {
    plane = {
      role = "admin";
      publicUrl = "https://example.com";
      adminUrl = "https://example.avunu.io";
    };
    source.type = "managed";
    database = sharedDb;
  };

  # A single-plane site must be entirely unaffected by any of this.
  single = eval {
    domain = "solo.example.com";
    acmeEmail = "ops@example.com";
    source = {
      type = "git";
      path = docroot;
    };
    database.createLocally = true;
  };

  # --- evaluation-time assertions ------------------------------------------
  cronChecks = [
    (lib.assertMsg (!public.services.wordpress-nix.cron.enable)
      "public plane: cron should default off — the admin plane owns it"
    )
    (lib.assertMsg admin.services.wordpress-nix.cron.enable "admin plane: cron should default on")
    (lib.assertMsg single.services.wordpress-nix.cron.enable "single plane: cron should default on")
  ];

  excludeChecks = [
    (lib.assertMsg
      (public.services.wordpress-nix.plane.excludePlugins == [
        "jwt-auth"
        "gitium"
      ])
      "public plane: jwt-auth and gitium must be dropped by default — jwt-auth refuses the visitor logins this plane serves"
    )
    (lib.assertMsg (admin.services.wordpress-nix.plane.excludePlugins == [ ])
      "admin plane: nothing should be excluded by default"
    )
    (lib.assertMsg (single.services.wordpress-nix.plane.excludePlugins == [ ])
      "single plane: nothing should be excluded by default"
    )
  ];
in
assert lib.all lib.id (cronChecks ++ excludeChecks);
pkgs.runCommand "wordpress-nix-plane"
  {
    nativeBuildInputs = [
      pkgs.frankenphp
      pkgs.jq
    ];
    publicCaddy = public.system.build.wordpressCaddyfile;
    publicLoginCaddy = publicLogin.system.build.wordpressCaddyfile;
    adminCaddy = admin.system.build.wordpressCaddyfile;
    singleCaddy = single.system.build.wordpressCaddyfile;
    publicConf = public.system.build.wordpressConfigPhp;
    adminConf = admin.system.build.wordpressConfigPhp;
  }
  ''
    export HOME=$TMPDIR XDG_DATA_HOME=$TMPDIR XDG_CONFIG_HOME=$TMPDIR

    fail() { echo "FAIL: $*" >&2; exit 1; }

    # Adapting at all is the first assertion: the public plane's matchers are
    # new Caddyfile syntax, and a typo there would otherwise only surface when a
    # site failed to start.
    adapt() { frankenphp adapt --config "$1" --adapter caddyfile; }
    adapt "$publicCaddy"      > public.json
    adapt "$publicLoginCaddy" > publiclogin.json
    adapt "$adminCaddy"       > admin.json
    adapt "$singleCaddy"      > single.json

    # Every short-circuiting response Caddy will produce, wherever it sits in
    # the route tree.
    responses() { jq '[.. | objects | select(.handler? == "static_response")]' "$1"; }

    # --- the public plane turns administration away ------------------------
    responses public.json > public-resp.json

    jq -e 'any(.[]; .status_code == 302
                and ((.headers.Location // []) | any(startswith("https://example.avunu.io"))))' \
      public-resp.json >/dev/null || fail "public plane: no redirect to the admin host"

    jq -e 'any(.[]; .status_code == 403)' public-resp.json >/dev/null \
      || fail "public plane: xmlrpc.php is not refused"

    jq -e 'any(.[]; .status_code == 404)' public-resp.json >/dev/null \
      || fail "public plane: wp-cron.php and stray PHP are not 404'd"

    # The two holes the front end depends on. admin-ajax and admin-post must NOT
    # be swept up by the wp-admin redirect: WooCommerce, Give and every form
    # plugin POST to them from the public side.
    jq -e '[.. | objects | select(.not? != null) | .not[]? | .path[]?]
           | (index("/wp-admin/admin-ajax.php") != null)
             and (index("/wp-admin/admin-post.php") != null)' \
      public.json >/dev/null || fail "public plane: admin-ajax/admin-post are not exempted from the wp-admin redirect"

    # /wp-json is never named in the config: capability checks, not a path list,
    # are what deny administration there.
    if grep -q 'wp-json' "$publicCaddy"; then
      fail "public plane: /wp-json must stay unmentioned — the Store API and form endpoints live there"
    fi

    # --- wp-login.php follows allowLogin -----------------------------------
    # The redirect target is `{uri}`, resolved per request, so the evidence is in
    # the path matcher rather than the Location header.
    logindPaths() { jq '[.. | objects | select(.path? != null) | .path[]]' "$1"; }

    jq -e 'index("/wp-login.php") != null' <(logindPaths public.json) >/dev/null \
      || fail "public plane: wp-login.php should redirect away by default"

    jq -e 'index("/wp-login.php") == null' <(logindPaths publiclogin.json) >/dev/null \
      || fail "plane.allowLogin: wp-login.php must be left reachable"

    # --- the admin plane and single-plane sites are untouched --------------
    # Every plane carries one static_response regardless: php_server's own 308
    # trailing-slash canonicalization. So look for the codes the public plane's
    # blocks produce, not for the absence of short-circuits altogether.
    for f in admin.json single.json; do
      jq -e 'any(.[]; .status_code as $c | [302, 403, 404] | index($c) != null) | not' \
        <(responses "$f") >/dev/null \
        || fail "$f: an administration block leaked onto a plane that should serve everything"
    done

    # --- constants ----------------------------------------------------------
    # The public plane names one host for both, so COOKIEHASH is derived from it
    # and an admin cookie cannot be replayed here.
    grep -qF "define('WP_HOME', 'https://example.com');"    "$publicConf" || fail "public: WP_HOME"
    grep -qF "define('WP_SITEURL', 'https://example.com');" "$publicConf" || fail "public: WP_SITEURL"
    grep -qF "define('DISALLOW_FILE_MODS', true);"          "$publicConf" || fail "public: DISALLOW_FILE_MODS"
    grep -qF "define('DISALLOW_FILE_EDIT', true);"          "$publicConf" || fail "public: DISALLOW_FILE_EDIT"
    grep -qF "define('WP_PLATFORM_PLANE', 'public');"       "$publicConf" || fail "public: WP_PLATFORM_PLANE"
    grep -qF "define('WP_PLATFORM_PLANE_EXCLUDED_PLUGINS', 'jwt-auth,gitium');" \
      "$publicConf" || fail "public: plane plugin exclusions"

    # The admin plane splits them: wp-admin lives on the private host while
    # permalinks and outgoing email still name the real site.
    grep -qF "define('WP_HOME', 'https://example.com');"            "$adminConf" || fail "admin: WP_HOME"
    grep -qF "define('WP_SITEURL', 'https://example.avunu.io');"    "$adminConf" || fail "admin: WP_SITEURL"
    grep -qF "define('WP_PLATFORM_PLANE', 'admin');"                "$adminConf" || fail "admin: WP_PLATFORM_PLANE"
    # Managed mode is the only mode with the mutable git-backed tree gitium
    # needs, so the platform installs and activates it there and nowhere else.
    grep -qF "define('WP_PLATFORM_GITIUM', true);"                  "$adminConf" || fail "admin: gitium not installed"
    if grep -qF "define('WP_PLATFORM_GITIUM'" "$publicConf"; then
      fail "public plane: gitium must not be installed — its working tree is a read-only store path"
    fi
    if grep -qF "define('DISALLOW_FILE_MODS'" "$adminConf"; then
      fail "admin plane: file modifications must stay available — it is the plane that installs plugins"
    fi

    # --- the composed docroot ----------------------------------------------
    test -f ${docroot}/wp-content/mu-plugins/platform-public-plane.php \
      || fail "docroot: platform mu-plugins are not composed in (git mode never copies them)"
    test -f ${docroot}/wp-includes/version.php || fail "docroot: core is missing"
    test ! -e ${docroot}/wp-config.php || fail "docroot: wp-config.php must not be in the store tree"
    test ! -e ${docroot}/wp-content/uploads || fail "docroot: uploads must stay a real directory, not a store copy"
    test ! -e ${docroot}/wp-content/plugins/gitium \
      || fail "docroot: gitium must not reach the public plane's tree"

    touch $out
  ''
