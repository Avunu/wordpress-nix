# The logging contract and the Caddyfile of services.wordpress-nix, checked
# without a VM.
#
# Run: nix build .#checks.<system>.logging
#
# The journald fields are asserted at evaluation time; the generated
# Caddyfiles are then adapted to JSON by FrankenPHP (nixpkgs' stock build, so
# no custom PHP is compiled) to prove they parse, that the access logger is
# wired up exactly when `logging.accessLog` is on, and that the routes of the
# v3 plan's §7.3 are there.
{
  pkgs,
  nixosSystem,
  wordpressModule,
  # A lib.mkBedrockSite package (the offline fixture).
  site,
}:
let
  inherit (pkgs) lib;

  eval =
    settings:
    (nixosSystem {
      modules = [
        wordpressModule
        {
          nixpkgs.pkgs = pkgs;
          boot.isContainer = true;
          system.stateVersion = "25.11";
          services.wordpress-nix = {
            enable = true;
            package = site;
          };
        }
        { services.wordpress-nix = settings; }
      ];
    }).config;

  # Access log on (the default), APP_SITE from the domain, a media bucket.
  web = eval {
    domain = "blog.example.com";
    acmeEmail = "ops@example.com";
    settings.S3_PUBLIC_URL = "https://media.example.com";
  };
  # Access log off, explicit site name, over a socket, no bucket.
  quiet = eval {
    socketPath = "/run/wordpress/wp.sock";
    logging = {
      site = "quiet-site";
      accessLog = false;
    };
  };

  fields = config: unit: {
    inherit (config.systemd.services.${unit}.serviceConfig) LogExtraFields;
    SyslogIdentifier = config.systemd.services.${unit}.serviceConfig.SyslogIdentifier or null;
  };
  expect =
    config: unit: identifier: role: site:
    let
      actual = fields config unit;
      wanted = {
        SyslogIdentifier = identifier;
        LogExtraFields = [
          "APP_SERVICE=${role}"
          "APP_SITE=${site}"
        ];
      };
    in
    lib.assertMsg (
      actual == wanted
    ) "${unit}: expected ${builtins.toJSON wanted}, got ${builtins.toJSON actual}";

  fieldChecks = [
    (expect web "frankenphp" "frankenphp" "web" "blog.example.com")
    (expect web "wordpress-init" "wordpress-init" "init" "blog.example.com")
    (expect web "wordpress-migrate" "wordpress-migrate" "migrate" "blog.example.com")
    (expect web "wordpress-cron" "wordpress-cron" "scheduler" "blog.example.com")
    (expect web "mysql" null "db" "blog.example.com")
    (expect quiet "frankenphp" "frankenphp" "web" "quiet-site")
  ];
in
assert lib.all lib.id fieldChecks;
pkgs.runCommand "wordpress-nix-logging"
  {
    nativeBuildInputs = [
      pkgs.frankenphp
      pkgs.jq
    ];
    web = web.system.build.wordpressCaddyfile;
    quiet = quiet.system.build.wordpressCaddyfile;
    settingsEnv = web.system.build.wordpressSettingsEnv;
  }
  ''
    export HOME=$TMPDIR XDG_DATA_HOME=$TMPDIR XDG_CONFIG_HOME=$TMPDIR
    fail() { echo "FAIL: $*" >&2; exit 1; }

    adapt() { frankenphp adapt --config "$1" --adapter caddyfile; }
    adapt "$web" > web.json
    adapt "$quiet" > quiet.json

    # Both: the default logger writes JSON to stderr.
    for f in web.json quiet.json; do
      jq -e '.logging.logs.default | .writer.output == "stderr" and .encoder.format == "json"' "$f" >/dev/null \
        || fail "$f: default logger is not JSON on stderr"
    done

    # Access log on: a JSON-on-stderr logger takes the http.log.access entries,
    # and the server routes its requests to it.
    jq -e '
      [.logging.logs[] | select((.include // []) | any(startswith("http.log.access")))
        | .writer.output == "stderr" and .encoder.format == "json"] == [true]
      and ([.apps.http.servers[] | has("logs")] | all)
    ' web.json >/dev/null || { cat web.json >&2; fail "web: access log not configured"; }

    # Access log off: no access logger and no server-level logs.
    jq -e '
      ([.logging.logs[] | select((.include // []) | any(startswith("http.log.access")))] | length == 0)
      and ([.apps.http.servers[] | has("logs")] | any | not)
    ' quiet.json >/dev/null || { cat quiet.json >&2; fail "quiet: access log unexpectedly on"; }

    # The routes: old uploads to the bucket or to /app/uploads, nothing under
    # uploads through PHP, the socket bound with its mode.
    grep -q 'https://media.example.com/{re.lm.1}' "$web" || fail "web: uploads do not move to the media bucket"
    grep -q '/app/uploads/{re.lm.1}' "$quiet" || fail "quiet: uploads without a bucket do not stay local"
    grep -q 'bind unix//run/wordpress/wp.sock|0660' "$quiet" || fail "quiet: socket not bound"
    jq -e '[.. | objects | select(.handler? == "file_server")] | length > 0' web.json >/dev/null \
      || fail "web: no file_server for uploads"

    # Bedrock's environment: the defaults, quoted for Dotenv.
    for line in "WP_ENV='production'" "WP_HOME='https://blog.example.com'" \
                "WP_SITEURL='https://blog.example.com/wp'" "DB_PASSWORD='''" \
                "DB_HOST='localhost:/run/mysqld/mysqld.sock'" "DISABLE_WP_CRON=true" \
                "WP_MAX_MEMORY_LIMIT='384M'" "WORDPRESS_NIX_PRODUCTION_ONLY_PLUGINS='fixture-plugin'"; do
      grep -qxF "$line" "$settingsEnv" || fail "settings: missing $line"
    done

    touch $out
  ''
