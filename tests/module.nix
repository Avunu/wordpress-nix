# NixOS VM test for services.wordpress-nix.
#
# Run: nix build .#checks.<system>.module   (needs KVM)
#
# Test VMs have no internet, so we exercise:
#   * git mode    — source.path = pkgs.wordpress (a read-only store document root)
#   * state mode  — core seeded offline (production would `wp core download`)
#   * socket mode — served over a unix socket with no TCP listener at all
# All use a local MariaDB over unix_socket (passwordless, OS-user matched).
{
  pkgs,
  wordpressModule,
}:
let
  # nixpkgs packages themes separately from core, so state mode has to seed one.
  themeName = "twentytwentyfive";
  theme = pkgs.wordpressPackages.themes.${themeName};
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
        # managed wp-config is in place
        machine.succeed("test -f /var/lib/wordpress/www/wp-config.php")
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

    # git mode: core files are symlinks into the read-only store
    git.succeed("readlink /var/lib/wordpress/www/index.php | grep -q /nix/store")

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
  '';
}
