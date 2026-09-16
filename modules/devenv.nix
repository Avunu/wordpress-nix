# devenv shell module for WordPress site repos: the `perSystem.wordpress-nix`
# option namespace, and from it a dev shell that runs the site the way the
# platform does -- the pinned core over the repo's wp-content, the platform PHP
# with the driver's native extensions, FrankenPHP, the WordPress SQLite
# Anywhere drop-in -- with a database that needs no server by default.
#
# The same options build the site's OCI image (`nix build .#image`), so the
# thing you develop against is the thing that ships.
{
  lib,
  flake-parts-lib,
  inputs,
  ...
}:
let
  inherit (lib) mkOption mkEnableOption types;
  inherit (flake-parts-lib) mkPerSystemOption;

  # Per-site port offset, 0..899, hashed from the site name: every clone of a
  # site on every machine gets the same port, and two different sites can run
  # side by side. Two clones of one site at once are devenv's port allocator's
  # problem, which is what it is for.
  portOffsetFor =
    siteName:
    lib.mod (lib.fromHexString (builtins.substring 0 4 (builtins.hashString "sha256" siteName))) 900;
in
{
  options.perSystem = mkPerSystemOption (
    { config, pkgs, ... }:
    {
      options.wordpress-nix = {
        enable = mkEnableOption "WordPress site devenv shell";

        siteName = mkOption {
          type = types.strMatching "^[a-z0-9][a-z0-9-]*$";
          description = "Site identifier: ports are hashed from it, and it names the image.";
          example = "anabaptistperspectives";
        };

        siteRoot = mkOption {
          type = types.path;
          description = ''
            The site repo's root (where wp-content/ lives). Used for the image
            build; the dev shell uses the live checkout at $DEVENV_ROOT.
          '';
          example = lib.literalExpression "./.";
        };

        php = mkOption {
          type = types.package;
          default = pkgs.php85;
          defaultText = lib.literalExpression "pkgs.php85";
          description = "The base PHP. The SQLite Anywhere plugin requires 8.5.";
        };

        port = mkOption {
          type = types.nullOr types.port;
          default = null;
          description = "The HTTP port; null hashes one from siteName (8100-8999).";
        };

        database = {
          type = mkOption {
            type = types.enum [
              "sqlite"
              "turso"
              "mysql"
            ];
            default = "sqlite";
            description = ''
              Where the data lives in the dev shell.

              `sqlite`: a file under .devenv/state, through the same driver the
              remote engines use -- no server, and `wp-import` fills it from a
              MySQL dump or a SQLite file.
              `turso`: a local `tursodb --sync-server` (or `database.turso.url`),
              read through the embedded replica as in production.
              `mysql`: devenv's MariaDB, for parity with a legacy host.
            '';
          };

          turso = {
            url = mkOption {
              type = types.nullOr types.str;
              default = null;
              description = ''
                A Turso database to develop against instead of a local
                `tursodb`. The token comes from WP_TURSO_TOKEN in the
                environment (a `.env` file, loaded by `devenv shell`).
              '';
              example = "turso://site-org.aws-us-east-1.turso.io";
            };
            embedded = mkOption {
              type = types.bool;
              default = true;
              description = ''
                Serve reads from an embedded replica inside FrankenPHP, as the
                platform does. Only the server process holds the replica;
                `wp` (wp-cli) always talks to the primary.
              '';
            };
          };
        };

        productionOnlyPlugins = mkOption {
          type = types.listOf types.str;
          default = [
            # Security, anti-spam, remote management: no environment switch of
            # their own; time and per-request writes for nothing here.
            "patchstack"
            "cleantalk-spam-protect"
            "wordfence"
            "sucuri-scanner"
            "mainwp-child"
            # CAPTCHA gates: a widget bound to the production domain can never
            # validate on 127.0.0.1, which locks the login form.
            "simple-cloudflare-turnstile"
            "advanced-google-recaptcha"
            "hcaptcha-for-forms-and-more"
          ];
          description = ''
            Plugin directories that stay inactive in the dev shell, without
            changing the database: security, anti-spam, remote-management
            and CAPTCHA plugins have no environment switch of their own and
            only cost time (and per-request writes) here -- or, for a
            CAPTCHA bound to the production domain, lock the login form.
            Applied by the platform's environment mu-plugin whenever
            WP_ENVIRONMENT_TYPE is not "production"; wp-admin says which
            ones are held back.
          '';
        };

        configExtra = mkOption {
          type = types.lines;
          default = "";
          description = ''
            PHP appended to the generated wp-config.php: the site's own
            constants. Reuse the string you give the NixOS module's
            `configExtra`. Secrets should come from the environment
            (`getenv()`), never from this string.
          '';
        };

        mailpit.enable = mkOption {
          type = types.bool;
          default = true;
          description = "Catch all outgoing mail in Mailpit (PHP's sendmail_path points at it).";
        };

        muPlugins = mkOption {
          type = types.listOf types.path;
          default = [ ../mu-plugins ];
          defaultText = lib.literalExpression "[ wordpress-nix/mu-plugins ]";
          description = "Directories of platform mu-plugins (platform-*.php) copied into wp-content/mu-plugins.";
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

        image = {
          enable = mkOption {
            type = types.bool;
            default = true;
            description = "Expose the site's OCI image as packages.image (and packages.default).";
          };
          name = mkOption {
            type = types.str;
            default = config.wordpress-nix.siteName;
            defaultText = lib.literalExpression "config.wordpress-nix.siteName";
            description = "The image name.";
          };
          d1 = mkOption {
            type = types.bool;
            default = true;
            description = "Bundle the SQLite Anywhere plugin and its native extensions in the image.";
          };
        };
      };
    }
  );

  config = {
    perSystem =
      { config, pkgs, ... }:
      let
        cfg = config.wordpress-nix;
        sqliteAnywhere = inputs.wordpress-sqlite-anywhere;
        system = pkgs.stdenv.hostPlatform.system;

        offset = portOffsetFor cfg.siteName;
        httpPort = if cfg.port != null then cfg.port else 8100 + offset;
        tursoPort = 9100 + offset;
        mailpitHttpPort = 10100 + offset;
        mailpitSmtpPort = 11100 + offset;

        localTurso = cfg.database.type == "turso" && cfg.database.turso.url == null;
        tursoUrl = if localTurso then "http://127.0.0.1:${toString tursoPort}" else cfg.database.turso.url;
        remoteSqlite = cfg.database.type != "mysql";

        # The platform PHP, unoptimised (a dev build should not take a clang/LTO
        # pass) and with pdo_sqlite, which the file engine needs and the
        # platform build (targeting remote engines) leaves out. Mail goes to
        # Mailpit through sendmail_path, which WordPress's PHPMailer uses.
        php = import ../lib/php.nix {
          inherit pkgs;
          php = cfg.php;
          optimize = false;
          extraExtensions = all: [
            all.pdo_sqlite
            all.sqlite3
          ];
          iniExtra = ''
            display_errors = Off
            log_errors = On
            ${lib.optionalString cfg.mailpit.enable ''
              sendmail_path = "${pkgs.mailpit}/bin/mailpit sendmail --smtp-addr 127.0.0.1:${toString mailpitSmtpPort}"
            ''}
          '';
        };
        phpExtensions = import ../lib/php-extensions.nix {
          inherit pkgs php sqliteAnywhere;
        };
        phpIniScanDir = "${php}/lib:${phpExtensions.iniDir}";
        frankenphp = import ../lib/frankenphp.nix { inherit pkgs php; };
        wpCli = pkgs.wp-cli.override { inherit php; };

        sqlitePlugin = "${
          sqliteAnywhere.packages.${system}.default
        }/share/wordpress/plugins/wordpress-sqlite-anywhere";
        dbDropIn = pkgs.runCommandLocal "wordpress-dev-db-drop-in" { } ''
          substitute ${sqlitePlugin}/db.copy $out \
            --replace-fail '{SQLITE_IMPLEMENTATION_FOLDER_PATH}' '${sqlitePlugin}' \
            --replace-fail '{SQLITE_PLUGIN}' 'wordpress-sqlite-anywhere/wordpress-sqlite-anywhere.php'
        '';

        # wp-config.php reads everything that varies from the environment, so
        # it can live in the store next to the core it configures. PHP resolves
        # __FILE__ through the docroot's symlinks into this store copy, so
        # ABSPATH is here and the config is found; WP_CONTENT_DIR points back at
        # the checkout.
        wpConfig = pkgs.writeText "wp-config.php" ''
          <?php
          // Generated by wordpress-nix's devenv module. Do not edit: change the
          // site flake's `wordpress-nix.configExtra` instead.
          define('WP_CONTENT_DIR', getenv('WP_CONTENT_DIR'));
          define('WP_CONTENT_URL', getenv('WP_DEV_URL') . '/wp-content');
          define('WP_HOME', getenv('WP_DEV_URL'));
          define('WP_SITEURL', getenv('WP_DEV_URL'));

          ${
            if cfg.database.type == "mysql" then
              ''
                define('DB_NAME', 'wordpress');
                define('DB_USER', 'wordpress');
                define('DB_PASSWORD', 'wordpress');
                define('DB_HOST', 'localhost:' . getenv('MYSQL_UNIX_PORT'));
              ''
            else
              ''
                define('DB_ENGINE', '${cfg.database.type}');
                define('DB_NAME', 'wordpress');
                define('DB_USER', "");
                define('DB_PASSWORD', "");
                define('DB_HOST', 'localhost');
              ''
          }
          define('DB_CHARSET', 'utf8mb4');
          define('DB_COLLATE', "");
          ${lib.optionalString (cfg.database.type == "sqlite") ''
            define('DB_DIR', getenv('WP_DEV_STATE') . '/database/');
            define('DB_FILE', '.ht.sqlite');
          ''}
          ${lib.optionalString (cfg.database.type == "turso") ''
            define('WP_TURSO_URL', '${tursoUrl}');
            if (getenv('WP_TURSO_TOKEN')) {
              define('WP_TURSO_TOKEN', getenv('WP_TURSO_TOKEN'));
            }
            // Only the server process sets WP_TURSO_REPLICA (one process per
            // replica); wp-cli runs primary-only.
            if (getenv('WP_TURSO_REPLICA')) {
              define('WP_TURSO_REPLICA', getenv('WP_TURSO_REPLICA'));
              define('WP_TURSO_REPLICA_PULL_MS', 1000);
            }
          ''}
          $table_prefix = 'wp_';

          require getenv('WP_DEV_STATE') . '/wp-salts.php';

          define('WP_DEBUG', true);
          define('WP_DEBUG_LOG', getenv('WP_DEV_STATE') . '/debug.log');
          define('WP_DEBUG_DISPLAY', false);
          define('SCRIPT_DEBUG', true);
          define('DISABLE_WP_CRON', true);
          define('AUTOMATIC_UPDATER_DISABLED', true);
          define('WP_AUTO_UPDATE_CORE', false);
          define('WP_ENVIRONMENT_TYPE', 'development');
          define('WP_PLATFORM_PRODUCTION_ONLY_PLUGINS', '${lib.concatStringsSep "," cfg.productionOnlyPlugins}');

          // --- site configuration (wordpress-nix.configExtra) ---
          ${cfg.configExtra}

          if (!defined('ABSPATH')) {
            define('ABSPATH', __DIR__ . '/');
          }
          require_once ABSPATH . 'wp-settings.php';
        '';

        # The pinned core with wp-config.php at its root; the docroot symlinks
        # every core entry into it (see wpConfig above).
        wordpressCore = import ../lib/wordpress-core.nix { inherit pkgs; };
        devCore = pkgs.runCommandLocal "wordpress-dev-core" { } ''
          mkdir $out
          cp -rL ${wordpressCore}/. $out/
          chmod -R u+w $out
          rm -rf $out/wp-content $out/wp-config-sample.php
          cp ${wpConfig} $out/wp-config.php
        '';

        caddyfile = pkgs.writeText "Caddyfile" ''
          {
            auto_https off
            admin off
            frankenphp {
              num_threads 4
            }
            order php_server before file_server
          }
          http://127.0.0.1:${toString httpPort} {
            root * {$WP_DOCROOT}
            encode zstd gzip
            php_server
          }
        '';

        muPluginDirs = lib.concatMapStringsSep " " toString cfg.muPlugins;

        # Assembles the docroot: core symlinks, wp-content from the checkout,
        # the drop-in and platform mu-plugins (both gitignored by the site).
        assemble = pkgs.writeShellScript "wp-dev-assemble" ''
          set -euo pipefail
          mkdir -p "$WP_DEV_STATE/database" "$WP_DEV_STATE/turso" "$WP_DOCROOT"
          for entry in ${devCore}/*; do
            ln -sfn "$entry" "$WP_DOCROOT/$(basename "$entry")"
          done
          # Stale symlinks from an older core.
          find "$WP_DOCROOT" -maxdepth 1 -xtype l -delete
          ln -sfn "$WP_CONTENT_DIR" "$WP_DOCROOT/wp-content"
          mkdir -p "$WP_CONTENT_DIR/mu-plugins" "$WP_CONTENT_DIR/uploads"
          ${lib.optionalString remoteSqlite ''
            install -m 0644 ${dbDropIn} "$WP_CONTENT_DIR/db.php"
          ''}
          ${lib.optionalString (!remoteSqlite) ''
            rm -f "$WP_CONTENT_DIR/db.php"
          ''}
          rm -f "$WP_CONTENT_DIR/mu-plugins"/platform-*.php
          for dir in ${muPluginDirs}; do
            cp -rL --no-preserve=mode "$dir"/. "$WP_CONTENT_DIR/mu-plugins/"
          done
          if [ ! -s "$WP_DEV_STATE/wp-salts.php" ]; then
            {
              echo '<?php'
              for key in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
                printf "define('%s', '%s');\n" "$key" "$(${pkgs.openssl}/bin/openssl rand -base64 48 | tr -d '/+=\n' | head -c 64)"
              done
            } > "$WP_DEV_STATE/wp-salts.php"
          fi
        '';

        migrationTools = [
          (import ../lib/mysql-to-sqlite.nix {
            inherit pkgs php;
            driverSrc = "${sqliteAnywhere.packages.${system}.driver}/src";
            parserExtension = phpExtensions.wp-mysql-parser;
          })
          (import ../lib/sqlite-to-turso.nix { inherit pkgs; })
          (import ../lib/restore-core-keys.nix { inherit pkgs php; })
        ];

        scripts = {
          wp = {
            description = "wp-cli against the dev site (primary-only on Turso)";
            exec = ''exec ${wpCli}/bin/wp --path="$WP_DOCROOT" --url="$WP_DEV_URL" "$@"'';
          };
          wp-import = {
            description = "Load a MySQL dump (.sql) or a SQLite file into the dev database";
            exec = ''
              set -euo pipefail
              src=''${1:?usage: wp-import <dump.sql | site.sqlite>}
              case "$src" in
                *.sql|*.sql.gz)
                  work=$(mktemp -d)
                  trap 'rm -rf "$work"' EXIT
                  dump="$src"
                  if [[ "$src" == *.gz ]]; then gunzip -c "$src" > "$work/dump.sql"; dump="$work/dump.sql"; fi
                  restore-core-keys "$dump" "$work/fixed.sql"
                  mysql-to-sqlite "$work/fixed.sql" "$work/site.sqlite" --continue-on-error
                  sqlite="$work/site.sqlite"
                  ;;
                *)
                  sqlite="$src"
                  ;;
              esac
              ${
                if cfg.database.type == "sqlite" then
                  ''
                    install -m 0644 "$sqlite" "$WP_DEV_STATE/database/.ht.sqlite"
                    echo "wp-import: installed $WP_DEV_STATE/database/.ht.sqlite"
                  ''
                else if cfg.database.type == "turso" then
                  ''
                    TURSO_AUTH_TOKEN="''${WP_TURSO_TOKEN:-}" sqlite-to-turso "$sqlite" "${tursoUrl}" --replace
                    rm -f "$WP_DEV_STATE/turso/replica.db"*
                    echo "wp-import: loaded into ${tursoUrl}; restart 'devenv up' so the replica re-bootstraps"
                  ''
                else
                  ''
                    echo "wp-import: database.type = mysql -- restore the dump with the mysql client instead:" >&2
                    echo "  mysql -S \$MYSQL_UNIX_PORT wordpress < dump.sql" >&2
                    exit 1
                  ''
              }
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
              echo "administrator: $user / $pass  ->  $WP_DEV_URL/wp-login.php"
            '';
          };
          wp-reset = {
            description = "Delete the dev database and replica (keeps wp-content)";
            exec = ''
              rm -rf "$WP_DEV_STATE/database" "$WP_DEV_STATE/turso"
              mkdir -p "$WP_DEV_STATE/database" "$WP_DEV_STATE/turso"
              echo "wp-reset: database state removed; run wp-import, or open $WP_DEV_URL to install"
            '';
          };
        };
      in
      lib.mkIf cfg.enable {
        packages = lib.optionalAttrs cfg.image.enable rec {
          # The site's container image: the pinned core + this repo's wp-content.
          image = import ../modules/containers.nix {
            inherit pkgs;
            php = cfg.php;
            imageName = cfg.image.name;
            wpContent = cfg.siteRoot + "/wp-content";
            sqliteAnywhere = if cfg.image.d1 then sqliteAnywhere else null;
            rustPkgs = inputs.nixpkgs.legacyPackages.${system};
          };
          # The Worker-Assets static tree (same pin + this wp-content) and the
          # platform edge Worker bundle.
          static-assets = import ../lib/static-assets.nix {
            inherit pkgs;
            wpContent = cfg.siteRoot + "/wp-content";
          };
          worker = import ../lib/worker.nix {
            inherit pkgs;
            d1ProxyWorkerSrc = sqliteAnywhere.lib.srcs.d1ProxyWorker;
          };
          default = image;
        };

        devenv.shells.default =
          { config, lib, ... }:
          {
            # devenv's dotenv integration asserts against the flake integration
            # (`nix develop`); keep .env loading for `devenv shell` only.
            dotenv.enable = lib.mkDefault (!config.devenv.flakesIntegration);

            packages =
              [
                php
                wpCli
                frankenphp
                pkgs.sqlite
                pkgs.gum
                pkgs.jq
                pkgs.curl
                pkgs.git
              ]
              ++ migrationTools
              ++ lib.optional (cfg.database.type == "turso") pkgs.turso
              ++ lib.optional cfg.mailpit.enable pkgs.mailpit
              ++ lib.optional (cfg.database.type == "mysql") pkgs.mariadb.client
              ++ cfg.extraDevPackages;

            env = {
              WP_DEV_STATE = config.devenv.state + "/wordpress";
              WP_DOCROOT = config.devenv.state + "/wordpress/www";
              WP_CONTENT_DIR = config.devenv.root + "/wp-content";
              WP_DEV_URL = "http://127.0.0.1:${toString httpPort}";
              PHP_INI_SCAN_DIR = phpIniScanDir;
            };

            services.mysql = lib.mkIf (cfg.database.type == "mysql") {
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
                  ${lib.optionalString (cfg.database.type == "turso" && cfg.database.turso.embedded) ''
                    export WP_TURSO_REPLICA="$WP_DEV_STATE/turso/replica.db"
                  ''}
                  cd "$WP_DEV_STATE"
                  exec ${frankenphp}/bin/frankenphp run --config ${caddyfile} --adapter caddyfile
                '';
                after =
                  lib.optional localTurso "devenv:processes:tursodb@started"
                  ++ lib.optional (cfg.database.type == "mysql") "devenv:processes:mysql@started";
              };
            }
            // lib.optionalAttrs localTurso {
              tursodb.exec = ''
                mkdir -p "$WP_DEV_STATE/turso"
                exec ${pkgs.turso}/bin/tursodb "$WP_DEV_STATE/turso/primary.db" --sync-server 127.0.0.1:${toString tursoPort}
              '';
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
              echo "  ${cfg.siteName} — WordPress ${wordpressCore.version} on ${cfg.database.type}  (wordpress-nix)"
              echo "  devenv up        start ${
                lib.concatStringsSep " + " (
                  [ "frankenphp" ]
                  ++ lib.optional localTurso "tursodb"
                  ++ lib.optional (cfg.database.type == "mysql") "mariadb"
                  ++ lib.optional cfg.mailpit.enable "mailpit"
                )
              }  →  $WP_DEV_URL"
              echo "  wp-import        load a MySQL dump or SQLite file"
              echo "  wp-admin-user    create a local administrator"
              echo "  wp               wp-cli against the dev site"
              ${lib.optionalString cfg.mailpit.enable ''
                echo "  mail             all outgoing mail → http://127.0.0.1:${toString mailpitHttpPort}"
              ''}
              echo ""
            '';

            scripts = scripts // cfg.extraScripts;
          };
      };
  };
}
