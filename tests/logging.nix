# Logging contract for services.wordpress-nix, checked without a VM.
#
# Run: nix build .#checks.<system>.logging
#
# The journald fields are asserted at evaluation time; the generated
# Caddyfiles are then adapted to JSON by FrankenPHP (nixpkgs' stock build, so
# no custom PHP is compiled) to prove they parse and that the access logger
# is wired up exactly when `logging.accessLog` is on.
{
  pkgs,
  nixosSystem,
  wordpressModule,
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
            source = {
              type = "git";
              path = "${pkgs.wordpress}/share/wordpress";
            };
          };
        }
        { services.wordpress-nix = settings; }
      ];
    }).config;

  # Access log on (the default), local MariaDB, APP_SITE from the domain.
  web = eval {
    domain = "blog.example.com";
    acmeEmail = "ops@example.com";
    database.createLocally = true;
  };
  # Access log off, explicit site name.
  quiet = eval {
    logging = {
      site = "quiet-site";
      accessLog = false;
    };
  };
  # The snapshot publisher's unit (evaluated only, never built).
  turso = eval {
    php = pkgs.php85;
    database = {
      type = "turso";
      turso = {
        url = "http://127.0.0.1:8080";
        snapshotPath = "/var/lib/wordpress/database/snapshot.db";
      };
    };
  };

  fields = config: unit: {
    inherit (config.systemd.services.${unit}.serviceConfig) SyslogIdentifier LogExtraFields;
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
    (expect web "wordpress" "wordpress" "web" "blog.example.com")
    (expect web "wordpress-init" "wordpress-init" "init" "blog.example.com")
    (expect web "wordpress-cron" "wordpress-cron" "cron" "blog.example.com")
    (expect web "mysql" "mysql" "db" "blog.example.com")
    (expect quiet "wordpress" "wordpress" "web" "quiet-site")
    (expect turso "wordpress-turso-publisher" "wordpress-turso-publisher" "publisher" "wordpress")
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
  }
  ''
    export HOME=$TMPDIR XDG_DATA_HOME=$TMPDIR XDG_CONFIG_HOME=$TMPDIR

    adapt() { frankenphp adapt --config "$1" --adapter caddyfile; }
    adapt "$web" > web.json
    adapt "$quiet" > quiet.json

    # Both: the default logger writes JSON to stderr.
    for f in web.json quiet.json; do
      jq -e '.logging.logs.default | .writer.output == "stderr" and .encoder.format == "json"' "$f" >/dev/null \
        || { echo "$f: default logger is not JSON on stderr" >&2; exit 1; }
    done

    # Access log on: a JSON-on-stderr logger takes the http.log.access entries,
    # and the server routes its requests to it.
    jq -e '
      [.logging.logs[] | select((.include // []) | any(startswith("http.log.access")))
        | .writer.output == "stderr" and .encoder.format == "json"] == [true]
      and ([.apps.http.servers[] | has("logs")] | all)
    ' web.json >/dev/null || { echo "web: access log not configured" >&2; cat web.json >&2; exit 1; }

    # Access log off: no access logger and no server-level logs.
    jq -e '
      ([.logging.logs[] | select((.include // []) | any(startswith("http.log.access")))] | length == 0)
      and ([.apps.http.servers[] | has("logs")] | any | not)
    ' quiet.json >/dev/null || { echo "quiet: access log unexpectedly on" >&2; cat quiet.json >&2; exit 1; }

    touch $out
  ''
