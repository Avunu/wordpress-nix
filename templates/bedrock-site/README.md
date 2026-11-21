# changeme

A WordPress site on [wordpress-nix](https://github.com/Avunu/wordpress): stock
[Roots Bedrock](https://roots.io/bedrock/), built by Nix from `composer.lock`
and served read-only.

- wordpress.org plugins and themes, and core, are Composer packages
  (`wp-plugin/*`, `wp-theme/*` from WP Packages; `roots/wordpress`).
- Premium and custom code is committed under `web/app/{plugins,themes,mu-plugins}/<slug>/`,
  each directory whitelisted in the managed block of `.gitignore`.
- `nix/vendor-hash` is the hash of everything Composer installs. After any
  change to `composer.lock`, run `nix run .#relock` and commit both.

```sh
composer update --no-install          # or: composer require wp-plugin/<slug>
nix run .#relock                      # rewrites nix/vendor-hash
nix build                             # the site: result/web is the document root
nix flake check --no-pure-eval        # composer-validate, vendor-hash, build
nix run .#verify-wp-checksums         # the build against wordpress.org (network)
```

## Developing

The dev shell (direnv loads it through `.envrc`; by hand,
`nix develop --no-pure-eval`) runs the site the way production does, over this
checkout, with `WP_ENV=development` (file modifications allowed):

```sh
composer install                      # core, plugins and vendor/ into the tree
devenv up                             # FrankenPHP + MariaDB + Mailpit
wp restore                            # the newest dev-restore dump of the site
wp-admin-user                         # a local administrator
```

Secrets a plugin needs locally (`S3_KEY`, ...) go in `.env`, which the shell
writes once with fresh salts and git ignores.
