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

```sh
nix develop                      # wrangler, node, etc.
nix build .#worker -o .worker    # the edge worker bundle
nix build .#static-assets -o assets && rm -rf public && cp -rL assets public
wrangler dev                     # Worker + container + local D1
```

Rollback: run the `deploy` workflow manually with a previous commit as
`ref`. (Code rolls back; D1 data does not — use D1 Time Travel for data.)
