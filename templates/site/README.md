# WordPress-on-Cloudflare site

A thin site repo: **payload + identity + pins**. All platform code (the
edge Worker, container image recipe, static-asset builder, deploy
pipeline) comes from [wordpress-nix](https://github.com/Avunu/wordpress)
at the revision pinned in `flake.lock`.

| What              | Where                                              |
| ----------------- | -------------------------------------------------- |
| Site code         | `wp-content/` (plugins, themes, mu-plugins) — managed by gitium from the admin backend |
| Site identity     | `wrangler.jsonc` (names, IDs, URLs, `WPCONF_*` config) |
| Platform version  | `flake.lock` — bump to update core/PHP/driver/worker atomically |
| Deploy pipeline   | `.github/workflows/deploy.yml` → calls the platform's reusable workflow |

## Setup

1. Replace every `CHANGEME` in `flake.nix` and `wrangler.jsonc`.
2. `wrangler d1 create <slug>` and `wrangler kv namespace create CACHE_KV`;
   paste the IDs into `wrangler.jsonc`.
3. Secrets: `wrangler secret put` × `WORDPRESS_SALTS`, `CACHE_PURGE_SECRET`,
   `D1_PROXY_TOKEN` (+ `WPCONF_S3_*`, `WPCONF_CLOUDFLARE_EMAIL_API_TOKEN`
   as needed).
4. GitHub: secret `CLOUDFLARE_API_TOKEN`, `CACHE_PURGE_SECRET`; vars
   `CLOUDFLARE_ACCOUNT_ID`, `PURGE_URL`.
5. Push to `main` — CI builds and deploys.

## Local development

`flake.nix` enables wordpress-nix's devenv module, so the site runs locally on
the platform stack — the pinned core, PHP 8.5 with the driver's native
extensions, FrankenPHP, the WordPress SQLite Anywhere drop-in, the platform
mu-plugins — over this repo's `wp-content/`:

```sh
direnv allow                     # or: nix develop --impure
devenv up                        # FrankenPHP (+ Mailpit) → http://127.0.0.1:<port>
wp-import dump.sql               # restore-core-keys → mysql-to-sqlite → the dev database
wp-admin-user                    # a local administrator (prints the password)
wp plugin list                   # wp-cli against the dev site
```

The port is hashed from `siteName`, so every clone gets the same one. The
database is a SQLite file under `.devenv/state/` by default (no server);
`database.type = "turso"` runs a local `tursodb` and reads through the
embedded replica exactly as production does, and `"mysql"` gives MariaDB.
Secrets the plugins need (`S3_KEY`, `JWT_AUTH_CLIENT_SECRET`, …) go in a
`.env` file, loaded by `devenv shell`; `configExtra` holds only the
non-secret constants, shared with the NixOS module. All outgoing mail is
caught by Mailpit.

The Cloudflare pieces still build from the same flake:

```sh
nix build .#worker -o .worker    # the edge worker bundle
nix build .#static-assets -o assets && rm -rf public && cp -rL assets public
wrangler dev                     # Worker + container + local D1
```

Rollback: run the `deploy` workflow manually with a previous commit as
`ref`. (Code rolls back; D1 data does not — use D1 Time Travel for data.)
