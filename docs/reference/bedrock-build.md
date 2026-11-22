# The Bedrock build (v3)

How a v3 site repository becomes the read-only tree production serves:
`lib/mk-bedrock-site.nix` (`lib.mkBedrockSite`), driven by
`wordpress-nix.site` in a site flake (`modules/site.nix`), with
`nix run .#relock` (`lib/relock.nix`) keeping `nix/vendor-hash` current.

## The two derivations

1. **The vendor FOD**, `php.mkComposerVendor`. Its source is `composer.json`
   and `composer.lock` only, so committed premium code and everything else in
   the repository never perturbs its hash. It runs nixpkgs' vendor hook
   (`composer install --no-autoloader`) with Composer plugins enabled
   (`composerNoPlugins = false`): `composer/installers` places `wp-plugin/*`,
   `wp-theme/*` and mu-plugins under `web/app/`, and
   `roots/wordpress-core-installer` places core in `web/wp`.
   - The hook keeps `vendor/` and the `extra.installer-paths` prefixes, not
     `extra.wordpress-install-dir`, so `postInstall` copies `web/wp` itself.
   - It then removes everything Composer *generates* from the packages:
     `vendor/autoload.php`, `vendor/composer/{autoload_*,ClassLoader,platform_check}.php`,
     `LICENSE`, `installed.php` and `InstalledVersions.php`. What stays is the
     packages and `installed.json`.
   - Its store path is its name and its hash, so CI, Attic and the cluster
     nodes share it even where the package derivations differ.

2. **The package**, `php.buildComposerProject2` with `composerVendor` set to
   the FOD. nixpkgs' install hook restores `vendor/` and the installer paths,
   then runs an offline `composer install`, which writes the autoloader;
   `postBuild` then runs `composer dump-autoload --no-dev --optimize`.
   `installPhase` copies the tree to `$out` (not the hook's
   `share/php/<pname>`), and:
   - refuses to install `.env*`, `auth.json` or `.git` into the store;
   - links `web/app/uploads` → `/var/lib/wordpress/uploads` and `.env` →
     `/run/wordpress/bedrock.env`, and drops `web/app/{cache,upgrade}`;
   - injects the platform mu-plugins as single files in `web/app/mu-plugins`;
   - writes the FOD's path to `share/wordpress-nix/vendor-src`, so the FOD is a
     runtime reference of every closure that holds the site;
   - asserts `web/wp/wp-includes/version.php`, `vendor/autoload.php`,
     `vendor/composer/autoload_real.php`, `installed.php` and
     `InstalledVersions.php` exist, and that the committed PHP under `web/app`
     parses (`php -l`).

`passthru.wordpressNix` carries `php` (the nixpkgs base the runtime optimizes),
`wordpressVersion` (from `composer.lock`), `vendor`, `uploadsDir`, `envFile`,
`productionOnlyPlugins`, `developmentOnlyPlugins` and `activeTheme`.

## Findings recorded in M1

- **The install hook rewrites `installed.php` and `InstalledVersions.php`.**
  Confirmed: the FOD has neither (the build removes them), the package has both,
  and `\Composer\InstalledVersions` answers for the locked core.
  `tests/bedrock-build.nix` (`bedrock-build`) asserts all three, so the
  fallback (keeping them in the FOD, which ties the hash to Composer's version)
  is not needed. The install hook's `composer install` finds every locked
  package installed ("Nothing to install, update or remove") and still writes
  the local repository files and the autoloader.
- **The hook nests installer paths into committed ones.** It restores each
  prefix with `cp -ar <vendor>/<prefix> <prefix>`; when `<prefix>` already
  exists, as `web/app/{plugins,themes,mu-plugins}` always do in a Bedrock
  repository, GNU cp copies *into* it (`web/app/plugins/plugins/…`). `preBuild`
  therefore moves the committed directories aside before the hook runs (stdenv
  runs the `preBuild` attribute ahead of `preBuildHooks`), and `postBuild`
  moves them back beside Composer's. A committed directory named like a
  Composer package fails the build (`bedrock-build-collision`).
- **The vendor hash is stable** across two clean builds
  (`bedrock-build-rebuild`: the FOD built again under another name, against the
  same committed hash; `nix build --rebuild` of the fixture's and the
  template's FOD) and across nixpkgs revisions: the fixture's committed hash
  built unchanged with Composer 2.9.8 (nixos-25.11), 2.10.3 (this flake's
  nixpkgs) and 2.11.2 (a later nixos-unstable). nixos-25.11's vendor hook still
  wrote the autoloader into the FOD; removing the generated files in
  `postInstall` is what makes that revision agree.
- **`composer validate --strict --no-check-all --no-check-publish --check-lock`
  passes on stock Bedrock with exact pins** (`roots/wordpress` `7.1.5`,
  `roots/wp-config` `1.0.0`): `templates/bedrock-site` and the fixture.
- **A plugin bump plus `nix run .#relock` builds**: verified on a copy of the
  template (`composer require wp-plugin/two-factor:0.9.1 --no-install`, relock,
  `nix build`, `nix flake check`).

## Relock

`nix run .#relock` (from the site's root) writes `lib.fakeHash` to
`nix/vendor-hash`, builds `.#vendor`, takes the `got:` hash from the failure,
writes it, and builds again to confirm. On any failure the old hash is put
back. In a git work tree the file is staged, since a flake sees only what git
tracks.

## The site flake's checks

All pure (`nix flake check` reaches no network beyond fixed-output fetches):

| check | what |
|---|---|
| `composer-validate` | `composer validate --strict --no-check-all --no-check-publish --check-lock` |
| `vendor-hash` | `nix/vendor-hash` is one sha256 SRI hash, not `lib.fakeHash` |
| `build` | the package, with the assertions above |

`verify-wp-checksums` (`nix run .#verify-wp-checksums`) needs the network and
runs as a CI step: core and `wp-plugin/*` against wordpress.org's checksum
APIs, `wp-theme/*` against the locked zip. A PHP file that differs, or that
wordpress.org never shipped, fails it.

## wordpress-nix's own checks

| check | what |
|---|---|
| `bedrock-build`, `-rebuild`, `-collision`, `-eval` | `tests/bedrock-build.nix` over `tests/fixtures/bedrock-site` |
| `bedrock-template` | the template's and the fixture's Bedrock files equal the pinned `bedrock` input |
| `verify-wp-checksums` | pristine passes; a tampered plugin file, an extra core or theme PHP file, an unknown plugin version fail |

The fixture resolves from a local Composer `artifact` repository
(`tests/fixtures/bedrock-site/artifacts`, written by `make-artifacts.py`):
the real `composer/installers`, `roots/wordpress-core-installer`,
`roots/wordpress` and `roots/bedrock-disallow-indexing`, plus small stand-ins
for core, a plugin, a theme and a PSR-4 library. `composerRepositoryPaths`
puts `artifacts/` in the FOD's source.
