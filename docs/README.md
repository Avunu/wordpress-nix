---
title: wordpress-nix
description: "Read-only WordPress sites built with Nix from a stock Bedrock repository, served by FrankenPHP on NixOS with migrate-on-deploy."
nav_title: Overview
order: 0
tags: [wordpress, nix, overview]
updated: 2026-11-24
---

# wordpress-nix

WordPress sites built with Nix and served read-only: each site is a stock
[Roots Bedrock](https://roots.io/bedrock/) repository whose `composer.lock` Nix
turns into an immutable tree, which a NixOS module serves with
[FrankenPHP](https://frankenphp.dev/), its own MariaDB, migrate-on-deploy and a
cron timer.

- **The build.** `lib.mkBedrockSite` and the site flake module
  (`flakeModules.default`, `wordpress-nix.site`): `php.mkComposerVendor` for the
  vendor tree, `php.buildComposerProject2` for the package, `nix run .#relock`
  after a lock change. See [the Bedrock build](reference/bedrock-build.md).
- **The runtime.** `nixosModules.default` (`services.wordpress-nix`): one site,
  read-only, configured through Bedrock's environment. See
  [the NixOS module](reference/nixos-module.md).
- **The dev shell.** The same flake module's devenv shell: the platform's PHP
  and FrankenPHP over the checkout, MariaDB, Mailpit, `wp restore`.
- **A new site.** `nix flake init -t github:Avunu/wordpress` lays down
  `templates/bedrock-site`; then `composer update && nix run .#relock`. Or
  `nix run github:Avunu/wordpress` ([the scaffolder](reference/scaffolder.md)).
  See [Getting started](getting-started.md).
- **A legacy site.** `wordpress-nix legacy pull`, then
  `nix run github:Avunu/wordpress -- --migrate` on the pulled copy, then the
  data and edge commands: see [Convert a legacy WordPress site](scaffolding/migrate.md)
  and [the CLI](reference/wordpress-nix-cli.md).
- **Site CI and updates.** `wordpress-nix` (py/wordpress_nix_tools, on
  python-devops-nix's standards-core) renders each site's managed files, and
  the reusable `site-ci.yml`, `site-deps.yml` and `site-audit.yml` gate, relock
  and audit it. See [Site CI](production/site-ci.md) and
  [Releases and the cluster](production/releases.md).

## Features

- PHP 8.3, 8.4 and 8.5, built ZTS with JIT and optional clang/LTO flags
  (`lib/php.nix`, `lib/frankenphp.nix`); the site's `wordpress-nix.site.php`
  picks one.
- Core, wordpress.org plugins and themes from Composer (WP Packages); premium
  and custom code committed under `web/app/`.
- A read-only document root: Bedrock's `DISALLOW_FILE_MODS`, an immutable store
  path, one writable uploads directory that never runs PHP.
- Migrate on deploy behind a verified snapshot, rolled back on failure
  (python-devops-nix's migrate flow, as frappe-nix and odoo-nix use).
- Platform mu-plugins (`mu-plugins/platform-*.php`, injected into every build):
  the runtime posture (`platform-environment.php`: two-factor for
  administrators, no application passwords, no update checks, no executable
  uploads, no mail outside production, plugins kept to their environment), a
  60-day nonce lifetime, and user-enumeration hardening (403 on `?author=<id>`
  probes, the users REST routes require authentication, no users sitemap, no
  oEmbed author URL).
- Migration tools: `audit-mysql-dump` (refuses or strips triggers, routines and
  events in an untrusted dump), `restore-core-keys` (core-table keys back to
  `wp_get_db_schema()`), `verify-wp-checksums` (a built tree against
  wordpress.org).

## Repo layout

```
flake.nix                       # lib, nixosModules, flakeModules, templates, packages, checks
lib/mk-bedrock-site.nix         # the site build
lib/relock.nix                  # nix run .#relock
lib/{php,frankenphp}.nix        # the PHP and FrankenPHP builds
modules/nixos.nix               # services.wordpress-nix
modules/site.nix                # wordpress-nix.site: a site flake's packages, apps, checks
modules/devenv.nix              # a site flake's dev shell
mu-plugins/                     # injected into every build
lib/site-smoke.nix              # a site's checks.smoke (ci / smoke)
templates/bedrock-site/         # nix flake init -t github:Avunu/wordpress
lib/tools.nix                   # wordpress-init and wordpress-nix (py/wordpress_nix_tools)
py/wordpress_nix_tools/         # the scaffolder, the conversion commands, the site standards
data/                           # presets, drop list, runtime writers, premium slugs, ...
tests/                          # the build, the template, the module (VM), logging
.github/workflows/site-*.yml    # the reusable workflows site repositories call
.github/workflows/rollout.yml   # a release to every site repository
tools/                          # audit-mysql-dump, restore-core-keys, verify-wp-checksums
```

## Logging

Everything goes to journald. Every unit the module defines, and the local
`mysql`, carries python-devops-nix's fields, shared with frappe-nix and
odoo-nix:

| Unit | SYSLOG_IDENTIFIER | APP_SERVICE |
| --- | --- | --- |
| frankenphp | frankenphp | web |
| wordpress-init | wordpress-init | init |
| wordpress-migrate | wordpress-migrate | migrate |
| wordpress-cron | wordpress-cron | scheduler |
| mysql | mysql | db |

`APP_SITE` is `logging.site`: the `domain`, or `wordpress` when none is set.

- Caddy and FrankenPHP log JSON lines to stderr, each with a `level`.
- `logging.accessLog` (on by default) adds one JSON line per request, from
  Caddy's `http.log.access` logger, which redacts `Cookie` and `Authorization`.
- PHP errors and `error_log()` go through FrankenPHP into the same JSON logger,
  at a level mapped from their severity (the module unsets `error_log`). wp-cli,
  under the migrate and cron units, writes its errors to stderr.

## Checks

`nix flake check` runs the Bedrock build over an offline fixture, the template
against the pinned Bedrock tag, `verify-wp-checksums` against a local stand-in
for wordpress.org, the dump tools, the logging contract, wordpress-nix-tools'
suites (`tools`), the template against what `wordpress-nix sync` renders
(`site-template-sync`), and two NixOS VM tests that need KVM and fetch the
template's Composer packages: the module (`checks.<system>.module`) and a
site's smoke test over the template (`site-smoke`). `selftest-scaffold.yml`
covers what needs the network: a new site, a Composer change, the relock and
the pipeline's checks on it.

## License

MIT
