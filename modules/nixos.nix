# NixOS module: one Bedrock WordPress site (lib.mkBedrockSite), served
# read-only by FrankenPHP, with its MariaDB, its migrate-on-deploy and its cron.
#
#   services.wordpress-nix = {
#     enable = true;
#     package = inputs.site-acme.packages.x86_64-linux.default;
#     socketPath = "/run/wordpress/wp.sock";        # or domain + acmeEmail
#     environmentFiles = [ "/run/secrets/env" ];    # the salts, S3_KEY, ...
#     settings.WP_HOME = "https://acme.example";
#   };
#
# The document root is ${package}/web, an immutable store path: Bedrock's own
# DISALLOW_FILE_MODS, and nothing under it is writable by anything. What the
# site writes goes to one of three places, and nowhere else:
#
#   uploadsDir   (/var/lib/wordpress/uploads)  what plugins write that is not an
#                attachment (builder CSS, fonts, form uploads, private
#                downloads); web/app/uploads links here. Caddy serves it with
#                file_server only, never PHP, and refuses the private prefixes
#                (denyUploadPaths)
#   stateDir     (/var/lib/wordpress)  Caddy's data, HOME, wp-cli's cache, the
#                migrate snapshots; never served
#   /tmp         each unit's own PrivateTmp (TMPDIR, get_temp_dir())
#
# Bedrock reads its configuration from ${package}/.env, a link to
# /run/wordpress/bedrock.env, which wordpress-init writes (0400) from
# `settings` (non-secret, in the store) and `environmentFiles` (secrets, never
# in the store). config/application.php is never edited.
#
# Units: wordpress-init, mysql (database.createLocally), wordpress-migrate
# (python-devops-nix's migrate flow: `wp core update-db` behind a verified
# snapshot, after an is-installed check), frankenphp, and the wordpress-cron
# timer. Each defines its own PrivateTmp, and all but mysql run under
# python-devops-nix's sandboxing baseline (lib.nixos.hardening) with the `jit`
# profile: opcache's JIT and PCRE's both map W+X memory.
#
# Outer layer: the flake passes python-devops-nix's lib as `base`.
{ base }:
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
    mkDefault
    mkRemovedOptionModule
    types
    optional
    optionalString
    concatStringsSep
    concatMapStringsSep
    getExe
    ;

  cfg = config.services.wordpress-nix;

  # What lib.mkBedrockSite records about the package.
  site = cfg.package.passthru.wordpressNix or { };

  # §7.2 of the v3 plan: sized so threads × memory_limit + the InnoDB pool +
  # 384 MiB fits the container's reservation (the cluster kind's memoryMB).
  profiles = {
    brochure = {
      threads = 3;
      memoryLimit = "192M";
      maxMemoryLimit = "384M";
      innodbBufferPool = "256M";
    };
    woocommerce = {
      threads = 4;
      memoryLimit = "384M";
      maxMemoryLimit = "768M";
      innodbBufferPool = "384M";
    };
  };
  profile = profiles.${cfg.profile};

  viaSocket = cfg.socketPath != "";
  dbLocal = cfg.database.createLocally;
  runtimeDir = "/run/wordpress";
  envFile = "${runtimeDir}/bedrock.env";
  docroot = "${cfg.package}/web";
  wpPath = "${docroot}/wp";

  sockets = base.nixos.unixSocket {
    inherit (cfg) user group;
    sockets = [
      {
        option = "services.wordpress-nix.socketPath";
        path = cfg.socketPath;
        example = "/run/wordpress/wp.sock";
      }
    ];
  };

  # Behind a unix socket there is no peer address and no TLS: the only client
  # is the tunnel's connector in this same container, and Cloudflare sets both
  # headers at the edge (and strips client-supplied CF-Connecting-IP), so PHP
  # takes the client's address and scheme from them. Before any script, so
  # Bedrock and WordPress see them as the request's own.
  socketPrepend = pkgs.writeText "wordpress-nix-socket-prepend.php" ''
    <?php
    if (PHP_SAPI !== 'cli') {
        if (!empty($_SERVER['HTTP_CF_CONNECTING_IP'])) {
            $_SERVER['REMOTE_ADDR'] = $_SERVER['HTTP_CF_CONNECTING_IP'];
        }
        if (($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? ''') === 'https') {
            $_SERVER['HTTPS'] = 'on';
            $_SERVER['SERVER_PORT'] = 443;
        }
    }
  '';

  # The PHP build is the same for every site (and so in the binary cache);
  # what is particular to this one is an ini file in a second scan directory,
  # read after conf/php.ini.
  php = import ../lib/php.nix {
    inherit pkgs;
    php = cfg.php;
    optimize = cfg.phpOptimize;
  };
  frankenphp = import ../lib/frankenphp.nix { inherit pkgs php; };
  wpCli = pkgs.wp-cli.override { inherit php; };

  # conf/php.ini's `error_log = /dev/stderr` is for a terminal. Under systemd,
  # fd 2 is a journald socket and open(2) on it fails (ENXIO), so it is unset:
  # FrankenPHP then hands each error to Caddy's JSON logger with a level, and
  # wp-cli writes it to stderr. display_errors would leak paths.
  #
  # opcache never revalidates: a deploy is a new store path, so a new key.
  # max_accelerated_files: core and a handful of plugins are well past the
  # default 2000 scripts.
  moduleIni = pkgs.writeTextDir "wordpress-nix.ini" (
    ''
      error_log =
      display_errors = Off
      display_startup_errors = Off
      memory_limit = ${cfg.memoryLimit}
      max_execution_time = 60
      upload_max_filesize = 64M
      post_max_size = 64M
      realpath_cache_size = 4096K
      opcache.memory_consumption = 96
      opcache.max_accelerated_files = 20000
      opcache.validate_timestamps = 0
      ${optionalString viaSocket "auto_prepend_file = ${socketPrepend}"}
    ''
    + cfg.phpIniExtra
  );

  # FrankenPHP's embedded PHP does not inherit the CLI wrapper's compiled-in
  # scan directory; set it explicitly, for both.
  phpIniScanDir = "${php}/lib:${moduleIni}";

  # ---- Bedrock's environment --------------------------------------------
  #
  # Rendered as a .env Bedrock's Dotenv reads: strings single-quoted (literal,
  # no interpolation), booleans and integers bare, null left out.
  renderValue =
    name: value:
    if builtins.isBool value then
      lib.boolToString value
    else if builtins.isInt value then
      toString value
    else
      "'${value}'";
  settingsLines = lib.mapAttrsToList (name: value: "${name}=${renderValue name value}") (
    lib.filterAttrs (_: v: v != null) cfg.settings
  );
  badSettings = lib.attrNames (
    lib.filterAttrs (
      _: v: builtins.isString v && (lib.hasInfix "'" v || lib.hasInfix "\n" v)
    ) cfg.settings
  );
  settingsFile = pkgs.writeText "wordpress-nix-settings.env" ''
    # services.wordpress-nix.settings (non-secret). wordpress-init appends the
    # environmentFiles to this and writes ${envFile}.
    ${concatStringsSep "\n" settingsLines}
  '';

  credentialName = i: "env-${toString i}";
  envCredentials = lib.imap0 (i: f: "${credentialName i}:${f}") cfg.environmentFiles;

  initScript = pkgs.writeShellScript "wordpress-init" ''
    set -euo pipefail
    umask 077
    tmp=${envFile}.tmp
    {
      cat ${settingsFile}
      ${concatMapStringsSep "\n" (i: ''
        printf '\n# environmentFiles[%d]\n' ${toString i}
        cat "$CREDENTIALS_DIRECTORY/${credentialName i}"
      '') (lib.range 0 (builtins.length cfg.environmentFiles - 1))}
    } > "$tmp"
    chmod 0400 "$tmp"
    mv -f "$tmp" ${envFile}
    # tmpfiles owns its mode and owner; this only covers a mount that came late.
    mkdir -p ${lib.escapeShellArg cfg.uploadsDir}
  '';

  # ---- the web server ------------------------------------------------------

  siteAddress = if cfg.domain != "" && !viaSocket then cfg.domain else ":80";
  bindDirective = optionalString viaSocket "bind unix/${cfg.socketPath}|${cfg.socketMode}";

  # Upload path lists as regular expressions: a `*` matches within one segment.
  globRe = p: lib.replaceStrings [ "." "*" ] [ "\\." "[^/]*" ] p;
  alternation = ps: "(?:${concatMapStringsSep "|" globRe ps})";
  # What stays on uploadsDir under its old /wp-content/uploads URL: served
  # (localUploadPrefixes) or refused (denyUploadPaths), never the media host.
  localRe = alternation (cfg.localUploadPrefixes ++ cfg.denyUploadPaths);
  mediaUrl = cfg.settings.S3_PUBLIC_URL or null;

  accessLogDirective = optionalString cfg.logging.accessLog ''
    log {
      output stderr
      format json
    }
  '';

  # §7.3 of the v3 plan. Caddy orders the directives itself (redir, rewrite,
  # handle, respond, php_server), whatever the order here.
  caddyfile = pkgs.writeText "Caddyfile" ''
    {
      ${optionalString (cfg.domain != "" && cfg.acmeEmail != "") "email ${cfg.acmeEmail}"}

      log {
        output stderr
        format json
      }

      frankenphp {
        num_threads ${toString cfg.threads}
      }

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
      ${accessLogDirective}
      root * ${docroot}
      encode zstd br gzip

      # Classic URLs a converted site's content and the outside world still
      # hold. Uploads that stay local keep their files here; every other
      # upload is an attachment, now in the media bucket.
      @legacyLocal path_regexp ll ^/wp-content/uploads/(${localRe}/.*)$
      redir @legacyLocal /app/uploads/{re.ll.1} 301
      @legacyMedia {
        path_regexp lm ^/wp-content/uploads/(.*)$
        not path_regexp ^/wp-content/uploads/${localRe}/
      }
      redir @legacyMedia ${
        if mediaUrl != null then lib.removeSuffix "/" mediaUrl else "/app/uploads"
      }/{re.lm.1} 301
      @legacyAdmin path /wp-admin /wp-admin/*
      redir @legacyAdmin /wp{uri} 301
      redir /wp-login.php /wp{uri} 308
      @legacyContent path_regexp lc ^/wp-content/(plugins|themes)/(.*)$
      rewrite @legacyContent /app/{re.lc.1}/{re.lc.2}

      # The one writable directory the web reaches: files only, never PHP,
      # whatever the extension, and never the private prefixes (which their
      # plugins serve through PHP themselves).
      handle /app/uploads/* {
        @private path_regexp ^/app/uploads/${alternation cfg.denyUploadPaths}(?:/|$)
        respond @private 403
        @exec path_regexp (?i)\.(ph(p\d?|tml|ar|ps|t)|cgi|pl|py|sh|htaccess|user\.ini)$
        respond @exec 403
        file_server
      }

      # No PHP file under wp-includes is an entry point but TinyMCE's loader.
      @incPhp {
        path_regexp (?i)^/wp/wp-includes/.*\.ph(p\d?|tml|ar)$
        not path /wp/wp-includes/js/tinymce/wp-tinymce.php
      }
      respond @incPhp 404

      respond /xmlrpc.php 403
      respond /wp/xmlrpc.php 403
      # Cron is the timer's (DISABLE_WP_CRON); the config is not a page.
      @notServed path /wp-cron.php /wp/wp-cron.php /wp-config.php /wp/wp-config-sample.php /.env*
      respond @notServed 404

      php_server
    }
  '';

  # ---- wp-cli, for the units and for operators ------------------------------

  serviceEnv = {
    HOME = cfg.stateDir;
    WP_CLI_CACHE_DIR = "${cfg.stateDir}/.wp-cli/cache";
    SSL_CERT_FILE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
    PHP_INI_SCAN_DIR = phpIniScanDir;
    TMPDIR = "/tmp";
  };

  wp = "${getExe wpCli} --path=${wpPath}";

  # `wp` on the system PATH. Run it as the service user (unix_socket auth
  # matches the OS user, and bedrock.env is 0400 to it):
  #   runuser -u wordpress -- wp option get home
  wpWrapper = pkgs.writeShellScriptBin "wp" ''
    ${lib.concatStringsSep "\n" (
      lib.mapAttrsToList (k: v: "export ${k}=${lib.escapeShellArg v}") serviceEnv
    )}
    exec ${wp} "$@"
  '';

  # Migrate and cron take one lock: cron never runs on a half-migrated
  # database, and skips its turn (exit 0) while a migrate holds it.
  lockFile = "${runtimeDir}/maintenance.lock";
  flock = "${pkgs.util-linux}/bin/flock";

  # ---- migrate on deploy ----------------------------------------------------
  #
  # python-devops-nix's migrate flow (lib.nixos.migrate.script), frappe-nix's
  # steps with WordPress's commands:
  #
  #   check      finish an interrupted rollback; skip (100) when this build
  #              already migrated, or when the database has no tables or no
  #              installed WordPress (first boot, before `site import`)
  #   dump       mysqldump | gzip into ${cfg.stateDir}/snapshots (0700, 0600)
  #   verify     the dump ends with mysqldump's "-- Dump completed"
  #   migrate    `wp core update-db`, `wp wc update` when WooCommerce is
  #              active, `wp cache flush`
  #   restore    drop every table and view, load the snapshot
  #   onSuccess  record the build
  #   onFailure  say so; the site keeps serving the new code on the restored
  #              schema, which WordPress supports (it asks for the database
  #              update in wp-admin)
  marker = "${cfg.stateDir}/.wordpress-migrate-build";
  restoring = "${cfg.stateDir}/.wordpress-migrate-restoring";
  dbName = cfg.database.name;
  connArgs = "--socket=${cfg.database.socket} --user=${cfg.database.user}";
  mysqlBin = "${cfg.database.package}/bin/mariadb";
  mysqldumpBin = "${cfg.database.package}/bin/mariadb-dump";
  gzip = "${pkgs.gzip}/bin/gzip";
  say = msg: lib.escapeShellArg "wordpress-migrate(${cfg.logging.site}): ${msg}";
  step =
    what: body:
    pkgs.writeShellScript "wordpress-migrate-${what}" ''
      set -uo pipefail
      ${body}
    '';

  migrateCheck = step "check" ''
    if [ -e ${restoring} ]; then
      SNAP="$(cat ${restoring})"
      if [ ! -f "$SNAP" ]; then
        echo "<3>"${say "an interrupted rollback was loading"}" $SNAP, which is gone; the database is partial. Restore it by hand, remove ${restoring}, then redeploy." >&2
        exit 1
      fi
      echo "<4>"${say "the last rollback was interrupted; finishing it from"}" $SNAP" >&2
      ${migrateRestore} "$SNAP" || { echo "<3>"${say "could not finish the interrupted rollback; the database is partial."} >&2; exit 1; }
    fi

    if [ "$(cat ${marker} 2>/dev/null || true)" = "${cfg.package}" ]; then
      echo ${say "build unchanged since the last successful migrate; skipping."}
      exit 100
    fi

    if ! TABLES="$(${mysqlBin} ${connArgs} -N -B -e \
        "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = '${dbName}';")"; then
      echo "<3>"${say "cannot reach the database"} >&2
      exit 1
    fi
    if [ "$TABLES" = "0" ] || ! ${wp} core is-installed 2>/dev/null; then
      echo "<5>"${say "WordPress is not installed in ${dbName}; nothing to migrate. Import or install the site, then redeploy."} >&2
      exit 100
    fi
    exit 0
  '';

  migrateDump = step "dump" ''
    set -e
    ${mysqldumpBin} ${connArgs} --single-transaction --quick --no-tablespaces \
      --routines --triggers ${dbName} | ${gzip}
  '';

  # Recorded in ${restoring} while it loads: a restore killed half-way leaves
  # a database that a later dump would call whole (see frappe-nix#296).
  migrateRestore = step "restore" ''
    printf '%s' "$1" > ${restoring}
    rc=0
    { echo "SET FOREIGN_KEY_CHECKS=0;"
      ${mysqlBin} ${connArgs} -N -e \
        "SELECT CONCAT('DROP ', IF(TABLE_TYPE='VIEW','VIEW','TABLE'), ' IF EXISTS \`', TABLE_NAME, '\`;') FROM information_schema.TABLES WHERE TABLE_SCHEMA = '${dbName}';"
    } | ${mysqlBin} ${connArgs} ${dbName} || rc=1
    ${gzip} -cd -- "$1" | ${mysqlBin} ${connArgs} ${dbName} || rc=1
    [ "$rc" -eq 0 ] && rm -f -- ${restoring}
    exit "$rc"
  '';

  migrateRun = step "run" ''
    set -e
    echo ${say "wp core update-db"}
    ${wp} core update-db
    if ${wp} plugin is-active woocommerce 2>/dev/null; then
      echo ${say "wp wc update"}
      ${wp} wc update
    fi
    ${wp} cache flush
  '';

  migrateSuccess = step "success" ''
    printf '%s' "${cfg.package}" > ${marker}
  '';

  migrateFailure = step "failure" ''
    echo "<3>"${say "migrate failed"}" (rollback: $MIGRATE_ROLLBACK); the site serves this build on the schema the rollback left. Investigate and redeploy." >&2
  '';

  migrateScript = base.nixos.migrate.script {
    inherit pkgs;
    label = "wordpress-migrate";
    target = cfg.logging.site;
    cfg = cfg.migrate;
    check = migrateCheck;
    migrate = migrateRun;
    onSuccess = migrateSuccess;
    onFailure = migrateFailure;
    dump = migrateDump;
    restore = migrateRestore;
    verify = base.nixos.migrate.verifiers.mysqldump { inherit pkgs; };
    snapshotDir = "${cfg.stateDir}/snapshots";
    prefix = "premigrate";
    extension = ".sql.gz";
  };

  cronScript = pkgs.writeShellScript "wordpress-cron" ''
    set -euo pipefail
    # An empty database (first boot, before `site import`) has nothing due.
    ${wp} core is-installed 2>/dev/null || exit 0
    # --due-now runs the events whose time has come (`--all --due-now` is
    # refused by wp-cli as contradictory).
    exec ${wp} cron event run --due-now
  '';

  # ---- units ----------------------------------------------------------------

  # journald fields shared with frappe-nix and odoo-nix (python-devops-nix's
  # lib.journald.fields): the log shipper's labels. SyslogIdentifier is the
  # unit's name.
  logFields =
    role: identifier:
    base.journald.fields {
      prefix = "wordpress";
      inherit role identifier;
      site = cfg.logging.site;
    };

  readWritePaths = [
    cfg.stateDir
  ]
  ++ optional (!lib.hasPrefix "${cfg.stateDir}/" cfg.uploadsDir) cfg.uploadsDir;

  harden = args: base.nixos.hardening ({ profiles = [ "jit" ]; } // args);

  phpPath = [
    wpCli
    pkgs.coreutils
    cfg.database.package
  ];
in
{
  imports =
    map
      (
        path:
        mkRemovedOptionModule
          (
            [
              "services"
              "wordpress-nix"
            ]
            ++ path
          )
          ''
            wordpress-nix 3.0 serves one Bedrock package (services.wordpress-nix.package,
            from lib.mkBedrockSite) and configures it through `settings` and
            `environmentFiles`. See docs/reference/nixos-module.md.
          ''
      )
      [
        [ "source" ]
        [ "saltsFile" ]
        [
          "database"
          "type"
        ]
        [
          "database"
          "turso"
        ]
        [
          "database"
          "host"
        ]
        [
          "database"
          "port"
        ]
        [
          "database"
          "passwordFile"
        ]
        [ "gitium" ]
        [ "plane" ]
        [ "tablePrefix" ]
        [ "configExtra" ]
        [ "muPlugins" ]
        [ "debug" ]
      ];

  options.services.wordpress-nix = {
    enable = mkEnableOption "a Bedrock WordPress site served read-only by FrankenPHP";

    package = mkOption {
      type = types.package;
      description = ''
        The site: lib.mkBedrockSite's package (a site flake's
        packages.<system>.default). `''${package}/web` is the document root,
        an immutable store path.
      '';
      example = lib.literalExpression "inputs.site-acme.packages.x86_64-linux.default";
    };

    php = mkOption {
      type = types.package;
      default = site.php or pkgs.php84;
      defaultText = lib.literalExpression "package.passthru.wordpressNix.php";
      description = "Base PHP: the one the package was built with. The module wraps it with the optimized ZTS build.";
    };

    phpOptimize = mkOption {
      type = types.bool;
      default = true;
      description = "Apply the aggressive clang/LTO/march optimization pass to PHP.";
    };

    phpIniExtra = mkOption {
      type = types.lines;
      default = "";
      description = "Extra php.ini lines, after conf/php.ini and the module's own.";
    };

    profile = mkOption {
      type = types.enum (lib.attrNames profiles);
      default = "brochure";
      description = ''
        The site's sizing: FrankenPHP threads, PHP memory_limit,
        WP_MAX_MEMORY_LIMIT (admin requests) and the InnoDB buffer pool.

        brochure     3 threads, 192M, 384M, 256M  (fits 1280 MiB)
        woocommerce  4 threads, 384M, 768M, 384M  (fits 2560 MiB)
      '';
    };

    threads = mkOption {
      type = types.ints.positive;
      default = profile.threads;
      defaultText = lib.literalExpression "the profile's";
      description = "FrankenPHP's num_threads.";
    };

    memoryLimit = mkOption {
      type = types.str;
      default = profile.memoryLimit;
      defaultText = lib.literalExpression "the profile's";
      description = "PHP's memory_limit, per thread.";
    };

    oomScoreAdjust = {
      database = mkOption {
        type = types.int;
        default = -500;
        description = "OOMScoreAdjust of mysql: under memory pressure the kernel kills PHP, never the database.";
      };
      php = mkOption {
        type = types.int;
        default = 300;
        description = "OOMScoreAdjust of frankenphp, wordpress-migrate and wordpress-cron.";
      };
    };

    stateDir = mkOption {
      type = types.path;
      default = "/var/lib/wordpress";
      description = "Caddy's data, HOME, wp-cli's cache and the migrate snapshots. Never served.";
    };

    uploadsDir = mkOption {
      type = types.path;
      default = site.uploadsDir or "${cfg.stateDir}/uploads";
      defaultText = lib.literalExpression "package.passthru.wordpressNix.uploadsDir";
      description = ''
        The writable uploads directory web/app/uploads links to (fixed when the
        package is built). Served by file_server only.
      '';
    };

    denyUploadPaths = mkOption {
      type = types.listOf types.str;
      default = [
        "woocommerce_uploads"
        "gravity_forms"
        "wpo_wcpdf*"
        "wc-logs"
        "fluentform/temp"
      ];
      description = ''
        Directories under uploads HTTP never reaches (403): private downloads
        and form files their plugins hand out through PHP. `*` matches within
        one path segment.
      '';
    };

    localUploadPrefixes = mkOption {
      type = types.listOf types.str;
      default = [
        "cwicly"
        "fonts"
        "wpcodebox"
        "fluentform"
      ];
      description = ''
        Directories under uploads that are not attachments and stay on
        uploadsDir: an old /wp-content/uploads/<prefix>/ URL moves to
        /app/uploads/<prefix>/, where every other old uploads URL moves to the
        media bucket (settings.S3_PUBLIC_URL).
      '';
    };

    user = mkOption {
      type = types.str;
      default = "wordpress";
      description = "User every unit but mysql runs as.";
    };
    group = mkOption {
      type = types.str;
      default = "wordpress";
      description = "Its group: a peer that connects to socketPath joins it.";
    };

    domain = mkOption {
      type = types.str;
      default = "";
      example = "blog.example.com";
      description = "Public FQDN. Set: Caddy terminates TLS itself (ACME) on :443 and :80. Empty: :80 only (or socketPath).";
    };

    acmeEmail = mkOption {
      type = types.str;
      default = "";
      description = "ACME account email (required with domain).";
    };

    socketPath = mkOption {
      type = types.str;
      default = "";
      example = "/run/wordpress/wp.sock";
      description = ''
        Serve over this unix socket instead of a TCP port, for a tunnel
        connector in the same container. Nothing then listens on the network,
        and PHP takes the client's address from CF-Connecting-IP and its
        scheme from X-Forwarded-Proto. Must be in its own directory.
      '';
    };

    socketMode = mkOption {
      type = types.str;
      default = "0660";
      description = "Mode of socketPath (Caddy's own default, 0200, opens to nobody).";
    };

    openFirewall = mkOption {
      type = types.bool;
      default = true;
      description = "Open :80 (and :443 with domain). Socket mode opens nothing.";
    };

    environmentFiles = mkOption {
      # Strings, not paths: a path literal would be copied into the store.
      type = types.listOf (types.strMatching "/.+");
      default = [ ];
      example = [ "/run/secrets/env" ];
      description = ''
        Files of KEY='value' lines appended to Bedrock's environment, after
        `settings`: the secrets (the eight salts, S3_KEY and S3_SECRET, mail
        tokens). Read through systemd's LoadCredential, so they may be root's
        0400 files; never copied to the store.
      '';
    };

    settings = mkOption {
      description = ''
        Bedrock's environment, non-secret (RFC 42): written to
        /run/wordpress/bedrock.env, which `''${package}/.env` links to.
        Strings are single-quoted, booleans and integers bare, null leaves a
        name out. Lands in the store: secrets go in `environmentFiles`.

        Besides Bedrock's own names, the platform reads WC_LOG_HANDLER,
        S3_DIRECT_UPLOADS and WP_MAX_MEMORY_LIMIT (mu-plugins/platform-environment.php),
        and wp-cloud-files the S3_* names.
      '';
      default = { };
      type = types.submodule (
        { config, ... }:
        {
          freeformType = types.attrsOf (
            types.nullOr (
              types.oneOf [
                types.bool
                types.int
                types.str
              ]
            )
          );
          options = {
            WP_ENV = mkOption {
              type = types.str;
              default = "production";
              description = "Bedrock's environment: production, staging or development.";
            };
            WP_HOME = mkOption {
              type = types.str;
              default = if cfg.domain != "" then "https://${cfg.domain}" else "http://localhost";
              defaultText = lib.literalExpression ''if domain != "" then "https://''${domain}" else "http://localhost"'';
              description = "The site's URL.";
            };
            WP_SITEURL = mkOption {
              type = types.str;
              default = "${config.WP_HOME}/wp";
              defaultText = lib.literalExpression ''"''${WP_HOME}/wp"'';
              description = "Where core lives: Bedrock's web/wp.";
            };
            DB_NAME = mkOption {
              type = types.str;
              default = cfg.database.name;
              defaultText = lib.literalExpression "database.name";
              description = "Database name.";
            };
            DB_USER = mkOption {
              type = types.str;
              default = cfg.database.user;
              defaultText = lib.literalExpression "database.user";
              description = "Database user.";
            };
            DB_PASSWORD = mkOption {
              type = types.str;
              default = "";
              description = "Empty: unix_socket auth. An external database's password goes in environmentFiles.";
            };
            DB_HOST = mkOption {
              type = types.str;
              default = "localhost:${cfg.database.socket}";
              defaultText = lib.literalExpression ''"localhost:''${database.socket}"'';
              description = "Database host; host:socket for a unix socket.";
            };
            DISABLE_WP_CRON = mkOption {
              type = types.bool;
              default = true;
              description = "The wordpress-cron timer runs cron; /wp-cron.php is not served.";
            };
            WP_TEMP_DIR = mkOption {
              type = types.str;
              default = "/tmp";
              description = "Each unit's own PrivateTmp.";
            };
            TMPDIR = mkOption {
              type = types.str;
              default = "/tmp";
              description = "Each unit's own PrivateTmp.";
            };
            WC_LOG_HANDLER = mkOption {
              type = types.nullOr types.str;
              default = "WC_Log_Handler_DB";
              description = "WooCommerce's logs go to the database, not to files under uploads.";
            };
            WP_MAX_MEMORY_LIMIT = mkOption {
              type = types.str;
              default = profile.maxMemoryLimit;
              defaultText = lib.literalExpression "the profile's";
              description = "memory_limit of admin and image requests (the admin_memory_limit filter).";
            };
            WORDPRESS_NIX_PRODUCTION_ONLY_PLUGINS = mkOption {
              type = types.str;
              default = concatStringsSep "," (site.productionOnlyPlugins or [ ]);
              defaultText = lib.literalExpression "package.passthru.wordpressNix.productionOnlyPlugins";
              description = "Plugin directories loaded only when WP_ENV is production.";
            };
            WORDPRESS_NIX_DEVELOPMENT_ONLY_PLUGINS = mkOption {
              type = types.str;
              default = concatStringsSep "," (site.developmentOnlyPlugins or [ ]);
              defaultText = lib.literalExpression "package.passthru.wordpressNix.developmentOnlyPlugins";
              description = "Plugin directories loaded only when WP_ENV is development.";
            };
          };
        }
      );
    };

    database = {
      createLocally = mkOption {
        type = types.bool;
        default = true;
        description = "A local MariaDB with unix_socket auth: no password anywhere.";
      };
      package = mkOption {
        type = types.package;
        default = pkgs.mariadb;
        defaultText = lib.literalExpression "pkgs.mariadb";
        description = "MariaDB: the server, and the client the migrate unit uses.";
      };
      socket = mkOption {
        type = types.str;
        default = "/run/mysqld/mysqld.sock";
        description = "The server's unix socket.";
      };
      name = mkOption {
        type = types.str;
        default = "wordpress";
        description = "Database name.";
      };
      user = mkOption {
        type = types.str;
        default = cfg.user;
        defaultText = lib.literalExpression "user";
        description = "Database user. With createLocally it must be `user` (unix_socket auth).";
      };
    };

    migrate = base.nixos.migrate.options {
      what = "`wp core update-db` (and `wp wc update` when WooCommerce is active)";
      dumpTool = "mysqldump";
      where = "in stateDir/snapshots";
    };

    cron = {
      enable = mkOption {
        type = types.bool;
        default = true;
        description = "Run due wp-cron events every minute on a timer (a preview turns it off).";
      };
      randomizedDelaySec = mkOption {
        type = types.str;
        default = "0";
        example = "20s";
        description = ''
          The timer's RandomizedDelaySec: spreads many sites' cron runs on one
          node apart. Off until measurements show they line up.
        '';
      };
    };

    logging = {
      site = mkOption {
        type = types.str;
        default = if cfg.domain != "" then cfg.domain else "wordpress";
        defaultText = lib.literalExpression ''if domain != "" then domain else "wordpress"'';
        description = "APP_SITE on every journal entry of this module's units.";
      };
      accessLog = mkOption {
        type = types.bool;
        default = true;
        description = "One JSON line per request (Caddy's access log, which redacts Cookie and Authorization).";
      };
    };
  };

  config = mkIf cfg.enable {
    # The project's binary cache: the optimized PHP builds. Priority 41 sits
    # behind cache.nixos.org. Same key as flake.nix's nixConfig.
    nix.settings = {
      substituters = [ "https://wordpress-nix.cachix.org?priority=41" ];
      trusted-public-keys = [
        "wordpress-nix.cachix.org-1:CYaSELCIUp9U26Ko+ajzE7M+l/dtiG5hBLEtvFXLUeA="
      ];
    };

    assertions = sockets.assertions ++ [
      {
        assertion = site ? envFile;
        message = "services.wordpress-nix.package must be a lib.mkBedrockSite package (it has no passthru.wordpressNix).";
      }
      {
        assertion = (site.envFile or envFile) == envFile;
        message = "services.wordpress-nix: the package's .env links to ${site.envFile or "?"}, but this module writes ${envFile}; build it with mkBedrockSite's default envFile.";
      }
      {
        assertion = (site.uploadsDir or cfg.uploadsDir) == cfg.uploadsDir;
        message = "services.wordpress-nix: uploadsDir is ${cfg.uploadsDir}, but the package's web/app/uploads links to ${site.uploadsDir or "?"}.";
      }
      {
        assertion = cfg.domain == "" || cfg.acmeEmail != "";
        message = "services.wordpress-nix: domain (ACME) requires acmeEmail.";
      }
      {
        assertion = !viaSocket || cfg.domain == "";
        message = "services.wordpress-nix: socketPath and domain are mutually exclusive: TLS terminates in front of a socket.";
      }
      {
        assertion = !dbLocal || cfg.database.user == cfg.user;
        message = "services.wordpress-nix: a local database uses unix_socket auth, so database.user must equal user.";
      }
      {
        assertion = badSettings == [ ];
        message = "services.wordpress-nix.settings: ${concatStringsSep ", " badSettings} hold a single quote or a newline, which a .env single-quoted value cannot.";
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

    systemd.tmpfiles.rules = lib.unique (
      [
        "d ${cfg.stateDir}              0750 ${cfg.user} ${cfg.group} -"
        "d ${cfg.stateDir}/caddy        0700 ${cfg.user} ${cfg.group} -"
        "d ${cfg.stateDir}/caddy/data   0700 ${cfg.user} ${cfg.group} -"
        "d ${cfg.stateDir}/caddy/config 0700 ${cfg.user} ${cfg.group} -"
        "d ${cfg.uploadsDir}            0750 ${cfg.user} ${cfg.group} -"
        "d ${runtimeDir} 0750 ${cfg.user} ${cfg.group} -"
      ]
      ++ sockets.tmpfiles
    );

    systemd.services.wordpress-init = {
      description = "Write Bedrock's environment";
      wantedBy = [ "multi-user.target" ];
      before = [
        "frankenphp.service"
        "wordpress-migrate.service"
        "wordpress-cron.service"
      ];
      # A new setting or secret rewrites the file on the next switch.
      restartTriggers = [ settingsFile ] ++ cfg.environmentFiles;
      path = [ pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = cfg.user;
        Group = cfg.group;
        LoadCredential = envCredentials;
        ExecStart = initScript;
      }
      // logFields "init" "wordpress-init"
      // harden {
        inherit readWritePaths;
        addressFamilies = [ "AF_UNIX" ];
      };
    };

    systemd.services.wordpress-migrate = mkIf cfg.migrate.enable {
      description = "Migrate the WordPress database for this build";
      wantedBy = [ "multi-user.target" ];
      before = [ "frankenphp.service" ];
      after = [ "wordpress-init.service" ] ++ optional dbLocal "mysql.service";
      requires = [ "wordpress-init.service" ] ++ optional dbLocal "mysql.service";
      path = phpPath;
      environment = serviceEnv;
      # A new build migrates on the switch that deploys it.
      restartTriggers = [ cfg.package ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = cfg.user;
        Group = cfg.group;
        ExecStart = "${flock} ${lockFile} ${getExe migrateScript}";
        OOMScoreAdjust = cfg.oomScoreAdjust.php;
      }
      // logFields "migrate" "wordpress-migrate"
      // harden { inherit readWritePaths; };
    };

    systemd.services.frankenphp = {
      description = "WordPress (FrankenPHP)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ] ++ optional cfg.migrate.enable "wordpress-migrate.service";
      # Ordered after migrate but not bound to it: a failed migrate is rolled
      # back, and WordPress serves new code on the old schema until the next
      # deploy migrates it.
      after = [
        "network-online.target"
        "wordpress-init.service"
      ]
      ++ optional cfg.migrate.enable "wordpress-migrate.service"
      ++ optional dbLocal "mysql.service";
      requires = [ "wordpress-init.service" ] ++ optional dbLocal "mysql.service";
      path = phpPath;
      environment = serviceEnv // {
        XDG_DATA_HOME = "${cfg.stateDir}/caddy/data";
        XDG_CONFIG_HOME = "${cfg.stateDir}/caddy/config";
      };
      restartTriggers = [ cfg.package ];
      serviceConfig = {
        User = cfg.user;
        Group = cfg.group;
        WorkingDirectory = cfg.stateDir;
        ExecStart = "${getExe frankenphp} run --config ${caddyfile} --adapter caddyfile";
        # A crash can leave the socket file behind, which blocks the rebind.
        ExecStartPre = optional viaSocket "${pkgs.coreutils}/bin/rm -f ${cfg.socketPath}";
        Restart = "always";
        RestartSec = "5";
        OOMScoreAdjust = cfg.oomScoreAdjust.php;
      }
      // logFields "web" "frankenphp"
      // harden (
        {
          readWritePaths = readWritePaths ++ sockets.directories;
        }
        # :80 and :443 without root. A socket binds no port and needs nothing.
        // lib.optionalAttrs (!viaSocket) {
          capabilities = [ "CAP_NET_BIND_SERVICE" ];
          overrides.AmbientCapabilities = [ "CAP_NET_BIND_SERVICE" ];
        }
      );
    };

    systemd.services.wordpress-cron = mkIf cfg.cron.enable {
      description = "WordPress scheduled tasks (wp-cron)";
      after = [ "frankenphp.service" ];
      requires = [ "wordpress-init.service" ];
      path = phpPath;
      environment = serviceEnv;
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = cfg.group;
        # Skips its turn (exit 0) while wordpress-migrate holds the lock.
        ExecStart = "${flock} --nonblock --conflict-exit-code 0 ${lockFile} ${cronScript}";
        OOMScoreAdjust = cfg.oomScoreAdjust.php;
      }
      // logFields "scheduler" "wordpress-cron"
      // harden { inherit readWritePaths; };
    };

    # A oneshot is never started while it runs, so runs cannot overlap.
    systemd.timers.wordpress-cron = mkIf cfg.cron.enable {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "1min";
        RandomizedDelaySec = cfg.cron.randomizedDelaySec;
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
        character-set-server = mkDefault "utf8mb4";
        collation-server = mkDefault "utf8mb4_unicode_ci";
        innodb_buffer_pool_size = mkDefault profile.innodbBufferPool;
      };
    };
    systemd.services.mysql.serviceConfig = mkIf dbLocal (
      logFields "db" null
      // {
        OOMScoreAdjust = cfg.oomScoreAdjust.database;
      }
    );

    networking.firewall = mkIf (cfg.openFirewall && !viaSocket) {
      allowedTCPPorts = [ 80 ] ++ optional (cfg.domain != "") 443;
      allowedUDPPorts = optional (cfg.domain != "") 443;
    };

    environment.systemPackages = [ wpWrapper ];

    # For inspection and the flake's logging check.
    system.build.wordpressCaddyfile = caddyfile;
    system.build.wordpressSettingsEnv = settingsFile;
  };
}
