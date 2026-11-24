---
title: Scaffolder
description: "The flags, modes and exit codes of wordpress-init, the program behind nix run github:Avunu/wordpress that creates, converts and reconciles Bedrock sites."
order: 4
tags: [reference, wordpress-init, cli, scaffolding]
updated: 2026-11-21
---

`nix run github:Avunu/wordpress` runs `wordpress-init`, the scaffolder. Pass flags after `--`:

```bash
nix run github:Avunu/wordpress -- --migrate --dry-run
```

It never prompts when stdin or stdout is not a terminal, so scripted runs must pass what they need (`-y` to convert).

```text
Usage: wordpress-init [options] [target-dir]
```

## Mode

The mode comes from the target directory (the current directory when it holds a WordPress install or a site):

| Directory | Mode |
|---|---|
| empty | `--init`: a new Bedrock site |
| Bedrock plus a wordpress-nix flake | reconcile (a no-op when nothing drifted) |
| classic WordPress (`wp-includes/version.php`, or plugins and themes under `wp-content/`) | migrate |
| a v2 wordpress-nix site (its flake names `Avunu/wordpress`, code under `wp-content/`) | migrate; the old flake goes to the backup |
| a foreign `flake.nix` | exit 4 (`--force` writes `flake.nix.wordpress-nix`) |
| multisite | exit 6 |

| Flag | Meaning |
|---|---|
| `--init` | Force scaffold mode. Needs an empty directory unless you add `--force`. |
| `--migrate` | Force migration mode. Also reconciles a wordpress-nix site. |
| `--sync` | Write the site's managed files (`wordpress-nix sync --write`). With `--migrate`, after the conversion. |
| `--check` | Report drift in them and change nothing (`wordpress-nix sync --check`). |
| `--force` | Relax the mode guard. It never overwrites an existing file. |
| `--dry-run` | Print the plan and exit without changing anything. |
| `-y`, `--yes` | Assume yes. Required to convert without a terminal. |

## Common

| Flag | Meaning |
|---|---|
| `--version <x.y.z>` | The WordPress core version to pin (`roots/wordpress`). Overrides detection. |
| `--name <slug>` | The site's slug. Defaults to the directory's name, without `site-`. |
| `--home-url <url>` | The site's home URL: the `--default-site` equivalent. Defaults to the survey's or the dump's `home`. |
| `--php <php83\|php84\|php85>` | The PHP the site builds with (`wordpress-nix.site.php`, `config.platform.php`). Default `php84`. |
| `--skip-lock` | Do not run `composer update`, `nix run .#relock` or `nix flake lock`. `nix/vendor-hash` stays `lib.fakeHash`. |
| `--strict` | Treat warnings as errors (exit 1). |
| `--commit[=<msg>]` | Commit the result instead of only staging it. |
| `-h`, `--help` | Show the help. |

## Migration only

| Flag | Meaning |
|---|---|
| `--survey <file>` | The legacy survey. Default: `.wordpress-nix-backup/survey.json`. |
| `--dump <file>` | The legacy dump, read for the active set when there is no survey. Default: `.wordpress-nix-backup/dump.sql`. |
| `--keep-plugin <slug>` | Keep a plugin the plan would drop, or resolve its `db-code` flag. Repeatable. |
| `--drop-plugin <slug>` | Drop a plugin the plan would keep, or resolve its `missing-code` flag. Repeatable. |
| `--keep-inactive` | Keep inactive plugins and themes; also allows a run with no survey or dump. |
| `--keep-salts` | Keep the legacy salts (written to `.wordpress-nix-backup/salts.env`). |
| `--accept-modified <slug>:<replace\|vendor>` | A wordpress.org copy with local changes: replace it with the Composer package, or commit the modified copy. Repeatable. |
| `--no-loosen` | With `--commit`, skip the second commit that loosens plugin constraints to `^x.y`. |

The environment: `WORDPRESS_NIX_CATALOG` (a catalog file in place of wordpress.org, WP Packages and Packagist: offline runs and the tests) and `WORDPRESS_NIX_DATA_DIR` (the data files) only.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Done, or `--dry-run` finished. |
| `1` | A generic failure, `--strict` warnings, a blocked plan, or a failed lock step (`composer update`, relock, `nix flake lock`). |
| `2` | `--init` into a directory that is not empty, without `--force`. |
| `4` | A `flake.nix` that is not wordpress-nix's is in the way. `--migrate --force` writes the site flake to `flake.nix.wordpress-nix` to merge. |
| `5` | The WordPress version is undetectable or below 6.6. |
| `6` | The directory is not the shape the mode needs (not WordPress, multisite), you declined the confirmation, or there is no survey or dump to tell the active set. |
| `7` | A WordPress skeleton with no code in it: nothing to migrate. |
| `8` | The directory is inside another git repository. `--force` creates a nested one anyway. |

The exit codes of `--sync` and `--check` are `wordpress-nix sync`'s, the shared table frappe-nix documents: 0 clean, 1 drift, 2 invalid configuration (including a site that has not opted in), 3 environment. A usage error under them is 2, never 1.
