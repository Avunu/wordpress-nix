# devenv shell module for a Bedrock site repository: `nix develop` (or
# `devenv shell`) in a site from templates/bedrock-site gives the site the way
# production runs it -- the platform's PHP and FrankenPHP serving web/, MariaDB,
# Mailpit -- over the live checkout, where Bedrock's development environment
# allows file modifications (config/environments/development.php).
#
#   composer install          vendor/, web/wp and the Composer plugins, in the tree
#   devenv up                 frankenphp + mariadb + mailpit  ->  $WP_HOME
#   wp restore [--at T]       the newest (or chosen) dev-restore dump of the site
#   relock                    nix run .#relock after a composer.lock change
#
# Bedrock reads its configuration from the environment first and .env second
# (Dotenv, immutable): the shell sets WP_ENV, WP_HOME, the DB_* names and the
# plugin lists, and .env (gitignored, written once with fresh salts) holds the
# salts and whatever secrets a developer adds (S3_KEY, ...). The platform
# mu-plugins are copied into web/app/mu-plugins (gitignored) as the package
# injects them.
{
  lib,
  flake-parts-lib,
  config,
  inputs,
  ...
}:
let
  inherit (lib) mkOption types;
  inherit (flake-parts-lib) mkPerSystemOption;

  site = config.wordpress-nix.site;

  # Per-site port offset, 0..899, hashed from the site name: every clone of a
  # site on every machine gets the same port, and two sites can run side by side.
  portOffsetFor =
    siteName:
    lib.mod (lib.fromHexString (builtins.substring 0 4 (builtins.hashString "sha256" siteName))) 900;

  platformMuPlugins = [
    ../mu-plugins/platform-environment.php
    ../mu-plugins/platform-nonce.php
    ../mu-plugins/platform-user-enumeration.php
  ];
in
{
  options.perSystem = mkPerSystemOption (
    { pkgs, ... }:
    {
      options.wordpress-nix = {
        enable = mkOption {
          type = types.bool;
          default = site != null;
          defaultText = lib.literalExpression "wordpress-nix.site != null";
          description = "The site's dev shell (devShells.default).";
        };

        siteName = mkOption {
          type = types.strMatching "^[a-z0-9][a-z0-9-]*$";
          default = if site != null then site.name else "site";
          defaultText = lib.literalExpression "wordpress-nix.site.name";
          description = "Ports are hashed from it, and `wp restore` reads its dumps (wordpress/<siteName>).";
        };

        php = mkOption {
          type = types.package;
          default = pkgs.${if site != null then site.php else "php84"};
          defaultText = lib.literalExpression "pkgs.\${wordpress-nix.site.php}";
          description = "The base PHP, as the site builds with it.";
        };

        port = mkOption {
          type = types.nullOr types.port;
          default = null;
          description = "The HTTP port; null hashes one from siteName (8100-8999).";
        };

        mailpit.enable = mkOption {
          type = types.bool;
          default = true;
          description = "Catch all outgoing mail in Mailpit (PHP's sendmail_path points at it).";
        };

        extraDevPackages = mkOption {
          type = types.listOf types.package;
          default = [ ];
          description = "Extra packages in the dev shell.";
        };

        extraScripts = mkOption {
          type = types.attrsOf types.attrs;
          default = { };
          description = "Extra devenv scripts, merged with wordpress-nix's own.";
        };
      };
    }
  );

  config.perSystem =
    { config, pkgs, ... }:
    let
      cfg = config.wordpress-nix;
      base = inputs.python-devops-nix.lib;

      offset = portOffsetFor cfg.siteName;
      httpPort = if cfg.port != null then cfg.port else 8100 + offset;
      mailpitHttpPort = 10100 + offset;
      mailpitSmtpPort = 11100 + offset;
      home = "http://127.0.0.1:${toString httpPort}";

      # The platform PHP, unoptimised (a dev build should not take a clang/LTO
      # pass). Mail goes to Mailpit through sendmail_path.
      php = import ../lib/php.nix {
        inherit pkgs;
        php = cfg.php;
        optimize = false;
        iniExtra = ''
          display_errors = Off
          log_errors = On
          error_log =
          ${lib.optionalString cfg.mailpit.enable ''
            sendmail_path = "${pkgs.mailpit}/bin/mailpit sendmail --smtp-addr 127.0.0.1:${toString mailpitSmtpPort}"
          ''}
        '';
      };
      composer = php.packages.composer;
      frankenphp = import ../lib/frankenphp.nix { inherit pkgs php; };
      wpCli = pkgs.wp-cli.override { inherit php; };

      caddyfile = pkgs.writeText "Caddyfile" ''
        {
          auto_https off
          admin off
          frankenphp {
            num_threads 4
          }
          order php_server before file_server
        }
        ${home} {
          root * {$DEVENV_ROOT}/web
          encode zstd gzip
          @legacyAdmin path /wp-admin /wp-admin/*
          redir @legacyAdmin /wp{uri} 301
          handle /app/uploads/* {
            @exec path_regexp (?i)\.(ph(p\d?|tml|ar|ps|t)|cgi|pl|py|sh)$
            respond @exec 403
            file_server
          }
          php_server
        }
      '';

      # The development-restore dumps the cluster's backups upload
      # (<prefix>/wordpress/<slug>/<stamp>.sql.gz). The layout ships with
      # python-devops-nix; a pin from before it has none.
      backupFetch =
        if base.backups ? layouts.wordpress then
          base.backups.fetch {
            inherit pkgs;
            label = "wordpress";
            envPrefix = "WORDPRESS";
            layout = base.backups.layouts.wordpress;
          }
        else
          null;

      # Seeds what the checkout needs and Composer does not give it: the
      # platform mu-plugins, and a .env with fresh salts the first time.
      assemble = pkgs.writeShellScript "wp-dev-assemble" ''
        set -euo pipefail
        cd "$DEVENV_ROOT"
        mkdir -p web/app/mu-plugins web/app/uploads "$WP_DEV_STATE"
        ${lib.concatMapStringsSep "\n" (
          p: "install -m 0644 ${p} web/app/mu-plugins/${baseNameOf (toString p)}"
        ) platformMuPlugins}
        if [ ! -e .env ]; then
          {
            echo "# Development only: the shell sets WP_ENV, WP_HOME and DB_*."
            echo "# Add secrets here (S3_KEY, S3_SECRET, ...); this file is gitignored."
            for key in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
              printf "%s='%s'\n" "$key" "$(${pkgs.openssl}/bin/openssl rand -base64 48 | tr -d '/+=\n' | head -c 64)"
            done
          } > .env
          echo "wordpress-nix: wrote .env with fresh salts"
        fi
        if [ ! -e vendor/autoload.php ] || [ ! -e web/wp/wp-includes/version.php ]; then
          echo "wordpress-nix: run \`composer install\` to install core, plugins and vendor/"
        fi
      '';

      scripts = {
        wp = {
          description = "wp-cli against the dev site (`wp restore` restores a backup)";
          exec = ''
            if [ "''${1:-}" = restore ]; then
              shift
              exec wp-restore "$@"
            fi
            exec ${wpCli}/bin/wp --path="$DEVENV_ROOT/web/wp" "$@"
          '';
        };
        wp-restore = {
          description = "Load the site's newest (or --at) dev-restore dump, then neutralize it";
          exec =
            if backupFetch == null then
              ''
                echo "wp restore: this wordpress-nix pins a python-devops-nix without lib.backups.layouts.wordpress" >&2
                exit 1
              ''
            else
              ''
                set -euo pipefail
                at=() neutralize=1
                while [ "$#" -gt 0 ]; do
                  case "$1" in
                    --at) at=(--at "$2"); shift 2 ;;
                    --no-neutralize) neutralize=0; shift ;;
                    *) echo "usage: wp restore [--at WHEN] [--no-neutralize]" >&2; exit 2 ;;
                  esac
                done
                manifest=$(${lib.getExe backupFetch} fetch "wordpress/${cfg.siteName}" "''${at[@]}")
                dump=$(printf '%s' "$manifest" | ${pkgs.jq}/bin/jq -er .database)
                echo "wp restore: loading $dump"
                wp db reset --yes
                ${pkgs.gzip}/bin/gzip -cd "$dump" | wp db import -
                if [ "$neutralize" = 1 ]; then
                  prefix=$(wp db prefix)
                  from=$(wp db query "SELECT option_value FROM ''${prefix}options WHERE option_name = 'home'" --skip-column-names)
                  if [ -n "$from" ] && [ "$from" != "$WP_HOME" ]; then
                    wp search-replace "$from" "$WP_HOME" --all-tables-with-prefix --skip-columns=guid --report-changed-only
                  fi
                  # No production mail, search engines or remote sessions here.
                  wp option update blog_public 0
                  wp db query "DELETE FROM ''${prefix}usermeta WHERE meta_key IN ('session_tokens', '_application_passwords')"
                fi
                wp cache flush
                echo "wp restore: done; wp-admin-user creates a local administrator"
              '';
        };
        wp-admin-user = {
          description = "Create (or reset the password of) a local administrator";
          exec = ''
            set -euo pipefail
            user=''${1:-dev}
            pass=$(${pkgs.openssl}/bin/openssl rand -hex 8)
            if wp user get "$user" --field=ID > /dev/null 2>&1; then
              wp user update "$user" --user_pass="$pass" --role=administrator > /dev/null
            else
              wp user create "$user" "$user@example.invalid" --role=administrator --user_pass="$pass" > /dev/null
            fi
            echo "administrator: $user / $pass  ->  $WP_HOME/wp/wp-login.php"
          '';
        };
        wp-reset = {
          description = "Empty the dev database (keeps the checkout)";
          exec = ''
            wp db reset --yes
            echo "wp-reset: database emptied; wp restore, or open $WP_HOME to install"
          '';
        };
        wp-stop = {
          description = "Stop the processes started by `devenv up -D`";
          exec = ''
            sock="$DEVENV_RUNTIME/pc.sock"
            if [ -S "$sock" ] && ${pkgs.process-compose}/bin/process-compose down -u "$sock" > /dev/null 2>&1; then
              echo "wp-stop: stopped"
            else
              rm -f "$sock"
              echo "wp-stop: nothing running (stale socket removed, if any)"
            fi
          '';
        };
        relock = {
          description = "Rewrite nix/vendor-hash after a composer.lock change";
          exec = ''exec nix run "$DEVENV_ROOT#relock" -- "$@"'';
        };
        verify-checksums = {
          description = "Compare the built site with wordpress.org's published files (network)";
          exec = ''exec nix run "$DEVENV_ROOT#verify-wp-checksums" -- "$@"'';
        };
        wpcodebox-export = {
          description = "Commit WPCodeBox's functionality plugin (production runs it as code)";
          exec = ''
            set -euo pipefail
            cd "$DEVENV_ROOT"
            dir=web/app/plugins/wpcodebox_functionality_plugin
            if [ ! -f "$dir/plugin.php" ]; then
              echo "wpcodebox-export: no $dir; turn on WPCodeBox's functionality plugin mode and save a snippet" >&2
              exit 1
            fi
            # Whitelisted in the managed block, so git sees it.
            if ! grep -qxF "!/$dir" .gitignore && ! grep -qxF "!$dir" .gitignore; then
              sed -i "/^# END wordpress-nix/i !$dir" .gitignore
            fi
            git add .gitignore "$dir"
            echo "wpcodebox-export: $dir staged; commit it, and production runs the snippets as code"
          '';
        };
      };
    in
    lib.mkIf cfg.enable {
      devenv.shells.default =
        { config, lib, ... }:
        {
          # No OCI images of the dev shell: production is the NixOS module, and
          # devenv's container outputs need nix2container and mk-shell-bin as
          # flake inputs, failing `nix flake check` without them.
          containers = lib.mkForce { };

          packages = [
            php
            composer
            wpCli
            frankenphp
            pkgs.mariadb.client
            pkgs.jq
            pkgs.curl
            pkgs.git
          ]
          ++ lib.optional cfg.mailpit.enable pkgs.mailpit
          ++ cfg.extraDevPackages;

          env = {
            WP_DEV_STATE = config.devenv.state + "/wordpress";
            PHP_INI_SCAN_DIR = "${php}/lib";
            WP_ENV = "development";
            WP_HOME = home;
            WP_SITEURL = "${home}/wp";
            DB_NAME = "wordpress";
            DB_USER = "wordpress";
            DB_PASSWORD = "wordpress";
            DB_HOST = "localhost:${config.env.MYSQL_UNIX_PORT}";
            WORDPRESS_NIX_PRODUCTION_ONLY_PLUGINS = lib.concatStringsSep "," (
              if site != null then site.productionOnlyPlugins else [ ]
            );
            WORDPRESS_NIX_DEVELOPMENT_ONLY_PLUGINS = lib.concatStringsSep "," (
              if site != null then site.developmentOnlyPlugins else [ ]
            );
          }
          // lib.optionalAttrs cfg.mailpit.enable {
            WORDPRESS_NIX_MAIL_CATCHER = "true";
          };

          services.mysql = {
            enable = true;
            package = pkgs.mariadb;
            initialDatabases = [ { name = "wordpress"; } ];
            ensureUsers = [
              {
                name = "wordpress";
                password = "wordpress";
                ensurePermissions = {
                  "wordpress.*" = "ALL PRIVILEGES";
                };
              }
            ];
          };

          processes = {
            frankenphp = {
              exec = ''
                ${assemble}
                cd "$WP_DEV_STATE"
                exec ${frankenphp}/bin/frankenphp run --config ${caddyfile} --adapter caddyfile
              '';
              after = [ "devenv:processes:mysql@started" ];
            };
          }
          // lib.optionalAttrs cfg.mailpit.enable {
            mailpit.exec = ''
              exec ${pkgs.mailpit}/bin/mailpit \
                --smtp 127.0.0.1:${toString mailpitSmtpPort} \
                --listen 127.0.0.1:${toString mailpitHttpPort} \
                --database "$DEVENV_STATE/mailpit.db"
            '';
          };

          enterShell = ''
            ${assemble}
            echo ""
            echo "  ${cfg.siteName}: Bedrock on wordpress-nix (WP_ENV=development)"
            echo "  composer install   core, plugins and vendor/ into the tree"
            echo "  devenv up          frankenphp + mariadb${lib.optionalString cfg.mailpit.enable " + mailpit"}  ->  ${home}"
            echo "  wp restore         the newest dev-restore dump (--at, --no-neutralize)"
            echo "  wp-admin-user      a local administrator"
            echo "  relock             nix/vendor-hash after a composer.lock change"
            ${lib.optionalString cfg.mailpit.enable ''
              echo "  mail               all outgoing mail -> http://127.0.0.1:${toString mailpitHttpPort}"
            ''}
            echo ""
          '';

          scripts = scripts // cfg.extraScripts;
        };
    };
}
