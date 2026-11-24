---
title: Convert a legacy WordPress site
description: "Pull a classic WordPress install off its legacy host, read-only, and convert it into a stock Bedrock wordpress-nix site, then provision, import, verify and cut over."
order: 2
tags: [scaffolding, migrate, bedrock, conversion, cutover]
updated: 2026-11-21
---

A conversion has two halves, as frappe-nix's does:

- **The tree.** `nix run github:Avunu/wordpress -- --migrate` (`wordpress-init`) turns a *pulled copy* of a classic install into a stock Bedrock repository, in place. It is a reconciler: it probes, adds what is missing, moves what it replaces into `.wordpress-nix-backup/`, never deletes, and stages without committing. Running it again on the result is a no-op.
- **The data and the edge.** Idempotent `wordpress-nix` commands (see [the CLI reference](../reference/wordpress-nix-cli.md)) pull, audit, provision, import, verify and cut over. None keeps a state file: each probes what exists (Cloudflare, GitHub, the cluster) and only the records in `.wordpress-nix-backup/` stay local.

## Never write to the legacy server

The legacy hosts are presumed compromised, and a conversion must be repeatable up to cutover day. So **no wordpress-nix command writes to a legacy host**:

- `legacy survey` reads `wp-config.php`'s constants with the host's own `php`, runs `SELECT`s, and walks `wp-content`.
- `legacy pull` rsyncs code *from* the host, and runs `mysqldump` with the credentials handed over a file descriptor: no file, no option, no plugin is written there.
- `media offload` reads uploads over sftp. `verify` runs `SELECT`s.
- The scaffolder never runs on the server. It converts the pulled copy, on your machine.

The exceptions are a human's, done through the xCloud panel and recorded in the cutover record's issue (D11):

1. the v2 pilot site in M0: putting the account-owned R2 key into its live wp-config, and remediating that site;
2. at T−7 days, every site: turning off WordPress and xCloud auto-updates and plugin installs, so the code does not drift before cutover;
3. on cutover day: the admin freeze, the Woo maintenance mode and the "submissions paused" notice.

## The runbook

```bash
# T−7: the tree
wordpress-nix legacy pull legacy@host --path /var/www/site --into ~/sites/site --age-recipient ~/.ssh/id_ed25519.pub
wordpress-nix db audit ~/sites/site/.wordpress-nix-backup/dump.sql \
  --survey ~/sites/site/.wordpress-nix-backup/survey.json --strip --out ~/sites/site/.wordpress-nix-backup/import.sql
cd ~/sites/site
nix run github:Avunu/wordpress -- --migrate --dry-run     # read the plan
nix run github:Avunu/wordpress -- --migrate -y --commit   # convert, relock, commit
wordpress-nix repo adopt site                             # a fresh repository, or a pull request (D6)
wordpress-nix repo apply Avunu/site-site

# T−7: the edge and the data
wordpress-nix media provision site --domain example.org   # prints <slug>-cloud-files and <slug>-media-ro
wordpress-nix edge provision site --domain example.org    # prints <slug>-cf/cloudflared and the tunnel id
#   cluster-ovh-01: avc secrets provision; the app with stagingDomain; merge after the gate
wordpress-nix media offload site --from legacy@host --path /var/www/site --dump .wordpress-nix-backup/import.sql \
  --credentials cloud-files.json
wordpress-nix site import site --dump .wordpress-nix-backup/import.sql \
  --files .wordpress-nix-backup/private.tar.gz --home https://staging.example.org
wordpress-nix verify site --against legacy@host --path /var/www/site

# Cutover day (after the cluster PR that sets WP_HOME to the real domain)
wordpress-nix legacy survey legacy@host --path /var/www/site --compare .wordpress-nix-backup/survey.json
wordpress-nix legacy pull legacy@host --path /var/www/site --into . --db-only
wordpress-nix db audit .wordpress-nix-backup/dump.sql --survey .wordpress-nix-backup/survey.json \
  --strip --out .wordpress-nix-backup/import.sql
wordpress-nix media offload site --from legacy@host --path /var/www/site --dump .wordpress-nix-backup/import.sql
wordpress-nix site import site --dump .wordpress-nix-backup/import.sql \
  --files .wordpress-nix-backup/private.tar.gz --home https://example.org --replace
wordpress-nix verify site --against legacy@host --path /var/www/site --via-socket
wordpress-nix cutover site --domain example.org
wordpress-nix verify site --against legacy@host --path /var/www/site --delta-since '<the pull timestamp>'
```

Rolling back: `wordpress-nix cutover site --rollback` restores the DNS records, the Page, Cache and Transform rules and APO exactly as the snapshot recorded them, and purges. Then `verify --delta-since <flip-ts>` against v3 lists what to re-enter on legacy.

## What `--migrate` does

1. **Guards.** A directory inside another repository: exit 8 (unless `--force`). WordPress below 6.6: exit 5. A multisite: exit 6. No survey or dump to tell what is active (it looks in `.wordpress-nix-backup/` for `survey.json`, then `dump.sql`) and no `--keep-inactive`: exit 6. `$table_prefix` becomes `DB_PREFIX`.
2. **Classifies** every directory in `wp-content/{plugins,themes,mu-plugins}` and every drop-in:

   | Disposition | When | Becomes |
   |---|---|---|
   | composer | wordpress.org knows the slug, `Update URI` is theirs or empty, the author matches, the installed files match what wordpress.org published for that version, and WP Packages has the version | `wp-plugin/<slug>` / `wp-theme/<slug>` at the exact version |
   | avunu | the directory and its header are an Avunu Packagist package | `avunu/<slug>` |
   | commit | premium or custom: `data/premium-slugs.json`, a foreign `Update URI`, unknown to wordpress.org | `web/app/{plugins,themes,mu-plugins}/<slug>/`, whitelisted in `.gitignore` |
   | drop | `data/drop-plugins.json` (MainWP, core-rollback, wp-console, caches, host plugins, the jwt-auth fork, …), host mu-plugins, drop-ins, `*.bak`, empty directories, inactive code (an active child theme keeps its parent) | moved to the backup; active plugins listed in `dropPlugins` |
   | platform | `mu-plugins/platform-*.php` | ignored: the build injects its own |

   Flags: `modified` (the files differ from wordpress.org's: decide with `--accept-modified <slug>:replace|vendor`), `missing-code` (active in the database, absent on disk: `--drop-plugin` it) and `db-code` (code stored in the database: `--keep-plugin` or `--drop-plugin`; WPCodeBox's default commits `wpcodebox_functionality_plugin` and makes `wpcodebox2` development-only, D12) block. `collision`, `not-in-registry`, `delisted`, `writes-to-disk`, `mpdf-tempdir`, `php-floor`, `suspicious-code`, `salt-dependent` and `non-english-locale` are warnings, which `--strict` makes errors.
3. **Prints the plan**: the table, the runtime writers (§4.8), tracked credentials, and the existing repository's history. With an `origin` remote it runs `repo adopt --check` and prints what D6 decides (create, replace or pr), or why it could not ask GitHub. `--dry-run` stops here (exit 0); a blocked plan stops a real run (exit 1); declining stops it (exit 6).
4. **Runs the pipeline.** Check-then-act; the git index and the tree are the state.
   1. `.wordpress-nix-backup/` (gitignored, with a README, `provenance.json` and `plan.json`); `git init` if needed.
   2. Legacy core, `wp-config.php`, `*.sql`, uploads, caches, dropped items and drop-ins move into the backup. Other root files go there too and are listed: move into `web/` any the site serves.
   3. The template (stock Bedrock and the site flake) is installed without overwriting anything; `nix/vendor-hash` starts as `lib.fakeHash`; `.github/wordpress-nix.json` records the site.
   4. Committed code moves to `web/app/`; what is left of `wp-content` goes to the backup.
   5. `composer.json` is reconciled: core and every Composer disposition at its exact version, `avunu/*`, `wp-plugin/two-factor`, `config.platform.php`, WP Packages' `only` guard.
   6. The managed `.gitignore` block, then `git check-ignore` over what the build needs.
   7. `git add -A`, **before any Nix command**: a flake in a git repository sees only tracked files.
   8. Unless `--skip-lock`: `composer update --no-install --no-scripts`, `nix run .#relock`, `nix flake lock`. A failure is exit 1 with the failing command and "rerun": a `lib.fakeHash` would fail CI.
   9. With `--sync`: `wordpress-nix sync --write`.
   10. Asserts the project files are indexed and `nix/vendor-hash` is real. With `--commit`: `feat: convert to Bedrock (wordpress-nix)`.
   11. With `--commit` (and not `--no-loosen`): `wp-plugin/*` and `wp-theme/*` loosened to `^x.y`, relocked, `chore(deps): loosen plugin constraints`.
   12. The report, and the next steps.

An interrupted run resumes from `plan.json`; a resumed run moves only legacy core, `wp-config.php` and SQL files from the root, never the template it already installed. A run on a converted site reconciles: it adds what is missing and changes nothing else.

## Salts and credentials

Salts are regenerated by default (D8): production's come from the cluster's secrets. `--keep-salts` writes the legacy salts to `.wordpress-nix-backup/salts.env` (0600) for a salt-dependent site on a host not suspected of compromise; provision them by hand. `db audit --strip` removes application passwords, session tokens and MainWP's options; `site import` resets every administrator's and editor's password. Every credential a tracked `wp-config.php` or dump ever held is in the repository's history: rotate it, and let `repo adopt` start a fresh repository.
