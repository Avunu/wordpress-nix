---
title: Getting started
description: "Scaffold a Bedrock site repository with wordpress-nix, with its CI, its Dependabot pipeline and a build the cluster can deploy."
nav_title: Getting started
order: 1
tags: [quick-start, bedrock]
updated: 2026-11-24
---

# Getting started

This page takes you from an empty directory to a site repository with its CI,
its Dependabot pipeline and a build the cluster can deploy. A site converted
from a running WordPress follows the scaffolder's migrate mode instead
(`wordpress-init --migrate`), and ends in the same place.

On a new machine, set up Nix, direnv and VS Code first with [the new-host guide](https://avunu.net/kb/nixos/getting-started-nix-development/).

You need Nix with flakes, `git`, and a GitHub account in the Avunu organisation
for the last steps.

## 1. The site

```sh
nix flake new -t github:Avunu/wordpress site-mysite
cd site-mysite
```

That is stock [Roots Bedrock](https://roots.io/bedrock/) plus wordpress-nix's
few files:

| File | What |
|---|---|
| `flake.nix` | `wordpress-nix.site = { name; src; php; ... }`; the only input is wordpress-nix |
| `composer.json`, `composer.lock` | core (`roots/wordpress`), wordpress.org plugins and themes (`wp-plugin/*`, `wp-theme/*` from WP Packages), PHP libraries |
| `nix/vendor-hash` | the hash of everything Composer installs (`nix run .#relock` writes it) |
| `wordpress-nix.toml` | the standards table: `profile = "deployment"`, the site's facts |
| `.github/`, `.editorconfig`, `.pre-commit-config.yaml`, `committed.toml`, the `site` block of `.gitignore` | managed by `wordpress-nix sync` |

Give it its slug (lowercase, digits, dashes; the repository is `site-<slug>`):

```sh
sed -i 's/name = "changeme";/name = "mysite";/' flake.nix
sed -i 's|home-url = "https://example.org"|home-url = "https://mysite.example"|' wordpress-nix.toml
git init -b main && git remote add origin git@github.com:Avunu/site-mysite.git
git add -A
nix flake lock
```

## 2. The managed files

```sh
nix run .#wordpress-nix -- sync --write
```

This writes the three caller workflows, pinned to the wordpress-nix revision
`flake.lock` now locks, and re-renders `.github/dependabot.yml` (its Composer
time is hashed from the slug) and `.github/wordpress-nix.json`. Run it again
after any change to `wordpress-nix.toml`; `sync --check` reports drift.
[Site CI](production/site-ci.md) lists every file.

## 3. Plugins, themes, core

```sh
nix run .#composer -- require --no-install wp-plugin/two-factor wp-theme/twentytwentyfive
nix run .#relock                 # nix/vendor-hash follows composer.lock
nix build                        # result/web is the document root
nix flake check --no-pure-eval   # composer-validate, vendor-hash, build, smoke (KVM)
```

Premium and custom code is committed under `web/app/{plugins,themes,mu-plugins}/<slug>/`,
each directory whitelisted between `# BEGIN wordpress-nix` and `# END wordpress-nix`
in `.gitignore` (`!web/app/plugins/<slug>`).

The dev shell (`direnv allow`, or `nix develop --no-pure-eval`) runs the site
over the checkout: `composer install`, `devenv up`, `wp restore`.

## 4. The repository

```sh
git add -A && git commit -m "feat: the site"
gh repo create Avunu/site-mysite --private --source . --push
```

Then, once per repository (`wordpress-nix repo apply`, or by hand):

- auto-merge on, squash merges only, secret scanning and push protection;
- the `main` ruleset requiring `ci / flake`, `ci / smoke` and `ci / audit`;
- the org secrets `APP_ID`, `APP_PRIVATE_KEY`, `WORDPRESS_NIX_CACHE_READ_TOKEN`
  and `WORDFENCE_API_KEY` reaching the repository as **both** Actions and
  Dependabot secrets, `WORDPRESS_NIX_CACHE_TOKEN` as an Actions secret, and the
  `WORDPRESS_NIX_CACHE` variable.

From here Dependabot opens grouped Composer PRs daily; each is relocked,
checked and merged by the [pipeline](production/site-ci.md) with no one acting,
and the cluster deploys `main` ([Releases and the cluster](production/releases.md)).
