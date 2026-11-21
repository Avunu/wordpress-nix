# Site CI and the dependency pipeline

Every site repository (`Avunu/site-<slug>`) runs the same three workflows, thin
callers of wordpress-nix's reusable ones, pinned to the wordpress-nix revision
the site's `flake.lock` locks. `wordpress-nix sync` (or `wordpress-init
--sync`) writes them; nobody edits them by hand.

| Site workflow | Calls | Runs on |
|---|---|---|
| `.github/workflows/ci.yml` | `site-ci.yml` | every pull request, every push to `main` |
| `.github/workflows/deps.yml` | `site-deps.yml` | every pull request (it acts on Dependabot's), pushes to `main`, every 6 hours |
| `.github/workflows/audit.yml` | `site-audit.yml` | daily and weekly |

## The managed files

A site opts in with `wordpress-nix.toml`:

```toml
[tool.wordpress-nix]
schema = 1
profile = "deployment"        # the wordpress-site profile, deployment@1.0

[tool.wordpress-nix.site]
home-url = "https://example.org"
# woocommerce = true          # default: whether composer.lock locks wp-plugin/woocommerce
# drop-plugins = ["mainwp-child/mainwp-child.php"]
# local-upload-prefixes = ["cwicly", "fonts", "wpcodebox", "fluentform"]
```

`deployment@1.0` is wordpress-nix's built-in profile for a site, a deployment
project in python-devops-nix's terms: everything `recommended` has except a
release flow. Its files:

| File | Module | What |
|---|---|---|
| `.github/dependabot.yml` | dependabot | Composer daily (a time between 02:00 and 05:59 hashed from the slug), grouped `wordpress-core`, `wp-plugins`, `avunu`, `php-libs`; cooldown 7 days, 1 for patches, none for core; github-actions weekly, ignoring wordpress-nix's own workflows |
| `.github/workflows/{ci,deps,audit}.yml` | ci | the callers above |
| `.github/wordpress-nix.json` | site | the site's facts (`slug`, `woocommerce`, `homeUrl`, `dropPlugins`, `localUploadPrefixes`), which the workflows, the decision table and the data commands read without Python |
| `.editorconfig` | editorconfig | 4 spaces for PHP, 2 for the rest |
| `.pre-commit-config.yaml` | lint | no dumps, `auth.json` or `.env` in git; `php -l`; committed's message check |
| `committed.toml` | commits | conventional commits |

`repo-policy` makes `main` require `ci / flake`, `ci / smoke` and `ci / audit`,
with no required approval (Dependabot's pull requests must merge with no person
in the loop), squash merges only.

## The gates (`site-ci.yml`)

| Check | Runner | What |
|---|---|---|
| `ci / flake` | `[self-hosted, nixos]` | every check of the site flake but `smoke`: `composer-validate`, `vendor-hash`, `build` and any the site adds; no tracked `*.sql`, `auth.json` or `.env`. On a push to `main`, the vendor tree is uploaded to the cache |
| `ci / smoke` | `[self-hosted, linux, x64, kvm]` | `checks.smoke`: the package in a NixOS VM with MariaDB; `wp core install`, every plugin activated, the theme, `/`, `/wp/wp-login.php`, `/wp-json/`; a read-only code tree; PHP under uploads refused; no PHP warning or permission error |
| `ci / audit` | `[self-hosted, nixos]`, networked steps | `composer audit --locked --no-dev`; `nix run .#verify-wp-checksums`; `nix run .#wordpress-nix -- audit --base <merge-base>` (Wordfence: only what the change introduces fails) |
| `ci / premium-diff` | `[self-hosted, nixos]` | advisory: warnings on `eval`, `base64_decode`, `gzinflate`, `create_function`, `assert($…)`, outbound PHP URLs and long opaque strings that a change adds to committed code |

A pull request from a fork runs on `ubuntu-latest`.

## A Composer pull request, end to end (`site-deps.yml`)

1. Dependabot opens `fix(deps): bump the wp-plugins group …`, changing
   `composer.json` and `composer.lock`. `disarm` turns off any auto-merge an
   earlier head enabled.
2. `relock-plan` (the caller's runner, no write token) runs `nix run .#relock`
   on the PR's head and uploads the patch.
3. `relock-push` (`ubuntu-latest`) installs wordpress-nix-tools from the
   integration branch's pinned wordpress-nix, hash-locked, then, before the App's
   token exists:
   - `wordpress-nix ci apply-relock-patch`: the patch touches `nix/vendor-hash`
     only, a regular file;
   - `wordpress-nix ci verify-relock` against the PR's merge-base (below);
   - `wordpress-nix site-verdict`, the decision table.

   It pushes `chore(deps): relock vendor hash [dependabot skip]` with the App's
   token, and enables auto-merge for that commit only when the lock is `ok` and
   the verdict is `relock`. Otherwise the PR is labelled `needs-review` with
   auto-merge off.
4. CI runs on the new head (an App's push starts it): flake, smoke, audit.
5. GitHub merges it once the required checks pass. The merge, made by the App,
   pushes to `main`, whose `ci / flake` uploads the vendor tree to the cache.
6. The cluster's Worker dispatches `flake-update.yml` in cluster-ovh-01 (see
   [Releases and the cluster](releases.md)).

A github-actions PR needs no relock: `auto-merge` enables auto-merge (third-party
actions) or closes it (wordpress-nix's own workflows, which move only through
the rollout). `hold` turns auto-merge off on any head no job reached a verdict on.

### `verify-relock`: the URL names the same package and version

frappe-nix's rule, never a host allowlist. For every package `composer.lock`
adds or changes:

| Package | Its `dist.url` must be |
|---|---|
| `wp-plugin/<s>`, `wp-theme/<s>` | `https://downloads.wordpress.org/{plugin,theme}/<s>.<version>.zip` (and the svn source wordpress.org's for `<s>`) |
| `roots/wordpress-no-content` | `https://downloads.w.org/release/wordpress-<version>-no-content.zip`, with the lock's `shasum` |
| anything else (Packagist) | `https://api.github.com/repos/<o>/<r>/zipball/<ref>` or `https://codeload.github.com/<o>/<r>/legacy.zip/<ref>`, where `<o>/<r>` is `source.url`'s and `<ref>` is `source.reference` |

A mismatch or another host is a `reject`: nothing is pushed and the PR is held.
A package the lock adds or removes is `needs-review`. `nix/vendor-hash` must be
one SRI hash, never `lib.fakeHash`.

### The decision table

| Change | Verdict |
|---|---|
| any major | review |
| `roots/wordpress*` patch | relock, then auto (no cooldown) |
| `roots/wordpress*` minor | review (until previews smoke-test it) |
| `wp-plugins` group, minor or patch | relock, then auto |
| `wp-plugin/woocommerce*` minor on a site whose `woocommerce` is true | review |
| `php-libs` group, minor or patch | relock, then auto |
| `avunu/*` | relock, then auto |
| an ungrouped Composer update, a new package, a `verify-relock` failure | review |
| github-actions, `Avunu/wordpress*` | close (moves through the rollout) |
| github-actions, other | auto |

Auto-merge still waits for every required check. A vulnerability fix is an
operator's: `composer update wp-plugin/<x>` and `nix run .#relock` on a
`security/<x>` branch, which bypasses Dependabot's cooldown; every gate applies.

## The scheduled audits (`site-audit.yml`)

| Job | When | What |
|---|---|---|
| `vulnerabilities` | daily | `wordpress-nix audit`; one issue, *Vulnerable packages* (label `security`), updated while there are findings, closed when there are none |
| `vendor-rebuild` | weekly | `nix build --rebuild .#vendor`; a mismatch under an unchanged lock opens *Vendor hash drift* (`security`). Never relock it: a registry served different bytes for a locked version |
| `cluster-compat` | daily, not required | the site built with the wordpress-nix and nixpkgs cluster-ovh-01 pins (`--override-input`), read from its `flake.lock` with the App's token on a hosted runner |

## Secrets, variables, runners

Named per the frappe-nix contract. `wordpress-nix repo apply` provisions them;
`APP_ID`, `APP_PRIVATE_KEY`, `WORDPRESS_NIX_CACHE_READ_TOKEN` and
`WORDFENCE_API_KEY` are both **org Actions secrets and org Dependabot secrets**,
scoped to `site-*` repositories: a run on Dependabot's own head sees only
Dependabot secrets, a run on an App-pushed head only Actions secrets.

| Name | Kind | Used by |
|---|---|---|
| `APP_ID`, `APP_PRIVATE_KEY` | secret (Actions + Dependabot) | relock-push, auto-merge, update-branches; site-audit's cluster read |
| `WORDPRESS_NIX_CACHE_READ_TOKEN` | secret (Actions + Dependabot) | every nix job |
| `WORDPRESS_NIX_CACHE_TOKEN` | secret (Actions only) | `ci / flake` on a push to `main` |
| `WORDFENCE_API_KEY` | secret (Actions + Dependabot) | `ci / audit`, `vulnerabilities` |
| `WORDPRESS_NIX_CACHE` | variable | `cachix:<name>` before the cluster is live, then `attic:<endpoint>/<cache>` |

Runners are nixos-dev-host's org runners: `[self-hosted, nixos]` for the
sandboxed checks and the audits, `[self-hosted, linux, x64, kvm]` for the smoke
VM. Never a production node. Every job that can hold the App's key runs on
`ubuntu-latest`.

## The fixture site (M5a)

`Avunu/site-fixture` proves the pipeline before any real site depends on it. It
holds no licensed code, and uses a private Cachix cache through
`WORDPRESS_NIX_CACHE` until the cluster's Attic is live. Its content is the
template's output:

```sh
nix flake new -t github:Avunu/wordpress site-fixture && cd site-fixture
sed -i 's/name = "changeme";/name = "fixture";/' flake.nix
git init -b main && git remote add origin git@github.com:Avunu/site-fixture.git
git add -A && nix flake lock
nix run .#wordpress-nix -- sync --write   # the callers, pinned to the locked wordpress-nix
nix run .#relock && nix flake check --no-pure-eval
git add -A && git commit -m "feat: the fixture site"
```

Then, by hand (M5a's exit criteria in the v3 plan):

1. Create the private repository and push; run `wordpress-nix repo apply`
   (auto-merge, squash only, the `main` ruleset requiring the three gates,
   secret scanning) and provision the secrets and the variable above.
2. Make sure the org runners carry the `nixos` and `kvm` labels.
3. Wait for Dependabot's first Composer PR, or pin an older `wp-plugin/*` to
   provoke one; it must relock, auto-merge and upload to the cache with no
   person acting. Likewise a github-actions PR and a `php-libs` group PR.
4. Refusals: push a branch whose `composer.lock` names a foreign dist host, and
   one whose Packagist dist owner differs from its source; `verify-relock` must
   refuse both (selftest-scaffold covers the first offline of GitHub).
5. Lock a known-vulnerable plugin version: `ci / audit` fails on the PR, and
   `site-audit`'s daily run opens the *Vulnerable packages* issue.
6. Settle whether Dependabot needs a credential-less `registries:` entry for WP
   Packages.
