# WordPress site

A thin site repo: **payload + identity + pins**. All platform code (the container image recipe, the dev shell, the NixOS module) comes from [wordpress-nix](https://github.com/Avunu/wordpress-nix) at the revision pinned in `flake.lock`.

| What | Where |
| --- | --- |
| Site code | wp-content/ (plugins, themes, mu-plugins) — managed by gitium from the admin backend |
| Site identity | flake.nix (siteName, siteConfig, WPCONF_* config) |
| Platform version | flake.lock — bump to update core/PHP/driver atomically |
| Publish workflow | .github/workflows/publish.yml → calls the platform's reusable workflow |

## Setup

1.  Replace every `CHANGEME` in `flake.nix`.
2.  Set the repository variable `CLUSTER_REPO` (the repo holding this site's `apps/<slug>.json`) and the secret `CLUSTER_DISPATCH_TOKEN`.
3.  Set `slug` in `.github/workflows/publish.yml` to match that file.
4.  Push to `main`.

## Local development

`flake.nix` enables wordpress-nix's devenv module, so the site runs locally on the platform stack — the pinned core, PHP 8.5 with the driver's native extensions, FrankenPHP, the WordPress SQLite Anywhere drop-in, the platform mu-plugins — over this repo's `wp-content/`:

```sh
direnv allow                     # or: nix develop --impure
devenv up                        # FrankenPHP (+ Mailpit) → http://127.0.0.1:<port>
wp-import dump.sql               # restore-core-keys → mysql-to-sqlite → the dev database
wp-admin-user                    # a local administrator (prints the password)
wp plugin list                   # wp-cli against the dev site
```

The port is hashed from `siteName`, so every clone gets the same one. The database is a SQLite file under `.devenv/state/` by default (no server); `database.type = "turso"` runs a local `tursodb` and reads through the embedded replica exactly as production does, and `"mysql"` gives MariaDB. `configExtra` holds only the non-secret constants, shared with the NixOS module. Secrets are optional in the dev shell — mail is caught by Mailpit and media is served read-only from `S3_PUBLIC_URL` — but any you do want (`S3_KEY` and `S3_SECRET` to upload, `JWT_AUTH_CLIENT_SECRET` to exercise the provider) go in a gitignored `.env`, which `.envrc` loads and `wordpress-nix.environmentConstants` defines into wp-config.php.
