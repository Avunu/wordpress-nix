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
        # Seed core offline (stand-in for `wp core download`).
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
              chown -R wordpress:wordpress "$dst"
            fi
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
        machine.succeed("curl -sSL http://localhost/ | grep -qi 'wordpress'")

    # git mode: core files are symlinks into the read-only store
    git.succeed("readlink /var/lib/wordpress/www/index.php | grep -q /nix/store")

    # end-to-end: install over the socket-auth DB, then confirm the title renders
    state.succeed(
        "su -s /bin/sh wordpress -c '"
        "wp core install --url=http://localhost --title=StateSite "
        "--admin_user=admin --admin_password=admin_pw_123 "
        "--admin_email=admin@example.com --skip-email'"
    )
    state.succeed("curl -sS http://localhost/ | grep -q StateSite")

    # ---- socket mode ----
    socket.wait_for_unit("wordpress-init.service")
    socket.wait_for_unit("wordpress.service")
    socket.wait_for_file("/run/wordpress/wp.sock")
    # group-openable, so a co-located connector can reach it (Caddy's own
    # default would be 0200 and unopenable)
    socket.succeed("stat -c '%a' /run/wordpress/wp.sock | grep -x 660")
    # the site serves over the socket...
    socket.succeed(
        "curl -sS --unix-socket /run/wordpress/wp.sock http://localhost/ | grep -qi 'wordpress'"
    )
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
        "curl -sS --unix-socket /run/wordpress/wp.sock http://localhost/ | grep -qi 'wordpress'"
    )
  '';
}
