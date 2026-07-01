# NixOS module: deploy a single WordPress instance served by FrankenPHP
# (standalone — Caddy terminates TLS itself via ACME; no nginx in front).
#
# Two source modes:
#   * state — WordPress core lives in a writable ${stateDir}/www, downloaded on
#             first boot with wp-cli; fully mutable (admin manages core / plugins
#             / themes via the UI). Best for flexible, server-specific instances.
#   * git   — source.path (a flake input / store path) IS the read-only document
#             root; ${stateDir}/www is a writable symlink farm into it with the
#             uploads/cache/upgrade dirs kept real. Best for source-managed sites.
#
# The PHP version is the `php` option; the module wraps it with the same
# optimized ZTS build (lib/php.nix) + FrankenPHP (lib/frankenphp.nix) used by the
# OCI images.
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

  dbLocal = cfg.database.createLocally;
  # Local DB uses MariaDB unix_socket auth (passwordless, OS-user matched);
  # external DB connects over TCP with a password.
  dbHost =
    if dbLocal then "localhost:${cfg.database.socket}" else "${cfg.database.host}:${toString cfg.database.port}";

  siteAddress = if cfg.domain != "" then cfg.domain else ":80";

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

    // Database settings
    define('DB_HOST', '${dbHost}');
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
    define('WP_AUTO_UPDATE_CORE', 'minor');
    define('CONCATENATE_SCRIPTS', false);
    define('DISALLOW_FILE_EDIT', true);
    ${optionalString (cfg.source.type == "git") "define('DISALLOW_FILE_MODS', true);"}
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

    # Writable content dirs (both modes; also created by tmpfiles).
    mkdir -p "$DOCROOT/wp-content/uploads" "$DOCROOT/wp-content/cache" "$DOCROOT/wp-content/upgrade"

    ${optionalString (cfg.source.type == "state") (
      concatMapStringsSep "\n" (p: ''
        mkdir -p "$DOCROOT/wp-content/mu-plugins"
        cp -aL --no-preserve=mode ${p}/. "$DOCROOT/wp-content/mu-plugins/"
      '') cfg.muPlugins
    )}

    # --- secrets: salts once, DB password every run (so rotation propagates) ---
    if [ ! -f "$SECRETS" ]; then
      {
        echo "<?php"
        for k in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY \
                 AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
          printf "define('%s', '%s');\n" "$k" "$(head -c 48 /dev/urandom | base64 | tr -d '\n')"
        done
      } > "$SECRETS"
    fi

    sed -i "/define('DB_PASSWORD'/d" "$SECRETS"
    if [ -n "''${CREDENTIALS_DIRECTORY:-}" ] && [ -f "''${CREDENTIALS_DIRECTORY:-}/db_password" ]; then
      pw="$(cat "$CREDENTIALS_DIRECTORY/db_password")"
      enc="$(printf '%s' "$pw" | base64 -w0)"
      printf "define('DB_PASSWORD', base64_decode('%s'));\n" "$enc" >> "$SECRETS"
    else
      printf "define('DB_PASSWORD', ''');\n" >> "$SECRETS"
    fi
    chmod 600 "$SECRETS"

    ${optionalString cfg.source.manageWpConfig ''
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
    exec ${getExe wpCli} --path=${lib.escapeShellArg docroot} "$@"
  '';

  serviceEnv = {
    HOME = cfg.stateDir;
    WP_CLI_CACHE_DIR = "${cfg.stateDir}/.wp-cli/cache";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  };
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

    openFirewall = mkOption {
      type = types.bool;
      default = true;
      description = "Open :80 (and :443 tcp+udp when a domain is set) in the firewall.";
    };

    source = {
      type = mkOption {
        type = types.enum [ "state" "git" ];
        default = "state";
        description = "`state` = mutable core in ${docroot}; `git` = read-only source.path document root.";
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
      manageWpConfig = mkOption {
        type = types.bool;
        default = true;
        description = "Generate and install wp-config.php. Set false if the git source ships its own.";
      };
    };

    database = {
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
        assertion = !dbLocal || cfg.database.user == cfg.user;
        message = "services.wordpress-nix: local DB uses unix_socket auth, so database.user must equal user.";
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

    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir}                     0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}                          0750 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/caddy               0700 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/caddy/data          0700 ${cfg.user} ${cfg.group} -"
      "d ${cfg.stateDir}/caddy/config        0700 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content               0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content/uploads       0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content/cache         0750 ${cfg.user} ${cfg.group} -"
      "d ${docroot}/wp-content/upgrade       0750 ${cfg.user} ${cfg.group} -"
    ];

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
      ];
      environment = serviceEnv;
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = cfg.user;
        Group = cfg.group;
        ReadWritePaths = [ cfg.stateDir ];
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
      ];
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
        # Bind :80/:443 without running as root.
        AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
        CapabilityBoundingSet = [ "CAP_NET_BIND_SERVICE" ];
        ReadWritePaths = [ cfg.stateDir ];
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
      ];
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

    networking.firewall = mkIf cfg.openFirewall {
      allowedTCPPorts = [ 80 ] ++ optional (cfg.domain != "") 443;
      allowedUDPPorts = optional (cfg.domain != "") 443;
    };

    environment.systemPackages = [ wpWrapper ];
  };
}
