# WordPress FrankenPHP for Nix

This project provides two ways to deploy an optimized [FrankenPHP](https://frankenphp.dev/)
WordPress stack from a single, shared Nix codebase:

1. **OCI containers** — `nix build .#wordpress-php83` produces an image for ghcr.io etc.
2. **A NixOS module** — `services.wordpress-nix` runs WordPress directly on a NixOS host.

Both paths share the same optimized ZTS PHP build (`lib/php.nix`) and FrankenPHP
(`lib/frankenphp.nix`).

## Features

* PHP 8.2 / 8.3 / 8.4, selectable per deployment (`services.wordpress-nix.php`).
* [FrankenPHP](https://frankenphp.dev/) as the server; standalone Caddy ACME TLS on NixOS.
* Optimized builds with CPU-specific flags (auto-gated per architecture; opt-out via `phpOptimize`).
* Two site-source modes on NixOS:
  * **state** — mutable WordPress core in local state storage (flexible, server-specific;
    admin manages core/plugins/themes via the UI).
  * **git** — a read-only document root pulled from a flake input (source-managed).
* Automated container builds and pushes to ghcr.io.

## Repo layout

```
flake.nix                 # outputs: nixosModules.default, lib, packages, checks
lib/{php,frankenphp,wordpress}.nix   # shared builders
lib/php-extensions.nix    # the native wp_mysql_parser + wp_d1_client extensions (from the plugin flake)
modules/nixos.nix         # services.wordpress-nix
modules/containers.nix    # OCI image build (reuses lib/)
conf/{php.ini,Caddyfile,wp-config.php}
tests/module.nix          # NixOS VM test
```

## NixOS module

Add this flake as an input and import `nixosModules.default`.

### State-managed site (flexible, server-specific)

```nix
{
  imports = [ inputs.wordpress-nix.nixosModules.default ];

  services.wordpress-nix = {
    enable = true;
    php = pkgs.php84;              # per-deployment PHP version
    domain = "blog.example.com";  # Caddy gets a cert via ACME
    acmeEmail = "admin@example.com";
    source.type = "state";        # core lives in /var/lib/wordpress/www, mutable
    database.createLocally = true; # local MariaDB, passwordless unix_socket auth
  };
}
```

### Source-managed site (git)

The WordPress document root (a full webroot, or a Bedrock/Composer layout) is a flake
input; it is mounted read-only, with `wp-content/uploads` kept writable in state.

```nix
{
  # flake inputs:  mysite.url = "git+ssh://git@host/mysite";
  imports = [ inputs.wordpress-nix.nixosModules.default ];

  services.wordpress-nix = {
    enable = true;
    php = pkgs.php83;
    domain = "shop.example.com";
    acmeEmail = "admin@example.com";
    source = {
      type = "git";
      path = inputs.mysite;   # read-only document root
      # manageWpConfig = false;  # set if the repo ships its own wp-config.php
    };
    database = {
      createLocally = false;                 # external DB
      host = "db.internal";
      name = "shop";
      user = "shop";
      passwordFile = config.age.secrets.wp-db.path;  # injected via systemd LoadCredential
    };
  };
}
```

Notes:
* Leaving `domain = ""` binds `:80` only (put your own TLS in front).
* An external DB connects over TCP using `passwordFile`; a local DB uses passwordless
  `unix_socket` auth, so `database.user` must equal `user`.
* Secrets (DB password + salts) are written to `/var/lib/wordpress/wp-secrets.php`
  (0600) at activation and never enter the Nix store.
* Run wp-cli as the service user: `sudo -u wordpress wp ...`.
* In git mode UI-driven plugin/theme installs are disabled (`DISALLOW_FILE_MODS`) —
  manage them in the source.

### Database backends

`services.wordpress-nix.database.type` selects where the data lives.

| | |
|---|---|
| `mysql` | MariaDB/MySQL, local or external. The default. |
| `d1` | Cloudflare D1 through the site Worker's authenticated `/__d1` proxy. |
| `turso` | A Turso database over SQL-over-HTTP, optionally reading from a locally published snapshot. |

Both remote backends run the MySQL-on-SQLite driver in place of MySQL, through
the [WordPress SQLite Anywhere](https://github.com/Avunu/wordpress-sqlite-anywhere)
plugin (a flake input); the module installs its `wp-content/db.php` drop-in and
sets `DB_ENGINE` for you. The plugin requires PHP 8.5, so these modes need
`php = pkgs.php85` (an assertion says so).

#### Turso

Three shapes. `database.turso.embedded = true` is the one to reach for: the
plugin's `wp_turso` extension holds an embedded replica open inside FrankenPHP,
pulls the primary's changes into it every `pullIntervalMs` (and again at the
end of any request that wrote), and serves every read from it at local-SQLite
speed — wp-admin included, which on a WAN primary goes from ~1.5 s a page to
~40 ms. Writes go to the primary. No publisher process, no snapshot copy.

```nix
services.wordpress-nix = {
  enable = true;
  php = pkgs.php85;
  database.type = "turso";
  database.turso = {
    url = "libsql://site-org.turso.io";
    tokenFile = "/run/agenix/site-turso-token";
    embedded = true;
  };
};
```

The other two shapes are chosen by whether `database.turso.snapshotPath` is set.

**Without a snapshot**, every statement goes to the primary. This is what the
control plane wants — wp-admin and cron must read their own writes immediately.
Co-locate the primary: per-statement latency is what a query-heavy admin page
multiplies, and a local `tursodb --sync-server` answers in ~156 µs where a WAN
round trip would not.

```nix
services.wordpress-nix = {
  enable = true;
  php = pkgs.php85;
  database.type = "turso";
  database.turso.url = "http://127.0.0.1:8080";
};
```

**With a snapshot**, reads come from a local SQLite file and writes go to the
primary; the first write latches the rest of the request to the primary so it
reads its own writes. This is the public front end, where rendering a page never
touches the network — measured at 22 ms per page and ~41 requests/second per
vCPU, indistinguishable from reading the database file directly.

```nix
services.wordpress-nix = {
  enable = true;
  php = pkgs.php85;
  database.type = "turso";
  database.turso = {
    url = "libsql://site-org.turso.io";
    tokenFile = "/run/agenix/site-turso-token";
    snapshotPath = "/var/lib/wordpress/database/snapshot.db";
    publishIntervalSeconds = 10;
  };
};
```

Setting `snapshotPath` starts `wordpress-turso-publisher`, which keeps the
snapshot current and publishes the first one before WordPress starts. **Front-end
reads are behind the primary by up to `publishIntervalSeconds`** — a real
semantic change worth documenting per site, though it composes with page caching,
which already means the public site lags the database by a bounded amount.

The snapshot exists because a live Turso embedded replica cannot be read by
`pdo_sqlite` at all: Turso holds an exclusive lock on it for the life of its
connection and coordinates its WAL through a file SQLite knows nothing about. The
publisher owns the replica and hands PHP a plain file instead. Nothing else may
touch `database.turso.replicaPath`.

#### Migrating a MySQL site

Two tools, both flake packages, take a `mysqldump` to a populated Turso (or
D1) database:

```sh
nix run github:Avunu/wordpress#mysql-to-sqlite -- dump.sql site.sqlite
TURSO_AUTH_TOKEN=... nix run github:Avunu/wordpress#sqlite-to-turso -- site.sqlite libsql://site-org.turso.io
```

`mysql-to-sqlite` replays the dump through the MySQL-on-SQLite driver itself
(with the same native parser the site runs on — a 130 MB dump takes about a
minute), so the SQLite file carries the exact schema — and the driver's
emulated `INFORMATION_SCHEMA`, with MySQL column types intact — that the site
will use at runtime. It replays under the SQL mode mysqldump sets, so
`0000-00-00` dates and the rest of what was valid on the source load as they
were. Triggers, procedures, functions and events are not migrated and are
reported one by one: WordPress creates none, so **read every one the report
lists** — a trigger on `wp_comments` that inserts an administrator is a
well-known backdoor. `sqlite-to-turso` copies that file into the Turso primary over its
SQL-over-HTTP pipeline: tables, rows (as typed arguments, never SQL text),
indexes, triggers, views and `AUTOINCREMENT` counters, then verifies every
table's row count. It refuses a target that already has tables unless you
pass `--replace`. The token comes from `TURSO_AUTH_TOKEN` (or
`TURSO_AUTH_TOKEN_FILE`); it is never taken from the command line.

## Containers

The container path is unchanged: WordPress is downloaded at container start
(`WORDPRESS_SOURCE_URL`, default `wordpress.org/latest.zip`) and configured from
environment variables (see `.env.example` and `conf/wp-config.php`).

### Prerequisites

* Nix with flakes enabled
* Docker (for local testing and pushing)
* GitHub account (for pushing to ghcr.io)

## Local Development

### Building Images

To build images locally:

```bash
# Build all images
nix build

# Build a specific PHP version
nix build .#wordpress-php83
```

### Testing Locally

There’s a script you can use to test locally:

```bash
./scripts/test.sh
```

Visit `http://localhost:8080` in your browser to test.

### Pushing to ghcr.io



1. Create a `.env` file in the project root:

```
GITHUB_USERNAME=your_github_username
GITHUB_TOKEN=your_personal_access_token
```


2\. Run the build and push script:

```bash
./scripts/build-and-push.sh
```

## GitHub Actions

The included GitHub Actions workflow automatically builds and pushes images to ghcr.io on pushes to the main branch.

## Contributing

Contributions are welcome! Please submit pull requests with any improvements or bug fixes.

## License

MIT