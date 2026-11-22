# NixOS VM test for services.wordpress-nix (v3): one Bedrock site, read-only.
#
# Run: nix build .#checks.<system>.module -L   (needs KVM)
#
# The site is templates/bedrock-site built by lib.mkBedrockSite: stock Bedrock,
# real core, two-factor and twentytwentyfive from its composer.lock. Its vendor
# tree is a fixed-output derivation fetched from Composer's registries, so a
# build without a substituter needs the network (the sandbox allows that to an
# FOD). One test-only mu-plugin is injected (tests/fixtures/module-probe.php).
#
#   site  over a unix socket, as the tunnel reaches it in the cluster:
#     - first boot on an empty database: migrate and cron exit 0
#     - installed, it serves; old admin, login, uploads and content URLs move
#     - nothing under uploads runs as PHP; private prefixes are refused
#     - the code tree cannot be written, by PHP or by `wp plugin install`
#     - Bedrock's environment: settings and environmentFiles, 0400, env-only S3;
#       the secret nowhere in the system's closure; a secret rotated in place
#       reaches PHP without a switch
#     - platform-environment.php: two-factor for administrators, no application
#       passwords, no update checks (and no request to make one), no executable
#       uploads, the constants bridge, plugins kept to their environment (and
#       kept in the database when another plugin is activated)
#     - a failed migrate is rolled back from its snapshot; WordPress failing to
#       load fails the migrate; the next one succeeds
#     - every unit has its own /tmp; OOM scores; MariaDB not on TCP; no
#       permission errors
#   tcp   over :80, uninstalled: the installer, through CAP_NET_BIND_SERVICE
#
# Not here yet, so M1 stays open: wp-cloud-files 0.6 offloading to MinIO with
# env-only S3_*, and a Woo-downloadable attachment staying local. 0.6 is
# unreleased (0.5 reads constants only and is a `library`, not a
# `wordpress-plugin`); until it is, this test stops at what the platform owns:
# the S3_* names reach PHP from the environment alone, and the private prefixes
# are refused over HTTP.
{
  pkgs,
  wordpressModule,
  mkBedrockSite,
}:
let
  package = mkBedrockSite {
    inherit pkgs;
    name = "module-test";
    src = ../templates/bedrock-site;
    php = pkgs.php84;
    developmentOnlyPlugins = [ "wpcodebox2" ];
    muPlugins = [
      ../mu-plugins/platform-environment.php
      ../mu-plugins/platform-nonce.php
      ../mu-plugins/platform-user-enumeration.php
      ./fixtures/module-probe.php
    ];
  };

  # What the cluster's secrets would hold: the salts and the S3 key pair. The
  # S3 secret is a distinctive (and plainly fake) marker, to be looked for in
  # the system's closure.
  s3Secret = "s3-secret-closure-probe-marker";
  secrets = pkgs.writeText "module-test-env" ''
    ${pkgs.lib.concatMapStrings (k: "${k}='test-${pkgs.lib.toLower k}-0123456789abcdef'\n") [
      "AUTH_KEY"
      "SECURE_AUTH_KEY"
      "LOGGED_IN_KEY"
      "NONCE_KEY"
      "AUTH_SALT"
      "SECURE_AUTH_SALT"
      "LOGGED_IN_SALT"
      "NONCE_SALT"
    ]}
    S3_KEY='test-key'
    S3_SECRET='${s3Secret}'
  '';

  common = {
    virtualisation.memorySize = 2048;
    environment.systemPackages = [
      pkgs.jq
      pkgs.unzip
      pkgs.zip
    ];
    # Root's 0400, as agenix leaves a secret: read through LoadCredential.
    environment.etc."wordpress-secrets.env" = {
      source = secrets;
      mode = "0400";
    };
  };
in
pkgs.testers.runNixOSTest {
  name = "wordpress-nix";

  nodes = {
    site =
      { ... }:
      {
        imports = [
          wordpressModule
          common
        ];
        services.wordpress-nix = {
          enable = true;
          inherit package;
          phpOptimize = false; # skip the slow clang/LTO build in CI
          socketPath = "/run/wordpress/wp.sock";
          environmentFiles = [ "/etc/wordpress-secrets.env" ];
          settings = {
            S3_BUCKET = "test-media";
            S3_ENDPOINT = "http://minio.invalid:9000";
            S3_PUBLIC_URL = "https://media.example.org";
            S3_DIRECT_UPLOADS = true;
          };
          cron.randomizedDelaySec = "5s";
        };
      };

    tcp =
      { ... }:
      {
        imports = [
          wordpressModule
          common
        ];
        services.wordpress-nix = {
          enable = true;
          inherit package;
          phpOptimize = false;
          environmentFiles = [ "/etc/wordpress-secrets.env" ];
          migrate.enable = false;
        };
      };
  };

  testScript = ''
    import hashlib
    import json
    import shlex

    PKG = "${package}"

    def wp(args, machine=site):
        return machine.succeed(f"runuser -u wordpress -- wp {args}").strip()

    def php(code):
        return wp(f"eval {shlex.quote(code)}")

    def request(path, fmt="%{http_code}", headers=""):
        return site.succeed(
            f"curl -sS -o /dev/null -w {shlex.quote(fmt)} --max-time 30"
            f" --unix-socket /run/wordpress/wp.sock {headers} http://localhost{path}"
        ).strip()

    def body(path, headers=""):
        return site.succeed(
            f"curl -sS --max-time 30 --unix-socket /run/wordpress/wp.sock {headers} http://localhost{path}"
        )

    def unit_ok(unit):
        site.succeed(f"systemctl show -p Result {unit} | grep -qx Result=success")

    start_all()

    with subtest("first boot, empty database: migrate and cron have nothing to do"):
        for unit in ("wordpress-init", "mysql", "wordpress-migrate", "frankenphp"):
            site.wait_for_unit(f"{unit}.service")
        site.wait_for_file("/run/wordpress/wp.sock")
        unit_ok("wordpress-migrate.service")
        site.succeed("journalctl -u wordpress-migrate -o cat | grep -q 'not installed'")
        site.succeed("systemctl start wordpress-cron.service")
        unit_ok("wordpress-cron.service")
        site.succeed("systemctl is-active wordpress-cron.timer")
        site.succeed("systemctl show -p RandomizedDelayUSec wordpress-cron.timer | grep -qx RandomizedDelayUSec=5s")

    with subtest("Bedrock's environment: settings and secrets, private to the service user"):
        site.succeed("stat -c '%a %U' /run/wordpress/bedrock.env | grep -qx '400 wordpress'")
        site.succeed(f"test \"$(readlink {PKG}/.env)\" = /run/wordpress/bedrock.env")
        site.succeed("grep -qx \"S3_KEY='test-key'\" /run/wordpress/bedrock.env")
        site.succeed("grep -qx \"WP_ENV='production'\" /run/wordpress/bedrock.env")
        # The secret is in the system's closure only in this test's own
        # secrets file (a store path here, agenix's /run/agenix in the cluster).
        site.succeed("grep -qF ${s3Secret} ${secrets}")
        site.succeed("nix-store -qR /run/current-system | grep -vxF ${secrets} > /tmp/closure")
        site.succeed("grep -qxF ${package} /tmp/closure")
        found = site.succeed("xargs -a /tmp/closure grep -rlFs ${s3Secret} || true").strip()
        assert found == "", f"the S3 secret is in the store:\n{found}"

    with subtest("installed, the site serves"):
        wp("core install --url=http://localhost --title=ModuleSite --admin_user=admin"
           " --admin_password=admin_pw_123 --admin_email=admin@example.com --skip-email")
        wp("plugin activate --all")
        wp("rewrite structure /%postname%/")
        # wp-cli reads the same environment (env-only S3: the key from a file).
        assert php("echo WP_ENV;") == "production"
        assert php('echo getenv("S3_KEY");') == "test-key"
        assert php('echo getenv("S3_BUCKET");') == "test-media"
        page = body("/")
        assert "ModuleSite" in page and "</html>" in page, page[:2000]
        assert request("/wp-json/") == "200"
        assert request("/wp/wp-login.php") == "200"
        assert request("/wp-admin/") == "301"
        assert request("/wp-admin/", "%{redirect_url}").endswith("/wp/wp-admin/")
        assert request("/wp-login.php?x=1") == "308"
        assert request("/wp-login.php?x=1", "%{redirect_url}").endswith("/wp/wp-login.php?x=1")

    with subtest("behind the socket, PHP takes the client from Cloudflare's headers"):
        seen = json.loads(body("/wp-json/probe/v1/env",
                               "-H 'CF-Connecting-IP: 203.0.113.7' -H 'X-Forwarded-Proto: https'"))
        assert seen["remote_addr"] == "203.0.113.7", seen
        assert seen["https"] is True, seen
        # env-only S3: the names reach the web server's PHP from the environment.
        assert seen["s3_bucket"] == "test-media" and seen["s3_key_set"] is True, seen
        assert seen["tmp"] == "/tmp/", seen

    with subtest("the code tree is read-only; uploads are writable"):
        seen = json.loads(body("/wp-json/probe/v1/env"))
        assert seen["docroot_rw"] is False and seen["uploads_rw"] is True, seen
        site.succeed("test -f /var/lib/wordpress/uploads/probe.txt")
        assert php("var_export(DISALLOW_FILE_MODS);") == "true"
        assert php("var_export(wp_is_file_mod_allowed(\"probe\"));") == "false"
        site.succeed(
            "mkdir -p /tmp/p/hello-probe && echo '<?php /* Plugin Name: hello-probe */' > /tmp/p/hello-probe/hello-probe.php"
            " && cd /tmp/p && zip -qr /tmp/hello-probe.zip hello-probe && chmod 0644 /tmp/hello-probe.zip"
        )
        site.fail("runuser -u wordpress -- wp plugin install /tmp/hello-probe.zip")
        site.fail(f"test -e {PKG}/web/app/plugins/hello-probe")

    with subtest("routes: nothing under uploads runs as PHP; private prefixes refused"):
        site.succeed(
            "cd /var/lib/wordpress/uploads && install -d -o wordpress -g wordpress 2024/01 cwicly woocommerce_uploads"
            " && echo '<?php echo 1;' > 2024/01/x.php && echo '<?php echo 1;' > x.phtml"
            " && echo zip > woocommerce_uploads/a.zip && echo 'body{}' > cwicly/x.css"
            " && chown -R wordpress:wordpress ."
        )
        assert request("/app/uploads/2024/01/x.php") == "403"
        assert request("/app/uploads/x.phtml") == "403"
        assert request("/app/uploads/woocommerce_uploads/a.zip") == "403"
        assert request("/app/uploads/cwicly/x.css") == "200"
        assert request("/wp-content/uploads/2024/01/a.jpg") == "301"
        assert request("/wp-content/uploads/2024/01/a.jpg", "%{redirect_url}") == "https://media.example.org/2024/01/a.jpg"
        assert request("/wp-content/uploads/cwicly/x.css", "%{redirect_url}").endswith("/app/uploads/cwicly/x.css")
        assert request("/wp-content/uploads/woocommerce_uploads/a.zip", "%{redirect_url}").endswith(
            "/app/uploads/woocommerce_uploads/a.zip")
        assert request("/wp-content/plugins/two-factor/readme.txt") == "200"
        assert request("/wp/wp-includes/version.php") == "404"
        assert request("/wp/wp-includes/js/tinymce/wp-tinymce.php") == "200"
        assert request("/xmlrpc.php") == "403"
        assert request("/wp/xmlrpc.php") == "403"
        assert request("/wp/wp-cron.php") == "404"
        assert request("/wp-config.php") == "404"

    with subtest("platform-environment.php"):
        # Two-factor: the Email provider for anyone who can manage_options.
        assert php('echo get_class(Two_Factor_Core::get_primary_provider_for_user(1));') == "Two_Factor_Email"
        wp("user create sub sub@example.com --role=subscriber --user_pass=sub_pw_123")
        assert php('var_export(Two_Factor_Core::is_user_using_two_factor(get_user_by("login", "sub")->ID));') == "false"
        assert php("var_export(wp_is_application_passwords_available());") == "false"
        # No update checks: the transients are answered locally, core's checks
        # are unhooked, and one started anyway never leaves.
        assert php('echo json_encode(get_site_transient("update_plugins")->response);') == "[]"
        site.succeed("rm -f /var/lib/wordpress/probe-http.log")
        for event in ("wp_update_plugins", "wp_update_themes", "wp_version_check"):
            wp(f"cron event schedule {event} now")
            wp(f"cron event run {event}")
        php('do_action("load-plugins.php"); do_action("load-themes.php"); do_action("load-update-core.php");')
        wp("plugin list")
        assert php('echo wp_remote_post("https://api.wordpress.org/plugins/update-check/1.1/")->get_error_code();') \
            == "wordpress_nix_read_only"
        php('wp_remote_get("http://probe.invalid/");')  # the probe sees what does leave
        site.succeed("grep -q probe.invalid /var/lib/wordpress/probe-http.log")
        site.fail("grep api.wordpress.org /var/lib/wordpress/probe-http.log")
        assert php('echo has_action("init", "wp_schedule_update_checks") ? "on" : "off";') == "off"
        # The constants bridge and the admin memory limit.
        assert php("echo WC_LOG_HANDLER;") == "WC_Log_Handler_DB"
        assert php("var_export(S3_DIRECT_UPLOADS);") == "true"
        assert php('echo apply_filters("admin_memory_limit", WP_MAX_MEMORY_LIMIT);') == "384M"
        # Executable uploads are refused before they are stored.
        assert php('echo apply_filters("wp_handle_upload_prefilter", ["name" => "x.php.jpg"])["error"] ?? "none";') != "none"
        assert php('echo apply_filters("wp_handle_upload_prefilter", ["name" => "x.jpg"])["error"] ?? "none";') == "none"
        # Development-only plugins never load in production.
        assert php('echo json_encode(wp_platform_filter_active_plugins(["wpcodebox2/wpcodebox2.php", "two-factor/two-factor.php"]));') \
            == '["two-factor\\/two-factor.php"]'
        # ... and stay active in the database when a plugin is (de)activated
        # here (a `wp restore` into dev still has WPCodeBox on).
        raw = "global $wpdb; echo $wpdb->get_var(\"SELECT option_value FROM $wpdb->options WHERE option_name = 'active_plugins'\");"
        php('global $wpdb; $p = get_option("active_plugins"); $p[] = "wpcodebox2/wpcodebox2.php";'
            ' $wpdb->update($wpdb->options, ["option_value" => serialize($p)], ["option_name" => "active_plugins"]);')
        assert "wpcodebox2/wpcodebox2.php" in php(raw)
        assert "wpcodebox2" not in php('echo json_encode(get_option("active_plugins"));')
        wp("plugin deactivate two-factor")
        stored = php(raw)
        assert "wpcodebox2/wpcodebox2.php" in stored and "two-factor" not in stored, stored
        wp("plugin activate two-factor")
        stored = php(raw)
        assert "wpcodebox2/wpcodebox2.php" in stored and "two-factor/two-factor.php" in stored, stored
        # Production sends mail.
        assert php('echo has_filter("pre_wp_mail") ? "off" : "on";') == "on"

    with subtest("a failed migrate is rolled back from its verified snapshot"):
        wp("option update blogname Before")
        site.succeed("touch /var/lib/wordpress/probe-fail-migrate && rm -f /var/lib/wordpress/.wordpress-migrate-build")
        site.fail("systemctl restart wordpress-migrate.service")
        assert wp("option get blogname") == "Before", "the rollback did not restore the database"
        site.succeed("journalctl -u wordpress-migrate -o cat | grep -q 'MIGRATION FAILED'")
        site.succeed("journalctl -u wordpress-migrate -o cat | grep -q 'rollback: restored'")
        site.succeed("stat -c '%a' /var/lib/wordpress/snapshots | grep -qx 700")
        site.succeed("stat -c '%a' /var/lib/wordpress/snapshots/premigrate-*.sql.gz | sort -u | grep -qx 600")
        # The site kept serving through it.
        assert request("/") == "200"

        site.succeed("rm /var/lib/wordpress/probe-fail-migrate")

        # WordPress that does not load is a failed migrate, not "not installed".
        site.succeed("touch /var/lib/wordpress/probe-broken")
        site.fail("systemctl restart wordpress-migrate.service")
        site.succeed("journalctl -u wordpress-migrate -o cat | grep -q 'is-installed failed (exit 255)'")
        site.succeed("rm /var/lib/wordpress/probe-broken")

        site.succeed("systemctl restart wordpress-migrate.service")
        unit_ok("wordpress-migrate.service")
        site.succeed(f"test \"$(cat /var/lib/wordpress/.wordpress-migrate-build)\" = {PKG}")
        # The same build again: nothing to do.
        site.succeed("systemctl restart wordpress-migrate.service")
        site.succeed("journalctl -u wordpress-migrate -o cat | grep -q 'build unchanged'")

    with subtest("cron runs on the installed site"):
        site.succeed("systemctl start wordpress-cron.service")
        unit_ok("wordpress-cron.service")

    with subtest("every unit has its own /tmp; PHP dies before the database does"):
        for unit in ("wordpress-init", "wordpress-migrate", "frankenphp", "wordpress-cron"):
            site.succeed(f"systemctl show -p PrivateTmp {unit} | grep -qx PrivateTmp=yes")
            site.succeed(f"systemctl show -p ProtectSystem {unit} | grep -qx ProtectSystem=strict")
        site.succeed("systemctl show -p OOMScoreAdjust mysql | grep -qx OOMScoreAdjust=-500")
        site.succeed("systemctl show -p OOMScoreAdjust frankenphp | grep -qx OOMScoreAdjust=300")
        site.fail("ss -HltnO | grep -qE ':(80|443)\\s'")
        # MariaDB is on its unix socket only.
        listening = site.succeed("ss -Hltn")
        assert ":3306" not in listening, listening
        site.succeed("test -S /run/mysqld/mysqld.sock")
        site.succeed("stat -c '%a' /run/wordpress/wp.sock | grep -qx 660")

    with subtest("a secret rotated in place reaches PHP without a switch"):
        before = site.succeed("systemctl show -p InvocationID --value frankenphp").strip()
        site.succeed(
            "sed s/test-key/rotated-key/ /etc/wordpress-secrets.env > /etc/wordpress-secrets.env.new"
            " && chmod 0400 /etc/wordpress-secrets.env.new"
            " && mv -f /etc/wordpress-secrets.env.new /etc/wordpress-secrets.env"
        )
        site.wait_until_succeeds("grep -qx \"S3_KEY='rotated-key'\" /run/wordpress/bedrock.env", timeout=60)
        site.wait_for_unit("frankenphp.service")
        site.wait_for_file("/run/wordpress/wp.sock")
        assert site.succeed("systemctl show -p InvocationID --value frankenphp").strip() != before
        want = hashlib.md5(b"rotated-key").hexdigest()
        site.wait_until_succeeds(
            "curl -sS --max-time 30 --unix-socket /run/wordpress/wp.sock http://localhost/wp-json/probe/v1/env"
            f" | jq -e '.s3_key_md5 == \"{want}\"'",
            timeout=60,
        )
        unit_ok("wordpress-migrate.service")

    with subtest("no permission errors in the journal"):
        journal = site.succeed(
            "journalctl -o cat --no-pager -u frankenphp -u wordpress-init -u wordpress-migrate -u wordpress-cron"
        )
        for bad in ("Permission denied", "Read-only file system", "EACCES", "EROFS", "PHP Warning", "PHP Fatal"):
            assert bad not in journal, f"{bad} in the journal:\n{journal}"

    with subtest("over TCP: the installer, on :80 without root"):
        tcp.wait_for_unit("frankenphp.service")
        tcp.wait_for_open_port(80)
        loc = tcp.succeed("curl -s -o /dev/null -D - http://localhost/ | grep -i '^location:'")
        assert "/wp/wp-admin/install.php" in loc, loc
        tcp.succeed("systemctl show -p AmbientCapabilities frankenphp | grep -qi cap_net_bind_service")
  '';
}
