# NixOS VM test for services.wordpress-nix.
#
# Run: nix build .#checks.<system>.module   (needs KVM)
#
# Test VMs have no internet, so we exercise:
#   * git mode    — source.path = pkgs.wordpress (a read-only store document root)
#   * state mode  — core seeded offline (production would `wp core download`)
#   * socket mode — served over a unix socket with no TCP listener at all
#   * split plane — wpadmin (writable, managed, owns MariaDB) + wppublic
#                   (read-only, git mode) over ONE shared database via TCP
# The first three use a local MariaDB over unix_socket (passwordless, OS-user
# matched); the split plane exercises the external-DB-over-TCP path instead.
{
  pkgs,
  wordpressModule,
}:
let
  # nixpkgs packages themes separately from core, so state mode has to seed one.
  themeName = "twentytwentyfive";
  theme = pkgs.wordpressPackages.themes.${themeName};

  # What the public plane serves: an immutable store docroot composed from core,
  # the site's payload and the platform mu-plugins. Built from nixpkgs' core so
  # the test does not depend on the pinned download, and with a theme grafted on
  # because a themeless install renders a 200 with an empty body.
  splitPlaneDocroot = import ../lib/mk-site-docroot.nix {
    inherit pkgs;
    name = "split-plane-docroot";
    core = "${pkgs.wordpress}/share/wordpress";
    themes.${themeName} = theme;
  };
in
pkgs.testers.runNixOSTest {
  name = "wordpress-nix";

  nodes = {
    # ---- git (source-managed) ----
    git =
      { ... }:
      {
        imports = [ wordpressModule ];
        virtualisation.memorySize = 2048;
        services.wordpress-nix = {
          enable = true;
          php = pkgs.php83;
          phpOptimize = false; # skip the slow clang/LTO build in CI
          source = {
            type = "git";
            # pkgs.wordpress installs to $out/share/wordpress, so the document
            # root is that subdirectory — $out itself contains only `share`.
            path = "${pkgs.wordpress}/share/wordpress";
          };
          database.createLocally = true;
        };
      };

    # ---- state (flexible) ----
    state =
      { config, ... }:
      {
        imports = [ wordpressModule ];
        virtualisation.memorySize = 2048;
        services.wordpress-nix = {
          enable = true;
          php = pkgs.php83;
          phpOptimize = false;
          source.type = "state";
          database.createLocally = true;
        };
        environment.systemPackages = [ pkgs.jq ];
        # Seed core offline (stand-in for `wp core download`), plus a theme.
        # pkgs.wordpress ships wp-content/themes containing only index.php —
        # nixpkgs packages themes separately — and an installed site with no
        # theme renders a 200 with a completely empty body, which is what the
        # end-to-end assertion below would otherwise trip over.
        systemd.services.seed-wordpress = {
          before = [ "wordpress-init.service" ];
          requiredBy = [ "wordpress-init.service" ];
          serviceConfig.Type = "oneshot";
          script = ''
            dst=/var/lib/wordpress/www
            if [ ! -e "$dst/wp-includes/version.php" ]; then
              mkdir -p "$dst"
              cp -r ${pkgs.wordpress}/share/wordpress/. "$dst/"
              chmod -R u+w "$dst"
            fi
            dest="$dst/wp-content/themes/${themeName}"
            if [ ! -e "$dest/style.css" ]; then
              mkdir -p "$dest"
              cp -r ${theme}/. "$dest/"
              chmod -R u+w "$dest"
            fi
            chown -R wordpress:wordpress "$dst"
          '';
        };
      };

    # ---- socket (unix socket origin, nothing on the network) ----
    socket =
      { ... }:
      {
        imports = [ wordpressModule ];
        virtualisation.memorySize = 2048;
        services.wordpress-nix = {
          enable = true;
          php = pkgs.php83;
          phpOptimize = false;
          source = {
            type = "git";
            # pkgs.wordpress installs to $out/share/wordpress, so the document
            # root is that subdirectory — $out itself contains only `share`.
            path = "${pkgs.wordpress}/share/wordpress";
          };
          database.createLocally = true;
          socketPath = "/run/wordpress/wp.sock";
        };
      };

    # ---- split plane: the writable admin face, and the database owner ----
    wpadmin =
      { ... }:
      {
        imports = [ wordpressModule ];
        virtualisation.memorySize = 2048;
        networking.firewall.allowedTCPPorts = [ 3306 ];
        services.wordpress-nix = {
          enable = true;
          php = pkgs.php83;
          phpOptimize = false;
          # Managed mode is the real admin-plane configuration: pinned core
          # symlinked out of the store, wp-content mutable, gitium installed.
          source.type = "managed";
          plane = {
            role = "admin";
            publicUrl = "http://wppublic";
            adminUrl = "http://wpadmin";
          };
          database = {
            createLocally = true;
            name = "wordpress";
            user = "wordpress";
          };
        };
        # The shared database the public plane reads over the network.
        services.mysql.settings.mysqld.bind-address = "0.0.0.0";
        systemd.services.mysql-grant-public = {
          after = [ "mysql.service" ];
          requires = [ "mysql.service" ];
          wantedBy = [ "multi-user.target" ];
          before = [ "wordpress-init.service" ];
          serviceConfig.Type = "oneshot";
          script = ''
            ${pkgs.mariadb}/bin/mysql -u root <<'SQL'
            CREATE USER IF NOT EXISTS 'wppublic'@'%' IDENTIFIED BY 'publicpw';
            GRANT ALL PRIVILEGES ON wordpress.* TO 'wppublic'@'%';
            FLUSH PRIVILEGES;
            SQL
          '';
        };
      };

    # ---- split plane: the read-only public face ----
    wppublic =
      { ... }:
      {
        imports = [ wordpressModule ];
        virtualisation.memorySize = 2048;
        environment.etc."wp-db-password".text = "publicpw";
        services.wordpress-nix = {
          enable = true;
          php = pkgs.php83;
          phpOptimize = false;
          source = {
            type = "git";
            path = splitPlaneDocroot;
          };
          plane = {
            role = "public";
            publicUrl = "http://wppublic";
            adminUrl = "http://wpadmin";
            # Visitor logins stay open, which is what a WooCommerce or
            # membership site needs — and what makes the login gate testable.
            allowLogin = true;
          };
          database = {
            createLocally = false;
            host = "wpadmin";
            name = "wordpress";
            user = "wppublic";
            passwordFile = "/etc/wp-db-password";
          };
        };
      };
  };

  testScript = ''
    start_all()

    for machine in (git, state):
        machine.wait_for_unit("wordpress-init.service")
        machine.wait_for_unit("mysql.service")
        machine.wait_for_unit("wordpress.service")
        machine.wait_for_open_port(80)
        # secrets file is present and locked down
        machine.succeed("test -f /var/lib/wordpress/wp-secrets.php")
        machine.succeed("stat -c '%a' /var/lib/wordpress/wp-secrets.php | grep -x 600")
        # the generated wp-config is in place -- but WHERE depends on the source
        # mode, so that is asserted per mode below
        # uploads is a real writable directory
        machine.succeed("test -d /var/lib/wordpress/www/wp-content/uploads")
        machine.succeed("sudo -u wordpress test -w /var/lib/wordpress/www/wp-content/uploads")
        # cron timer is armed
        machine.succeed("systemctl is-active wordpress-cron.timer")
        # core is served: an uninstalled site redirects to the installer
        # Fetch to a file rather than piping: grep -q exits on the first match,
        # which SIGPIPEs curl, and pipefail then surfaces curl's exit 23. That
        # only bites once the body is large enough to still be streaming.
        machine.succeed("curl -sSL http://localhost/ -o /tmp/home.html")
        machine.succeed("grep -qi wordpress /tmp/home.html")

    # state mode: a real docroot, so ABSPATH is the docroot and the config is here
    state.succeed("test -f /var/lib/wordpress/www/wp-config.php")

    # git mode: core files are symlinks into the read-only store
    git.succeed("readlink /var/lib/wordpress/www/index.php | grep -q /nix/store")

    # Regression: PHP resolves those symlinks, so ABSPATH is the store tree and
    # the generated wp-config.php has to live there too. While it was written
    # into the docroot instead, WordPress found no config at all and redirected
    # every request to setup-config.php -- git mode could only ever serve the
    # installer, never a configured site. A configured site whose database is
    # reachable but empty asks for install.php instead.
    loc = git.succeed("curl -s -o /dev/null -D - http://localhost/ | grep -i '^location:'")
    assert "install.php" in loc, f"git mode did not reach a configured site: {loc}"
    assert "setup-config.php" not in loc, f"git mode cannot find wp-config.php: {loc}"
    git.succeed(
        "test -f \"$(dirname \"$(readlink -f /var/lib/wordpress/www/wp-load.php)\")/wp-config.php\""
    )

    # end-to-end: install over the socket-auth DB, then confirm the title renders
    state.succeed(
        "su -s /bin/sh wordpress -c '"
        "wp core install --url=http://localhost --title=StateSite "
        "--admin_user=admin --admin_password=admin_pw_123 "
        "--admin_email=admin@example.com --skip-email'"
    )
    # A themeless install returns 200 with an empty body, so assert the body is
    # actually rendered, not just that the request succeeded.
    state.succeed("curl -sS http://localhost/ -o /tmp/installed.html")
    state.succeed("grep -q StateSite /tmp/installed.html")
    state.succeed("grep -q '</html>' /tmp/installed.html")

    # ---- logging: journald fields, JSON access log, PHP errors via Caddy ----
    def journal_has(unit, jq_filter):
        state.wait_until_succeeds(f"journalctl -u {unit} -o json | jq -e -s '{jq_filter}' >/dev/null")

    for unit, role in [("wordpress", "web"), ("wordpress-init", "init"), ("mysql", "db")]:
        journal_has(unit, f'any(.[]; .APP_SERVICE == "{role}" and .APP_SITE == "wordpress")')
    journal_has("wordpress", 'any(.[]; .SYSLOG_IDENTIFIER == "wordpress")')
    # every request is one JSON line from Caddy's access logger
    journal_has(
        "wordpress",
        'any(.[]; .MESSAGE | fromjson? | .logger // "" | startswith("http.log.access"))',
    )
    # error_log is unset under systemd, so PHP errors take the SAPI logger:
    # FrankenPHP writes them through Caddy's JSON logger, with a level
    state.succeed(
        "install -o wordpress -g wordpress -m 0644 /dev/stdin /var/lib/wordpress/www/logtest.php"
        " <<< '<?php error_log(\"wordpress-nix-logtest\"); var_dump(ini_get(\"error_log\"));'"
    )
    state.succeed("curl -sS http://localhost/logtest.php -o /tmp/logtest.txt")
    state.succeed("grep -qF 'string(0) \"\"' /tmp/logtest.txt")
    journal_has(
        "wordpress",
        'any(.[]; .APP_SITE == "wordpress" and (.MESSAGE | fromjson? | .logger == "frankenphp" and .msg == "wordpress-nix-logtest" and .level != null))',
    )

    # ---- socket mode ----
    socket.wait_for_unit("wordpress-init.service")
    socket.wait_for_unit("wordpress.service")
    socket.wait_for_file("/run/wordpress/wp.sock")
    # group-openable, so a co-located connector can reach it (Caddy's own
    # default would be 0200 and unopenable)
    socket.succeed("stat -c '%a' /run/wordpress/wp.sock | grep -x 660")
    # the site serves over the socket...
    socket.succeed(
        "curl -sSL --unix-socket /run/wordpress/wp.sock http://localhost/ -o /tmp/sock.html"
    )
    socket.succeed("grep -qi wordpress /tmp/sock.html")
    # ...and nothing at all listens on the network
    socket.fail("curl -sS --max-time 5 http://localhost/")
    socket.fail("ss -HltnO | grep -qE ':(80|443)\\s'")
    # no port-binding capability is retained in socket mode
    socket.fail(
        "systemctl show -p AmbientCapabilities wordpress.service"
        " | grep -qi cap_net_bind_service"
    )
    # a stale socket left by a crash does not block the rebind
    socket.succeed("systemctl stop wordpress.service")
    socket.succeed("touch /run/wordpress/wp.sock")
    socket.succeed("systemctl start wordpress.service")
    socket.wait_for_file("/run/wordpress/wp.sock")
    socket.succeed(
        "curl -sSL --unix-socket /run/wordpress/wp.sock http://localhost/ -o /tmp/sock.html"
    )

    # ---- split plane: one database, two faces ----
    for machine in (wpadmin, wppublic):
        machine.wait_for_unit("wordpress-init.service")
        machine.wait_for_unit("wordpress.service")
        machine.wait_for_open_port(80)
    wpadmin.wait_for_unit("mysql.service")

    def status(machine, url):
        return machine.succeed(f"curl -s -o /dev/null -w '%{{http_code}}' {url}").strip()

    # Only the admin plane ever writes schema.
    wpadmin.succeed(
        "su -s /bin/sh wordpress -c '"
        "wp core install --url=http://wpadmin --title=SplitSite "
        "--admin_user=admin --admin_password=admin_pw_123 "
        "--admin_email=admin@example.com --skip-email'"
    )
    # In production both planes serve the same wp-content, composed from one
    # site repo. Mirror that here: give the admin plane the theme the public
    # plane's store docroot carries, and activate it. Without this the planes
    # disagree about the active theme and the public face renders an empty body.
    wpadmin.succeed(
        "cp -rL ${theme}/. /var/lib/wordpress/www/wp-content/themes/${themeName}/"
        " ; chown -R wordpress:wordpress /var/lib/wordpress/www/wp-content/themes"
    )
    wpadmin.succeed("su -s /bin/sh wordpress -c 'wp theme activate ${themeName}'")

    # Pretty permalinks, as every real site has. WordPress only registers the
    # /wp-json/ rewrite when a permalink structure is set, so without this the
    # REST API exists only at /?rest_route=... and the public plane's "the REST
    # API stays open" assertion would be testing a 404. Caddy's php_server is the
    # front controller, so no .htaccess is involved.
    wpadmin.succeed(
        "su -s /bin/sh wordpress -c 'wp rewrite structure /%postname%/ && wp rewrite flush'"
    )

    wpadmin.succeed(
        "su -s /bin/sh wordpress -c '"
        "wp user create sub sub@example.com --role=subscriber --user_pass=sub_pw_123'"
    )
    post_id = wpadmin.succeed(
        "su -s /bin/sh wordpress -c '"
        "wp post create --post_title=SharedPost --post_status=publish --porcelain'"
    ).strip()

    # The point of the whole design: the public plane reads the admin plane's
    # database over TCP, so content is shared with no sync step at all.
    wppublic.succeed("curl -sS http://wppublic/ -o /tmp/home.html")
    # A themeless install answers 200 with an empty body, so assert the page was
    # actually rendered before asserting what is in it.
    wppublic.succeed("grep -q '</html>' /tmp/home.html")
    wppublic.succeed("grep -q SplitSite /tmp/home.html")
    wppublic.succeed(f"curl -sSL 'http://wppublic/?p={post_id}' -o /tmp/post.html")
    wppublic.succeed("grep -q SharedPost /tmp/post.html")

    # ---- the public plane turns administration away ----
    assert status(wppublic, "http://wppublic/wp-admin/") == "302"
    loc = wppublic.succeed("curl -s -o /dev/null -D - http://wppublic/wp-admin/ | grep -i '^location:'")
    assert "http://wpadmin/wp-admin/" in loc, f"wp-admin redirect went to {loc}"

    # admin-ajax must still reach PHP: WooCommerce, Give and every form plugin
    # POST to it from the public side. With no action, WordPress answers 400 "0".
    assert status(wppublic, "http://wppublic/wp-admin/admin-ajax.php") == "400"
    wppublic.succeed("curl -sS http://wppublic/wp-admin/admin-ajax.php -o /tmp/ajax.txt")
    wppublic.succeed("grep -qx 0 /tmp/ajax.txt")

    assert status(wppublic, "http://wppublic/xmlrpc.php") == "403"
    assert status(wppublic, "http://wppublic/wp-cron.php") == "404"
    assert status(wppublic, "http://wppublic/wp-includes/version.php") == "404"

    # /wp-json stays open — capability checks, not a path list, deny admin there
    assert status(wppublic, "http://wppublic/wp-json/wp/v2/posts") == "200"
    # ...but anonymous user enumeration is still refused
    assert status(wppublic, "http://wppublic/wp-json/wp/v2/users") == "401"

    # ---- the session rule ----
    def login(machine, host, user, password):
        machine.succeed(
            "curl -s -o /tmp/login.html -D /tmp/login.hdr"
            " -b 'wordpress_test_cookie=WP+Cookie+check'"
            f" --data-urlencode 'log={user}' --data-urlencode 'pwd={password}'"
            " --data 'wp-submit=Log+In'"
            f" http://{host}/wp-login.php"
        )

    # An administrator cannot obtain a session on the public plane...
    login(wppublic, "wppublic", "admin", "admin_pw_123")
    wppublic.fail("grep -qi 'set-cookie: wordpress_logged_in' /tmp/login.hdr")
    wppublic.succeed("grep -qi 'administration host' /tmp/login.html")

    # ...while a subscriber can, because that is who this plane serves.
    login(wppublic, "wppublic", "sub", "sub_pw_123")
    wppublic.succeed("grep -qi 'set-cookie: wordpress_logged_in' /tmp/login.hdr")

    # The same administrator logs in normally on the admin plane.
    login(wpadmin, "wpadmin", "admin", "admin_pw_123")
    wpadmin.succeed("grep -qi 'set-cookie: wordpress_logged_in' /tmp/login.hdr")

    # Cookie names are derived from siteurl, which differs per plane, so an
    # admin cookie is not even read on the public host.
    def cookiehash(machine):
        return machine.succeed(
            "sudo -u wordpress wp eval 'echo COOKIEHASH;'"
        ).strip()

    assert cookiehash(wpadmin) != cookiehash(wppublic), "COOKIEHASH must differ between planes"

    # ---- the admin plane keeps everything the public plane gave up ----
    wpadmin.succeed("systemctl is-active wordpress-cron.timer")
    wppublic.fail("systemctl is-active wordpress-cron.timer")
    # Regression: managed mode keeps wp-config.php in the store core, not the
    # docroot, so wp-cli has to be pointed at the core. Pointing it at the
    # docroot made every `wp` call fail with "'wp-config.php' not found" —
    # silently disabling cron on the one plane that owns it.
    wpadmin.succeed("systemctl start wordpress-cron.service")
    wpadmin.succeed("systemctl show -p Result wordpress-cron.service | grep -x Result=success")
    assert status(wpadmin, "http://wpadmin/xmlrpc.php") != "403"
    # The constant is asserted through WordPress rather than by reading a file:
    # in git mode wp-config.php lives inside the store tree, not the docroot.
    wppublic.succeed(
        "sudo -u wordpress wp eval 'var_export(DISALLOW_FILE_MODS);'"
        " | grep -qx true"
    )
    # Uploads must be writable and must NOT resolve into the read-only store.
    wppublic.succeed(
        "sudo -u wordpress wp eval 'echo wp_upload_dir()[\"basedir\"];'"
        " | grep -qx /var/lib/wordpress/www/wp-content/uploads"
    )
    wppublic.succeed("sudo -u wordpress test -w /var/lib/wordpress/www/wp-content/uploads")

    # gitium: installed by the platform, activated by a filter, so the site repo
    # neither carries it nor records it — and it never loads on the public plane,
    # whose working tree is a read-only store path.
    wpadmin.succeed("test -x /var/lib/wordpress/www/wp-content/plugins/gitium/inc/ssh-git")
    wpadmin.succeed(
        "su -s /bin/sh wordpress -c 'wp plugin list --status=active --field=name'"
        " | grep -qx gitium"
    )
    wppublic.fail(
        "su -s /bin/sh wordpress -c 'wp plugin list --status=active --field=name'"
        " | grep -qx gitium"
    )
  '';
}
