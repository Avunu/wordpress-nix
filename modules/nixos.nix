# NixOS module: deploy a single WordPress instance served by FrankenPHP
# (standalone — Caddy terminates TLS itself via ACME; no nginx in front).
#
# Set `socketPath` instead of `domain` to serve over a unix socket with no TCP
# listener at all, for a co-located reverse proxy or tunnel connector that
# terminates TLS elsewhere.
#
# Three source modes:
#   * state   — WordPress core lives in a writable ${stateDir}/www, downloaded on
#               first boot with wp-cli; fully mutable (admin manages core / plugins
#               / themes via the UI). Best for flexible, server-specific instances.
#   * git     — source.path (a flake input / store path) IS the read-only document
#               root; ${stateDir}/www is a writable symlink farm into it with the
#               uploads/cache/upgrade dirs kept real. Sets DISALLOW_FILE_MODS.
#   * managed — the split-plane admin backend: core is symlinked from a PINNED
#               store docroot (lib/wordpress-core.nix — never self-updates), while
#               wp-content is fully mutable and seeded from the site's git repo
#               (source.siteRepo), so wp-admin file mods work and gitium can
#               version them. Pair with database.type = "d1" to share the public
#               frontend's database.
#
# Two database modes:
#   * mysql — local MariaDB (createLocally) or external, as before.
#   * d1    — no local database: the SQLite-driver D1 backend connects to the
#             site Worker's authenticated /__d1 proxy (database.d1.*). Requires
#             consuming this module via the flake's nixosModules (which injects
#             the driver source).
#
# The PHP version is the `php` option; the module wraps it with the same
# optimized ZTS build (lib/php.nix) + FrankenPHP (lib/frankenphp.nix) used by the
# OCI images.
#
# Outer layer: the flake injects the sqlite-database-integration source and the
# Rust nixpkgs for the native extensions; both default to null so importing the
# file directly still works for mysql-only deployments.
{
  d1DriverSrc ? null,
  rustNixpkgs ? null,
}:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  inherit (lib)
    mkOption
    mkEnableOption
    mkIf
    mkMerge
    types
    optional
    optionalString
    concatMapStringsSep
    getExe
    ;

  cfg = config.services.wordpress-nix;

  managed = cfg.source.type == "managed";
  d1 = cfg.database.type == "d1";

  php = import ../lib/php.nix {
    inherit pkgs;
    php = cfg.php;
    optimize = cfg.phpOptimize;
    # display_errors leaks paths to visitors; keep it off unless debugging.
    iniExtra = cfg.phpIniExtra + optionalString (!cfg.debug) "\ndisplay_errors = Off\n";
  };
  frankenphp = import ../lib/frankenphp.nix { inherit pkgs php; };
  wpCli = pkgs.wp-cli.override { inherit php; };

  docroot = "${cfg.stateDir}/www";
  secretsFile = "${cfg.stateDir}/wp-secrets.php";
  runtimeSaltsFile = "/run/wordpress/wp-salts.php";

  # --- D1 database mode plumbing ---
  # Native wp_mysql_parser + wp_d1_client extensions, mirroring the OCI image.
  phpExtensions =
    if d1 then
      import ../lib/php-extensions.nix {
        inherit pkgs php;
        src = d1DriverSrc;
        rustPkgs =
          if rustNixpkgs != null then
            rustNixpkgs.legacyPackages.${pkgs.stdenv.hostPlatform.system}
          else
            pkgs;
      }
    else
      null;

  # FrankenPHP's embedded PHP does not inherit the CLI wrapper's compiled-in
  # scan directory; set it explicitly (buildEnv config + native extensions).
  phpIniScanDir = lib.concatStringsSep ":" (
    [ "${php}/lib" ] ++ optional (phpExtensions != null) "${phpExtensions.iniDir}"
  );

  # The SQLite Database Integration plugin, dereferenced (its wp-includes/
  # database tree is symlinked in the repo).
  d1Plugin =
    if d1 then
      pkgs.runCommandLocal "sqlite-database-integration-plugin" { } ''
        cp -rL ${d1DriverSrc}/packages/plugin-sqlite-database-integration $out
      ''
    else
      null;

  # The db.php drop-in, pointed at the store copy of the plugin. Installed
  # into wp-content by wordpress-init (a drop-in, so gitium ignores it).
  d1DropIn =
    if d1 then
      pkgs.runCommandLocal "wordpress-d1-db-drop-in" { } ''
        substitute ${d1Plugin}/wp-includes/database/d1/db.copy $out \
          --replace-fail '{SQLITE_IMPLEMENTATION_FOLDER_PATH}' '${d1Plugin}'
      ''
    else
      null;

  # --- managed source mode plumbing ---
  # The pinned core with the generated wp-config.php at its root. Core entries
  # are symlinked from the docroot into this tree; PHP resolves __FILE__
  # through the symlinks, so ABSPATH is this store path and wp-config.php is
  # found right here — while WP_CONTENT_DIR points back at the mutable tree.
  wordpressCore = import ../lib/wordpress-core.nix { inherit pkgs; };
  managedCore =
    if managed then
      pkgs.runCommandLocal "wordpress-managed-core" { } ''
        mkdir $out
        cp -rL ${wordpressCore}/. $out/
        chmod -R u+w $out
        rm -rf $out/wp-content
        rm -f $out/wp-config-sample.php
        cp ${wpConfig} $out/wp-config.php
      ''
    else
      null;

  dbLocal = cfg.database.createLocally && !d1;
  # Local DB uses MariaDB unix_socket auth (passwordless, OS-user matched);
  # external DB connects over TCP with a password.
  dbHost =
    if dbLocal then "localhost:${cfg.database.socket}" else "${cfg.database.host}:${toString cfg.database.port}";

  # Socket mode: Caddy binds a unix socket instead of a TCP port, so nothing in
  # the container listens on the network at all. Caddy's default socket mode is
  # 0200 (u=w), which a separate connector process could not open — hence the
  # explicit permission suffix.
  viaSocket = cfg.socketPath != "";
  socketDir = builtins.dirOf cfg.socketPath;

  # Normalized so identical paths collapse under lib.unique.
  mkRuntimeDir = path: "d ${path} 0750 ${cfg.user} ${cfg.group} -";

  # A unix socket is NOT valid as a site address — Caddy parses `unix/...` as the
  # host `unix` plus a path, then binds TCP :80 anyway. It belongs in the `bind`
  # directive inside the site block instead, with the site address kept as `:80`
  # so no scheme is inferred and automatic HTTPS stays off.
  siteAddress = if cfg.domain != "" && !viaSocket then cfg.domain else ":80";

  # Caddy's default unix socket mode is 0200 (u=w only), which no peer process
  # could open, so the mode is always stated explicitly.
  bindDirective = optionalString viaSocket "bind unix/${cfg.socketPath}|${cfg.socketMode}";

  # A unix socket has no peer address, so REMOTE_ADDR is meaningless and Caddy's
  # trusted_proxies (which parses an IP) cannot vouch for X-Forwarded-For. Take
  # the client IP from Cloudflare's header instead: the socket is unreachable
  # except through the connector in this same container, and Cloudflare strips
  # client-supplied CF-Connecting-IP at the edge, so it is trustworthy here.
  socketRemoteAddr = optionalString viaSocket ''
    // Socket mode: recover the real client IP from Cloudflare's header.
    if (!empty($_SERVER['HTTP_CF_CONNECTING_IP'])) {
        $_SERVER['REMOTE_ADDR'] = $_SERVER['HTTP_CF_CONNECTING_IP'];
    }
  '';

  caddyfile = pkgs.writeText "Caddyfile" ''
    {
      ${optionalString (cfg.domain != "" && cfg.acmeEmail != "") "email ${cfg.acmeEmail}"}

      frankenphp

      servers {
        timeouts {
          read_body 100s
          read_header 100s
          write 100s
          idle 100s
        }
        keepalive_interval 100s
        max_header_size 16KB
        trusted_proxies static private_ranges
        client_ip_headers X-Forwarded-For
        enable_full_duplex
      }

      order php_server before file_server
    }

    ${siteAddress} {
      ${bindDirective}

      @static {
        file
        path *.css *.eot *.gif *.ico *.jpeg *.jpg *.js *.otf *.png *.svg *.ttf *.webp *.woff *.woff2
      }

      root * ${docroot}
      encode br zstd gzip

      php_server
    }
  '';

  wpConfig = pkgs.writeText "wp-config.php" ''
    <?php
    // Managed by services.wordpress-nix — regenerated on activation; do not edit.

    ${socketRemoteAddr}
    ${optionalString managed ''
      // Managed mode: core lives in the (read-only) store; content is mutable.
      define('WP_CONTENT_DIR', '${docroot}/wp-content');
    ''}
    ${optionalString d1 ''
      // Cloudflare D1 via the site Worker's authenticated proxy (db.php drop-in).
      define('WP_D1_PROXY_URL', '${cfg.database.d1.proxyUrl}');
      define('WP_D1_PROXY_TOKEN', trim((string) @file_get_contents('${cfg.database.d1.tokenFile}')));
      define('WP_D1_HTTP_TIMEOUT_MS', ${toString cfg.database.d1.requestTimeoutMs});
    ''}
    // Database settings${optionalString d1 " (placeholders — the D1 drop-in owns the connection)"}
    define('DB_HOST', '${if d1 then "localhost" else dbHost}');
    define('DB_USER', '${cfg.database.user}');
    define('DB_NAME', '${cfg.database.name}');
    define('DB_CHARSET', 'utf8');
    define('DB_COLLATE', ''');

    $table_prefix = '${cfg.tablePrefix}';

    // DB password + authentication salts (kept out of the Nix store).
    require '${secretsFile}';

    // Debug mode
    define('WP_DEBUG', ${if cfg.debug then "true" else "false"});
    ${optionalString (cfg.domain != "") ''
      define('WP_HOME', 'https://${cfg.domain}');
      define('WP_SITEURL', 'https://${cfg.domain}');
    ''}
    define('FS_METHOD', 'direct');
    ${
      if managed then
        ''
          // The core version is pinned by the platform flake; never self-update.
          define('WP_AUTO_UPDATE_CORE', false);
          define('AUTOMATIC_UPDATER_DISABLED', true);
        ''
      else
        "define('WP_AUTO_UPDATE_CORE', 'minor');"
    }
    define('CONCATENATE_SCRIPTS', false);
    ${optionalString (!managed) "define('DISALLOW_FILE_EDIT', true);"}
    ${optionalString (cfg.source.type == "git") "define('DISALLOW_FILE_MODS', true);"}
    ${optionalString (managed && cfg.source.siteRepo.deployKeyFile != null) ''
      // gitium authenticates pushes with the same deploy key init cloned with.
      define('GIT_KEY_FILE', '${cfg.source.siteRepo.deployKeyFile}');
    ''}
    define('DISABLE_WP_CRON', true);
    define('WP_CACHE', true);
    define('WP_POST_REVISIONS', 5);
    define('EMPTY_TRASH_DAYS', 7);
    define('WP_MEMORY_LIMIT', '1G');

    ${cfg.configExtra}

    /** Absolute path to the WordPress directory. */
    if ( ! defined( 'ABSPATH' ) ) {
        define( 'ABSPATH', __DIR__ . '/' );
    }

    /** Sets up WordPress vars and included files. */
    require_once ABSPATH . 'wp-settings.php';
  '';

  initScript = pkgs.writeShellScript "wordpress-init" ''
    set -euo pipefail
    umask 077

    DOCROOT=${lib.escapeShellArg docroot}
    SECRETS=${lib.escapeShellArg secretsFile}

    # A crashed core update can leave the site stuck in maintenance mode.
    rm -f "$DOCROOT/.maintenance" || true

    ${
      if cfg.source.type == "state" then
        ''
          # State mode: real, writable tree. Download core on first boot.
          if [ ! -f "$DOCROOT/wp-includes/version.php" ]; then
            echo "Downloading WordPress core (${cfg.source.version})"
            ${getExe wpCli} core download --path="$DOCROOT" --version=${lib.escapeShellArg cfg.source.version}
          fi
        ''
      else if managed then
        ''
          # Managed mode: seed the site repo (once), then (re)point the core
          # symlinks at the pinned store docroot — atomic and idempotent, so a
          # platform core bump takes effect on the next start.
          ${optionalString (cfg.source.siteRepo.url != null) ''
            if [ ! -e "$DOCROOT/.git" ]; then
              echo "Seeding site repo ${cfg.source.siteRepo.url}"
              git -C "$DOCROOT" init -b ${lib.escapeShellArg cfg.source.siteRepo.branch}
              git -C "$DOCROOT" remote add origin ${lib.escapeShellArg cfg.source.siteRepo.url}
              ${optionalString (cfg.source.siteRepo.deployKeyFile != null) ''
                export GIT_SSH_COMMAND="ssh -i ${lib.escapeShellArg cfg.source.siteRepo.deployKeyFile} -o StrictHostKeyChecking=accept-new"
              ''}
              git -C "$DOCROOT" fetch origin ${lib.escapeShellArg cfg.source.siteRepo.branch}
              git -C "$DOCROOT" checkout -f ${lib.escapeShellArg cfg.source.siteRepo.branch}
            fi
          ''}

          CORE=${managedCore}
          for entry in "$CORE"/*; do
            base=$(basename "$entry")
            case "$base" in
              wp-content|wp-config.php) continue ;;
            esac
            ln -sfn "$entry" "$DOCROOT/$base"
          done

          mkdir -p "$DOCROOT/wp-content/plugins" "$DOCROOT/wp-content/themes" \
                   "$DOCROOT/wp-content/mu-plugins" "$DOCROOT/wp-content/languages"

          # A themeless site white-screens: seed the core default themes on
          # first boot (they become part of the site repo via gitium).
          if [ -z "$(ls -A "$DOCROOT/wp-content/themes" 2>/dev/null)" ]; then
            cp -aL --no-preserve=mode ${wordpressCore}/wp-content/themes/. "$DOCROOT/wp-content/themes/"
          fi
        ''
      else
        ''
          # Git mode: rebuild the writable symlink farm into the read-only store docroot.
          SRC=${lib.escapeShellArg (toString cfg.source.path)}
          for entry in "$SRC"/*; do
            base=$(basename "$entry")
            case "$base" in
              wp-content|wp-config.php|wp-config-sample.php) continue ;;
            esac
            ln -sfn "$entry" "$DOCROOT/$base"
          done
          mkdir -p "$DOCROOT/wp-content"
          for sub in plugins themes mu-plugins languages; do
            if [ -e "$SRC/wp-content/$sub" ]; then
              ln -sfn "$SRC/wp-content/$sub" "$DOCROOT/wp-content/$sub"
            fi
          done
        ''
    }

    # Writable content dirs (all modes; also created by tmpfiles).
    mkdir -p "$DOCROOT/wp-content/uploads" "$DOCROOT/wp-content/cache" "$DOCROOT/wp-content/upgrade"

    ${optionalString (cfg.source.type == "state") (
      concatMapStringsSep "\n" (p: ''
        mkdir -p "$DOCROOT/wp-content/mu-plugins"
        cp -aL --no-preserve=mode ${p}/. "$DOCROOT/wp-content/mu-plugins/"
      '') cfg.muPlugins
    )}
    ${optionalString managed (
      # Refresh platform mu-plugins without disturbing the site's own
      # (gitium ignores platform-*.php via the site repo's .gitignore).
      ''
        rm -f "$DOCROOT/wp-content/mu-plugins"/platform-*.php
      ''
      + concatMapStringsSep "\n" (p: ''
        cp -aL --no-preserve=mode ${p}/. "$DOCROOT/wp-content/mu-plugins/"
      '') cfg.muPlugins
    )}

    ${optionalString d1 ''
      # The D1 database drop-in (regenerated each boot; a drop-in, so gitium
      # ignores it and WordPress loads it in place of MySQL).
      install -m 0644 ${d1DropIn} "$DOCROOT/wp-content/db.php"
    ''}

    # --- secrets: salts (provided or generated once), DB password every run ---
    ${
      if cfg.saltsFile != null then
        ''
          # Platform-shared salts: materialize to tmpfs so the secret never
          # rests on the state filesystem; regenerate the require shim.
          {
            echo "<?php"
            cat ${lib.escapeShellArg cfg.saltsFile}
          } > ${runtimeSaltsFile}
          chmod 400 ${runtimeSaltsFile}
          {
            echo "<?php"
            echo "require '${runtimeSaltsFile}';"
          } > "$SECRETS"
        ''
      else
        ''
          if [ ! -f "$SECRETS" ]; then
            {
              echo "<?php"
              for k in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY \
                       AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
                printf "define('%s', '%s');\n" "$k" "$(head -c 48 /dev/urandom | base64 | tr -d '\n')"
              done
            } > "$SECRETS"
          fi
        ''
    }

    sed -i "/define('DB_PASSWORD'/d" "$SECRETS"
    if [ -n "''${CREDENTIALS_DIRECTORY:-}" ] && [ -f "''${CREDENTIALS_DIRECTORY:-}/db_password" ]; then
      pw="$(cat "$CREDENTIALS_DIRECTORY/db_password")"
      enc="$(printf '%s' "$pw" | base64 -w0)"
      printf "define('DB_PASSWORD', base64_decode('%s'));\n" "$enc" >> "$SECRETS"
    else
      printf "define('DB_PASSWORD', ''');\n" >> "$SECRETS"
    fi
    chmod 600 "$SECRETS"

    ${optionalString (cfg.source.manageWpConfig && !managed) ''
      rm -f "$DOCROOT/wp-config.php"
      cp ${wpConfig} "$DOCROOT/wp-config.php"
      chmod 644 "$DOCROOT/wp-config.php"
    ''}
  '';

  # Convenience `wp` on the system PATH. Run DB-touching commands as the service
  # user: `sudo -u ${cfg.user} wp ...` (unix_socket auth matches the OS user).
  wpWrapper = pkgs.writeShellScriptBin "wp" ''
    export HOME=${lib.escapeShellArg cfg.stateDir}
    export WP_CLI_CACHE_DIR=${lib.escapeShellArg "${cfg.stateDir}/.wp-cli/cache"}
    export PHP_INI_SCAN_DIR=${lib.escapeShellArg phpIniScanDir}
    exec ${getExe wpCli} --path=${lib.escapeShellArg docroot} "$@"
  '';

  serviceEnv = {
    HOME = cfg.stateDir;
    WP_CLI_CACHE_DIR = "${cfg.stateDir}/.wp-cli/cache";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    # Applies to FrankenPHP's embedded PHP and to wp-cli alike (the D1
    # native extensions load from here in d1 mode).
    PHP_INI_SCAN_DIR = phpIniScanDir;
  };

  # gitium shells out to git over ssh from web requests and cron.
  gitPath = optional managed pkgs.git ++ optional managed pkgs.openssh;
in
{
  options.services.wordpress-nix = {
    enable = mkEnableOption "WordPress served by FrankenPHP (standalone)";

    php = mkOption {
      type = types.package;
      default = pkgs.php83;
      defaultText = lib.literalExpression "pkgs.php83";
      example = lib.literalExpression "pkgs.php84";
      description = "Base PHP interpreter. The module wraps it with the optimized ZTS build.";
    };

    phpOptimize = mkOption {
      type = types.bool;
      default = true;
      description = "Apply the aggressive clang/LTO/march optimization pass to PHP.";
    };

    phpIniExtra = mkOption {
      type = types.lines;
      default = "";
      description = "Extra php.ini lines appended after conf/php.ini.";
    };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/wordpress";
      description = "State directory: holds the document root, uploads, secrets and Caddy certs.";
    };

    user = mkOption {
      type = types.str;
      default = "wordpress";
      description = "User the service runs as.";
    };
    group = mkOption {
      type = types.str;
      default = "wordpress";
      description = "Group the service runs as.";
    };

    domain = mkOption {
      type = types.str;
      default = "";
      example = "blog.example.com";
      description = "Public FQDN. Set → Caddy auto-HTTPS (ACME) on :443 + :80 redirect; empty → bind :80 only.";
    };

    acmeEmail = mkOption {
      type = types.str;
      default = "";
      description = "ACME account email (required when domain is set).";
    };

    socketPath = mkOption {
      type = types.str;
      default = "";
      example = "/run/wordpress/wp.sock";
      description = ''
        Serve over this unix socket instead of a TCP port. Set → Caddy binds
        only the socket (no :80, no :443, no ACME) and nothing listens on the
        network; empty → the `domain`/`:80` behaviour above.

        Must live in its own directory, which this module creates 0750 owned by
        `user` — Caddy runs as that user and could not create a socket directly
        in root-owned /run.

        Intended for a co-located reverse proxy or tunnel connector, which must
        be able to open the socket — see `socketMode`. In this mode the client
        IP is taken from `CF-Connecting-IP`, since a unix socket has no peer
        address.
      '';
    };

    socketMode = mkOption {
      type = types.str;
      default = "0660";
      example = "0666";
      description = ''
        Permission mode for `socketPath`. Caddy's own default is 0200 (u=w),
        which no other process could open, so this is set explicitly. 0660 pairs
        with putting the peer process in this service's group.
      '';
    };

    openFirewall = mkOption {
      type = types.bool;
      default = true;
      description = "Open :80 (and :443 tcp+udp when a domain is set) in the firewall.";
    };

    source = {
      type = mkOption {
        type = types.enum [
          "state"
          "git"
          "managed"
        ];
        default = "state";
        description = ''
          `state` = mutable core in ${docroot}; `git` = read-only source.path
          document root; `managed` = pinned store core (symlinked) + mutable,
          git-seeded wp-content with file mods allowed (the split-plane admin
          backend).
        '';
      };
      version = mkOption {
        type = types.str;
        default = "latest";
        description = "WordPress version to download in state mode (`wp core download --version`).";
      };
      path = mkOption {
        type = types.nullOr types.path;
        default = null;
        example = lib.literalExpression "inputs.my-wordpress";
        description = "Git mode: a store path / flake input that is the WordPress document root (read-only).";
      };
      siteRepo = {
        url = mkOption {
          type = types.nullOr types.str;
          default = null;
          example = "git@github.com:Avunu/site-example.git";
          description = "Managed mode: the site repo cloned into the WP root on first boot (gitium pushes back to it).";
        };
        branch = mkOption {
          type = types.str;
          default = "main";
          description = "Managed mode: the branch to seed from.";
        };
        deployKeyFile = mkOption {
          type = types.nullOr types.str;
          default = null;
          example = "/run/agenix/site-git-deploy-key";
          description = ''
            Runtime path to an SSH deploy key (never in the Nix store) used
            for the initial clone and, via GIT_KEY_FILE, by gitium's pushes.
          '';
        };
      };
      manageWpConfig = mkOption {
        type = types.bool;
        default = true;
        description = "Generate and install wp-config.php. Set false if the git source ships its own.";
      };
    };

    saltsFile = mkOption {
      type = types.nullOr types.str;
      default = null;
      example = "/run/agenix/site-wp-salts";
      description = ''
        Runtime path to the authentication salts (the 8-define block, without
        the PHP opening tag) — shared with the frontend plane's
        WORDPRESS_SALTS secret. Unset: salts are generated once locally.
      '';
    };

    database = {
      type = mkOption {
        type = types.enum [
          "mysql"
          "d1"
        ];
        default = "mysql";
        description = ''
          `mysql` = MariaDB/MySQL (local or external); `d1` = the shared
          Cloudflare D1 database through the site Worker's authenticated
          /__d1 proxy (no local database at all).
        '';
      };
      d1 = {
        proxyUrl = mkOption {
          type = types.str;
          default = "";
          example = "https://example.com/__d1";
          description = "Base URL of the site Worker's authenticated D1 proxy route.";
        };
        tokenFile = mkOption {
          type = types.nullOr types.str;
          default = null;
          example = "/run/agenix/site-d1-proxy-token";
          description = "Runtime path to the bearer token (matches the Worker's D1_PROXY_TOKEN secret).";
        };
        requestTimeoutMs = mkOption {
          type = types.int;
          default = 20000;
          description = "HTTP request timeout for D1 proxy calls.";
        };
      };
      createLocally = mkEnableOption "a local MariaDB (passwordless unix_socket auth)";
      package = mkOption {
        type = types.package;
        default = pkgs.mariadb;
        defaultText = lib.literalExpression "pkgs.mariadb";
        description = "MariaDB package for the local database.";
      };
      host = mkOption {
        type = types.str;
        default = "127.0.0.1";
        description = "Database host (external DB only).";
      };
      port = mkOption {
        type = types.port;
        default = 3306;
        description = "Database TCP port (external DB only).";
      };
      socket = mkOption {
        type = types.str;
        default = "/run/mysqld/mysqld.sock";
        description = "Unix socket path (local DB only).";
      };
      name = mkOption {
        type = types.str;
        default = "wordpress";
        description = "Database name.";
      };
      user = mkOption {
        type = types.str;
        default = "wordpress";
        description = "Database user. For local (socket) auth this must equal `user`.";
      };
      passwordFile = mkOption {
        type = types.nullOr types.path;
        default = null;
        description = "File containing the DB password (external DB). Injected via systemd LoadCredential.";
      };
    };

    tablePrefix = mkOption {
      type = types.str;
      default = "wp_";
      description = "WordPress table prefix.";
    };

    cron.enable = mkOption {
      type = types.bool;
      default = true;
      description = "Run wp-cron on a systemd timer (WordPress' own cron is disabled).";
    };

    debug = mkOption {
      type = types.bool;
      default = false;
      description = "Enable WP_DEBUG and PHP display_errors.";
    };

    muPlugins = mkOption {
      type = types.listOf types.path;
      default = [ ../mu-plugins ];
      defaultText = lib.literalExpression "[ ../mu-plugins ]";
      description = "Must-use plugin directories copied into wp-content/mu-plugins (state mode).";
    };

    configExtra = mkOption {
      type = types.lines;
      default = "";
      description = "Extra PHP appended to the generated wp-config.php.";
    };
  };

  config = mkIf cfg.enable {
    assertions = [
      {
        assertion = cfg.source.type != "git" || cfg.source.path != null;
        message = "services.wordpress-nix: source.type = \"git\" requires source.path.";
      }
      {
        assertion = cfg.domain == "" || cfg.acmeEmail != "";
        message = "services.wordpress-nix: setting `domain` (enables ACME) requires `acmeEmail`.";
      }
      {
        assertion = !viaSocket || cfg.domain == "";
        message = "services.wordpress-nix: socketPath and domain are mutually exclusive — ACME cannot terminate TLS on a unix socket. Serve over the socket and let the proxy or tunnel in front handle TLS.";
      }
      {
        # Caddy runs as cfg.user and has to create the socket, so its directory
        # must be one this service owns. /run itself is 0755 root:root.
        assertion =
          !viaSocket || (lib.hasPrefix "/" cfg.socketPath && socketDir != "/run" && socketDir != "/");
        message =
          "services.wordpress-nix: socketPath must be an absolute path inside its own directory"
          + " (e.g. /run/wordpress/wp.sock), not directly in /run — Caddy runs as"
          + " ${cfg.user} and cannot create a socket in a root-owned directory.";
      }
      {
        assertion = !dbLocal || cfg.database.user == cfg.user;
        message = "services.wordpress-nix: local DB uses unix_socket auth, so database.user must equal user.";
      }
      {
        assertion = !d1 || d1DriverSrc != null;
        message = "services.wordpress-nix: database.type = \"d1\" requires consuming the module via the flake's nixosModules (it injects the driver source).";
      }
      {
        assertion = !d1 || (cfg.database.d1.proxyUrl != "" && cfg.database.d1.tokenFile != null);
        message = "services.wordpress-nix: database.type = \"d1\" requires database.d1.proxyUrl and database.d1.tokenFile.";
      }
      {
        assertion = !d1 || !cfg.database.createLocally;
        message = "services.wordpress-nix: database.type = \"d1\" is remote-only; disable database.createLocally.";
      }
      {
        assertion = !managed || cfg.source.manageWpConfig;
        message = "services.wordpress-nix: managed mode generates wp-config.php into the store core; manageWpConfig must stay true.";
      }
    ];

    users.users = mkIf (cfg.user == "wordpress") {
      wordpress = {
        isSystemUser = true;
        group = cfg.group;
        home = cfg.stateDir;
      };
    };
    users.groups = mkIf (cfg.group == "wordpress") { wordpress = { }; };

    systemd.tmpfiles.rules = lib.unique ([
      "d ${cfg.stateDir}                     0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}                          0750 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/caddy               0700 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/caddy/data          0700 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/caddy/config        0700 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content               0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content/uploads       0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content/cache         0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content/upgrade       0750 ${cfg.user} ${cfg.group} -"
    ]
    # These two go through one formatter so lib.unique collapses them when the
    # socket lives in /run/wordpress alongside the salts.
    ++ optional (cfg.saltsFile != null) (mkRuntimeDir "/run/wordpress")
    # Caddy creates the socket here and owns the directory, so 0750 is enough:
    # a peer connector only needs traversal plus write on the 0660 socket.
    ++ optional viaSocket (mkRuntimeDir socketDir));

    systemd.services.wordpress-init = {
      description = "Initialize WordPress runtime state";
      wantedBy = [ "multi-user.target" ];
      before = [ "wordpress.service" ];
      after = optional dbLocal "mysql.service";
      requires = optional dbLocal "mysql.service";
      path = [
        wpCli
        pkgs.coreutils
        pkgs.gnused
        (cfg.database.package.client or cfg.database.package)
      ]
      ++ gitPath;
      environment = serviceEnv;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = cfg.user;
        Group = cfg.group;
        ReadWritePaths = [ cfg.stateDir ] ++ optional (cfg.saltsFile != null) "/run/wordpress";
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
        LoadCredential = optional (
          cfg.database.passwordFile != null
        ) "db_password:${cfg.database.passwordFile}";
        ExecStart = initScript;
      };
    };

    systemd.services.wordpress = {
      description = "WordPress (FrankenPHP)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [
        "network-online.target"
        "wordpress-init.service"
      ] ++ optional dbLocal "mysql.service";
      requires = [ "wordpress-init.service" ] ++ optional dbLocal "mysql.service";
      path = [
        wpCli
        (cfg.database.package.client or cfg.database.package)
      ]
      ++ gitPath;
      environment = serviceEnv // {
        XDG_DATA_HOME = "${cfg.stateDir}/caddy/data";
        XDG_CONFIG_HOME = "${cfg.stateDir}/caddy/config";
      };
      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = docroot;
        ExecStart = "${getExe frankenphp} run --config ${caddyfile} --adapter caddyfile";
        Restart = "always";
        RestartSec = "5";
        # A crash with Restart=always can leave the socket file behind, which
        # blocks the rebind. Clear it before every start.
        ExecStartPre = optional viaSocket "${pkgs.coreutils}/bin/rm -f ${cfg.socketPath}";
        # Bind :80/:443 without running as root. Socket mode binds no port, so it
        # needs no capability at all.
        AmbientCapabilities = optional (!viaSocket) "CAP_NET_BIND_SERVICE";
        CapabilityBoundingSet = optional (!viaSocket) "CAP_NET_BIND_SERVICE";
        # ProtectSystem=strict makes the whole hierarchy read-only, so the socket's
        # directory has to be opened up for Caddy to create the socket in it.
        ReadWritePaths = [ cfg.stateDir ] ++ optional viaSocket socketDir;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
        # NOTE: do NOT set MemoryDenyWriteExecute — opcache JIT needs W^X toggling
        # and would crash FrankenPHP at startup.
      };
    };

    systemd.services.wordpress-cron = mkIf cfg.cron.enable {
      description = "WordPress scheduled tasks (wp-cron)";
      after = [ "wordpress.service" ];
      requires = [ "wordpress.service" ];
      path = [
        wpCli
        (cfg.database.package.client or cfg.database.package)
      ]
      ++ gitPath;
      environment = serviceEnv;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        ReadWritePaths = [ cfg.stateDir ];
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
        ExecStart = "${getExe wpCli} cron event run --all --due-now --path=${docroot}";
      };
    };

    systemd.timers.wordpress-cron = mkIf cfg.cron.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "1min";
        Unit = "wordpress-cron.service";
      };
    };

    services.mysql = mkIf dbLocal {
      enable = true;
      package = cfg.database.package;
      ensureDatabases = [ cfg.database.name ];
      ensureUsers = [
        {
          name = cfg.database.user;
          ensurePermissions = {
            "${cfg.database.name}.*" = "ALL PRIVILEGES";
          };
        }
      ];
      settings.mysqld = {
        character-set-server = "utf8mb4";
        collation-server = "utf8mb4_unicode_ci";
      };
    };

    # Socket mode binds no port, so there is nothing to open.
    networking.firewall = mkIf (cfg.openFirewall && !viaSocket) {
      allowedTCPPorts = [ 80 ] ++ optional (cfg.domain != "") 443;
      allowedUDPPorts = optional (cfg.domain != "") 443;
    };

    environment.systemPackages = [ wpWrapper ];
  };
}
