# The NixOS module (v3)

`nixosModules.default` (`modules/nixos.nix`) runs one Bedrock site, built by
`lib.mkBedrockSite` ([the Bedrock build](bedrock-build.md)), read-only, with
its MariaDB, its migrate-on-deploy and its cron.

```nix
{
  imports = [ inputs.wordpress-nix.nixosModules.default ];

  services.wordpress-nix = {
    enable = true;
    package = inputs.site-acme.packages.x86_64-linux.default;
    profile = "brochure";                       # or "woocommerce"
    socketPath = "/run/wordpress/wp.sock";      # or domain + acmeEmail
    environmentFiles = [ "/run/secrets/env" ];  # the salts, S3_KEY, S3_SECRET, ...
    settings = {
      WP_HOME = "https://acme.example";
      S3_BUCKET = "wp-acme-media";
      S3_ENDPOINT = "https://<account>.r2.cloudflarestorage.com";
      S3_PUBLIC_URL = "https://media.acme.example";
    };
  };
}
```

## What is writable

The document root is `${package}/web`, an immutable store path, and Bedrock's
own `DISALLOW_FILE_MODS` is on: no plugin, theme or core change happens in
place. A change is a commit to the site's repository. What the site writes goes
to one of three places:

| Path | What | Served |
|---|---|---|
| `uploadsDir` (`/var/lib/wordpress/uploads`) | What plugins write that is not an attachment: builder CSS, fonts, form uploads, private downloads. `web/app/uploads` links here | `file_server` only, never PHP; `denyUploadPaths` refused (403) |
| `stateDir` (`/var/lib/wordpress`) | Caddy's data, `HOME`, wp-cli's cache, the migrate snapshots | Never |
| `/tmp` | Each unit's own `PrivateTmp` (`TMPDIR`, `get_temp_dir()`) | Never |

Attachments go to the media bucket through wp-cloud-files.

## Configuration

Bedrock reads `${package}/.env`, a link to `/run/wordpress/bedrock.env`.
`wordpress-init` writes that file (0400, the service user's) from:

- `settings`: non-secret, rendered as a `.env` (strings single-quoted,
  booleans and integers bare, `null` left out). It is a Nix value and lands in
  the store. Its defaults: `WP_ENV=production`, `WP_HOME` (from `domain`, else
  `http://localhost`), `WP_SITEURL=${WP_HOME}/wp`, the `DB_*` names for the
  local socket with an empty password, `DISABLE_WP_CRON=true`,
  `WP_TEMP_DIR=TMPDIR=/tmp`, `WC_LOG_HANDLER=WC_Log_Handler_DB`,
  `WP_MAX_MEMORY_LIMIT` (the profile's), and the package's production-only and
  development-only plugin lists.
- `environmentFiles`: `KEY='value'` files appended after the settings, read
  through systemd's `LoadCredential` (so they may be root's 0400 files), never
  copied to the store. The salts, `S3_KEY`/`S3_SECRET`, mail tokens. A file
  rewritten in place (a rotated agenix secret keeps its path) is noticed by
  `wordpress-env.path`, which restarts `wordpress-init`, and with it
  `frankenphp` and `wordpress-migrate`.

`config/application.php` is never edited. What Bedrock does not define comes
from the environment through `mu-plugins/platform-environment.php`.

## Profiles

| `profile` | `threads` (FrankenPHP) | `memoryLimit` | `WP_MAX_MEMORY_LIMIT` | InnoDB pool | fits |
|---|---|---|---|---|---|
| `brochure` | 3 | 192M | 384M | 256M | 1280 MiB |
| `woocommerce` | 4 | 384M | 768M | 384M | 2560 MiB |

`threads` and `memoryLimit` are options of their own (defaulting to the
profile's), the InnoDB pool is a `mkDefault`, and `WP_MAX_MEMORY_LIMIT` a
setting. Under memory pressure the kernel kills PHP, never the database:
`oomScoreAdjust.database` (-500, mysql) and `oomScoreAdjust.php` (+300,
frankenphp, migrate, cron).

## Units

| Unit | What |
|---|---|
| `wordpress-init` | Writes `bedrock.env`; makes `uploadsDir` |
| `wordpress-env.path` | Restarts `wordpress-init` when an `environmentFiles` entry changes |
| `mysql` | `database.createLocally` (the default): unix_socket auth, so no password; `skip-networking`, so nothing on TCP |
| `wordpress-migrate` | Before `frankenphp`, on every new build; below |
| `frankenphp` | Caddy and PHP. `socketPath`, or `:80` (and `:443` with `domain`) |
| `wordpress-cron.timer` | Every minute, `wp cron event run --due-now`; `cron.randomizedDelaySec` spreads sites apart |

Every unit but mysql runs as the service user under python-devops-nix's
sandboxing baseline (`lib.nixos.hardening`, `ProtectSystem=strict`, with the
`jit` profile: opcache's JIT and PCRE's both need W+X memory), with
`ReadWritePaths = [ stateDir ]` and its own `PrivateTmp`. The journal fields are
python-devops-nix's (`APP_SERVICE` web, init, migrate, scheduler, db;
`APP_SITE` from `logging.site`), and each unit's `SyslogIdentifier` is its name.

### Migrate on deploy

`wordpress-migrate` is python-devops-nix's migrate flow
(`lib.nixos.migrate.script`) with WordPress's commands, as frappe-nix's is with
Frappe's:

1. **check**: finish a rollback a restart interrupted; skip when this build
   already migrated (`stateDir/.wordpress-migrate-build`), and when the database
   has no tables or no installed WordPress (the first boot before
   `site import`), so an empty site starts cleanly. Only `wp core is-installed`'s
   own "not installed" (exit 1) skips; any other failure (a PHP fatal in the new
   build) fails the unit;
2. **snapshot**: `mysqldump | gzip` into `stateDir/snapshots` (0700, each 0600),
   verified by mysqldump's closing line before anything relies on it;
3. **migrate**: `wp core update-db`, `wp wc update` when WooCommerce is active,
   `wp cache flush`;
4. on failure, **restore** the snapshot. The site keeps serving the new build
   on the restored schema, which WordPress supports (wp-admin asks for the
   database update); the unit fails, and the journal says so at `err`.

Migrate and cron share a lock (`/run/wordpress/maintenance.lock`): cron skips
its turn while a migrate runs, so it never runs on a half-migrated database.

## Routes

| Request | Answer |
|---|---|
| `/wp-content/uploads/<localUploadPrefixes or denyUploadPaths>/…` | 301 to `/app/uploads/…` |
| `/wp-content/uploads/…` (anything else) | 301 to `settings.S3_PUBLIC_URL/…` (to `/app/uploads/…` without a bucket) |
| `/wp-admin…`, `/wp-login.php` | 301 / 308 to `/wp/…` |
| `/wp-content/{plugins,themes}/…` | served as `/app/{plugins,themes}/…` |
| `/app/uploads/<denyUploadPaths>/…` | 403 |
| `/app/uploads/…*.php`, `.phtml`, `.phar`, `.cgi`, `.sh`, … | 403 |
| `/app/uploads/…` | the file, never PHP |
| `/wp/wp-includes/…*.php` (but TinyMCE's loader) | 404 |
| `/xmlrpc.php`, `/wp/xmlrpc.php` | 403 |
| `/wp-cron.php`, `/wp-config.php`, `/.env*` | 404 |

Over `socketPath` there is no peer address and no TLS: PHP takes the client's
address from `CF-Connecting-IP` and the scheme from `X-Forwarded-Proto` (a
`auto_prepend_file`), as only the tunnel's connector reaches the socket.

## The platform mu-plugin

`mu-plugins/platform-environment.php`, injected by `mkBedrockSite`:

- **Constants bridge:** `DISABLE_WP_CRON`, `WC_LOG_HANDLER` and
  `S3_DIRECT_UPLOADS` from the environment when set and not defined;
  `WP_MAX_MEMORY_LIMIT` through the `admin_memory_limit` and
  `image_memory_limit` filters (core defines the constant before plugins load).
- **Plugins kept to their environment:** `WORDPRESS_NIX_PRODUCTION_ONLY_PLUGINS`
  are dropped from `active_plugins` outside production,
  `WORDPRESS_NIX_DEVELOPMENT_ONLY_PLUGINS` outside development, as the option
  is read. When it is written (activating or deactivating any plugin), their
  stored entries are put back (`pre_update_option_active_plugins`), so the
  database keeps each plugin's state.
- **No update checks** while `DISALLOW_FILE_MODS`: the update transients are
  answered locally, core's checks are unhooked (`wp_version_check`,
  `wp_update_plugins`, `wp_update_themes`: their cron events, the `load-*`
  screens, `admin_init`, their scheduling), a check started elsewhere (`wp
  plugin list`) is refused at `pre_http_request` before it reaches
  api.wordpress.org, and Site Health drops `background_updates`,
  `plugin_theme_auto_updates`, `update_temp_backup_writable` and
  `available_updates_disk_space`.
- **Two-factor for administrators** (production): every user who can
  `manage_options` has the Email provider enabled, through the two-factor
  plugin's `two_factor_enabled_providers_for_user` filter. Verified against
  two-factor 0.10.0: the filter is applied with the enabled list and the user
  ID, `two_factor_primary_provider_for_user` and `two_factor_providers` exist
  as well, and the Email provider is available to every user.
- **PDF temp files in /tmp:** dk-pdf's mPDF `tempDir` through
  `dkpdf_mpdf_temp_dir` (checked against dk-pdf 2.3.1). give-pdf-receipts 3.3.0
  needs nothing: its TCPDF caches in `sys_get_temp_dir()`, and its export
  directory is under uploads.
- **Application passwords** off unless `WORDPRESS_NIX_APPLICATION_PASSWORDS=true`.
- **No executable uploads:** `wp_handle_upload_prefilter` refuses a name with an
  executable extension anywhere in it.
- **No mail outside production** (`pre_wp_mail`), unless
  `WORDPRESS_NIX_MAIL_CATCHER=true` (the dev shell's Mailpit).
- **Previews:** outside production, `WORDPRESS_NIX_HOME_FROM_REQUEST=true` makes
  home and siteurl follow the request's host.
- **Path-scoped Access:** with `WORDPRESS_NIX_ACCESS_ADMIN_PATHS=true`, an admin
  or login request without Cloudflare Access's assertion header is refused
  (`admin-ajax.php`, `admin-post.php`, `load-styles.php` and `load-scripts.php`
  stay open).

## Operating it

`wp` is on the system `PATH`, pointed at the package. Run it as the service
user, whose OS user the database authenticates and who alone reads
`bedrock.env`:

```sh
runuser -u wordpress -- wp option get home
```

## From v2

3.0.0 removed the split plane (`plane.*`), gitium, the SQLite and Turso
backends (`database.type`, `database.turso.*`), the `state`, `git` and
`managed` source modes (`source.*`), the generated `wp-config.php`
(`saltsFile`, `tablePrefix`, `configExtra`), `muPlugins`, `debug` and the
external database options. Setting any of them is an evaluation error that
points here. The OCI images, the `site` template, `wordpress-init`'s bash
bootstrapper and the `site-publish` workflow are gone too.
