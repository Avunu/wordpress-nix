# A stock Roots Bedrock site, built the nixpkgs way: php.mkComposerVendor, a
# fixed-output derivation whose only inputs are composer.json and
# composer.lock, fed to php.buildComposerProject2, whose install hook restores
# it and writes the Composer autoloader offline. The result is the read-only
# tree production serves: `${package}/web` is the document root.
#
#   mkBedrockSite {
#     inherit pkgs;
#     name = "examplepainting";
#     src = ./.;                       # the site repository
#     php = pkgs.php84;                # vendorHashFile defaults to src + "/nix/vendor-hash"
#   }
#
# Returns the package; `.vendor` (and passthru.wordpressNix.vendor) is the FOD.
# `nix run .#relock` (lib/relock.nix) rewrites nix/vendor-hash after a lock
# change. docs/reference/bedrock-build.md has the reasoning; the short of it:
#
#   - The vendor hash covers composer.json and composer.lock only, so committed
#     premium code and everything else in the repository never perturbs it.
#   - The FOD's store path depends only on its name and hash, so CI, Attic and
#     the cluster nodes share it even where the package derivations differ.
#   - Composer plugins run (composerNoPlugins = false): composer/installers
#     places wp-plugin/* and wp-theme/* under web/app, and
#     roots/wordpress-core-installer places core in web/wp.
{
  pkgs,
  lib ? pkgs.lib,
  # The site's slug: the package's pname and the vendor FOD's.
  name,
  # The site repository: composer.json, composer.lock, config/, web/.
  src,
  # A nixpkgs PHP (pkgs.php84, ...). It runs Composer here; the runtime builds
  # wordpress-nix's optimized PHP from it (passthru.wordpressNix.php).
  php ? pkgs.php84,
  # The one-line file holding the vendor FOD's hash (`nix run .#relock`).
  vendorHashFile ? src + "/nix/vendor-hash",
  # Paths under src, besides composer.json and composer.lock, that the lock
  # resolves from: a local `path` or `artifact` repository. Empty for a real
  # site; the offline fixture (tests/bedrock-build.nix) names its artifacts.
  composerRepositoryPaths ? [ ],
  # Single-file platform mu-plugins, injected into web/app/mu-plugins (which
  # WordPress loads without Bedrock's autoloader). A copy the site committed
  # under the same name is replaced.
  muPlugins ? [
    ../mu-plugins/platform-environment.php
    ../mu-plugins/platform-nonce.php
    ../mu-plugins/platform-user-enumeration.php
  ],
  # Where the runtime keeps what is writable (§7.1 of the v3 plan): uploads
  # (web/app/uploads links there) and Bedrock's .env (the package's .env links
  # there; Bedrock reads real environment variables when it is absent).
  uploadsDir ? "/var/lib/wordpress/uploads",
  envFile ? "/run/wordpress/bedrock.env",
  # Carried in passthru.wordpressNix for the NixOS module and the smoke test.
  productionOnlyPlugins ? [ ],
  developmentOnlyPlugins ? [ ],
  activeTheme ? null,
}:
let
  root = lib.path.append src;

  lock = lib.importJSON (root "composer.lock");
  locked = lib.listToAttrs (map (p: lib.nameValuePair p.name p.version) (lock.packages or [ ]));
  # roots/wordpress-no-content is the core itself; roots/wordpress is the
  # metapackage Bedrock requires, pinned to the same version.
  wordpressVersion = lib.removePrefix "v" (
    locked."roots/wordpress-no-content" or locked."roots/wordpress"
      or (throw "mkBedrockSite: ${name}'s composer.lock locks neither roots/wordpress-no-content nor roots/wordpress")
  );

  vendorHash = lib.fileContents vendorHashFile;

  composerSettings = {
    # composer/installers and roots/wordpress-core-installer must run: without
    # them every plugin, theme and core itself would land in vendor/.
    composerNoPlugins = false;
    composerNoDev = true;
    composerNoScripts = true;
    # `composer validate` is its own flake check (composer-validate), with
    # --check-lock; here it would only repeat it.
    composerStrictValidation = false;
  };

  vendor = php.mkComposerVendor (
    composerSettings
    // {
      pname = "${name}-vendor";
      # Not the WordPress version: the FOD's path is its name and its hash, and
      # a core bump already changes the hash.
      version = "0";
      src = lib.fileset.toSource {
        root = src;
        fileset = lib.fileset.unions (
          [
            (root "composer.json")
            (root "composer.lock")
          ]
          ++ map root composerRepositoryPaths
        );
      };
      inherit vendorHash;

      # The vendor hook keeps vendor/ and the extra.installer-paths prefixes
      # (web/app/{mu-plugins,plugins,themes}); core arrives through
      # extra.wordpress-install-dir, which the hook knows nothing of, so web/wp
      # is copied here or lost.
      postInstall = ''
        wpdir=$(jq -r '.extra."wordpress-install-dir" // "web/wp"' composer.json)
        mkdir -p "$out/$(dirname "$wpdir")"
        cp -a "$wpdir" "$out/$wpdir"
        test -f "$out/$wpdir/wp-includes/version.php"

        # The FOD holds the packages and installed.json, nothing Composer
        # generates from them: installed.php and InstalledVersions.php come
        # from the Composer that ran here (so would tie the hash to its
        # version), and the autoloader from nixpkgs' vendor hook (nixos-25.11's
        # wrote it, later ones pass --no-autoloader). The package's offline
        # `composer install` writes them all again (tests/bedrock-build.nix
        # asserts that it does).
        rm -f "$out/vendor/autoload.php" "$out/vendor/composer/installed.php" \
          "$out/vendor/composer/InstalledVersions.php" "$out/vendor/composer/ClassLoader.php" \
          "$out/vendor/composer/LICENSE" "$out/vendor/composer/platform_check.php" \
          "$out"/vendor/composer/autoload_*.php
      '';
    }
  );

  # The site repository less what must never reach the store tree production
  # serves (secrets, credentials, VCS and dev state) and what the build
  # replaces (vendor/, core).
  excluded = [
    ".git"
    ".github"
    ".devenv"
    ".direnv"
    ".envrc"
    "auth.json"
    "flake.nix"
    "flake.lock"
    "nix"
    "secrets"
    "vendor"
    "web/wp"
  ];
  cleanedSrc = lib.cleanSourceWith {
    name = "${name}-source";
    inherit src;
    filter =
      path: _type:
      let
        rel = lib.removePrefix "${toString src}/" (toString path);
        base = baseNameOf rel;
      in
      !(lib.elem rel excluded || lib.hasPrefix ".env" base || lib.hasPrefix "result" rel);
  };

  # The installer-paths prefixes, as the nixpkgs hooks read them.
  installerPaths = ''jq -r 'try((.extra."installer-paths") | keys[])' composer.json | sed 's/{\$name}.*//; s:/*$::' '';
in
php.buildComposerProject2 (
  composerSettings
  // {
    pname = name;
    version = wordpressVersion;
    src = cleanedSrc;
    composerVendor = vendor;
    inherit php;

    postConfigure = ''
      wpdir=$(jq -r '.extra."wordpress-install-dir" // "web/wp"' composer.json)
      mkdir -p "$(dirname "$wpdir")"
      cp -a ${vendor}/"$wpdir" "$wpdir"
      chmod -R u+w "$wpdir"
    '';

    # composerInstallBuildHook restores each installer-paths prefix with
    # `cp -ar <vendor>/<prefix> <prefix>`, which nests the copy inside a prefix
    # the tree already has, and stock Bedrock commits all three
    # (web/app/{mu-plugins,plugins,themes}). So the committed ones step aside
    # until the hook is done; this runs before it (stdenv runs the preBuild
    # attribute ahead of preBuildHooks).
    preBuild = ''
      mkdir -p "$NIX_BUILD_TOP/committed"
      while read -r prefix; do
        if [[ -n "$prefix" && -e "$prefix" && -e "${vendor}/$prefix" ]]; then
          mkdir -p "$NIX_BUILD_TOP/committed/$(dirname "$prefix")"
          mv "$prefix" "$NIX_BUILD_TOP/committed/$prefix"
        fi
      done < <(${installerPaths})
    '';

    postBuild = ''
      # Committed premium and custom code back beside what Composer installed.
      # A committed directory named like a Composer package is an error, never
      # a silent overwrite either way.
      while read -r prefix; do
        [[ -n "$prefix" && -d "$NIX_BUILD_TOP/committed/$prefix" ]] || continue
        mkdir -p "$prefix"
        for entry in "$NIX_BUILD_TOP/committed/$prefix"/* "$NIX_BUILD_TOP/committed/$prefix"/.[!.]*; do
          [[ -e "$entry" ]] || continue
          if [[ -e "$prefix/$(basename "$entry")" ]]; then
            echo "mkBedrockSite: committed $prefix/$(basename "$entry") collides with a Composer package of the same name" >&2
            exit 1
          fi
          mv "$entry" "$prefix/"
        done
      done < <(${installerPaths})

      composer dump-autoload --no-dev --optimize --no-plugins --no-scripts --no-interaction
    '';

    # The tree itself, not the hook's share/php/<pname>: the NixOS module
    # serves ''${package}/web and reads ''${package}/.env. So no
    # `runHook preInstall`, whose composerInstallInstallHook would copy the
    # whole tree a second time under share/php.
    installPhase = ''
      mkdir -p "$out"
      cp -a . "$out/"
      cd "$out"

      # Fail closed on anything that must not be in a store path.
      leaked=$(find . \( -name '.env*' -o -name auth.json -o -name .git \) -print)
      if [[ -n "$leaked" ]]; then
        echo "mkBedrockSite: refusing to install secrets or VCS state into the store:" >&2
        echo "$leaked" >&2
        exit 1
      fi

      # Writable state lives outside the store (§7.1); cache and upgrade only
      # matter to a writable install.
      rm -rf web/app/uploads web/app/cache web/app/upgrade
      ln -s ${lib.escapeShellArg uploadsDir} web/app/uploads
      ln -s ${lib.escapeShellArg envFile} .env

      mkdir -p web/app/mu-plugins
      ${lib.concatMapStringsSep "\n" (p: ''
        install -m 0444 ${p} web/app/mu-plugins/${baseNameOf (toString p)}
      '') muPlugins}

      # The FOD in every closure that holds the package, so a binary cache's
      # retention keeps it as long as anything running needs it.
      mkdir -p share/wordpress-nix
      echo ${vendor} > share/wordpress-nix/vendor-src

      wpdir=$(jq -r '.extra."wordpress-install-dir" // "web/wp"' composer.json)
      for f in "$wpdir/wp-includes/version.php" vendor/autoload.php \
               vendor/composer/autoload_real.php vendor/composer/installed.php \
               vendor/composer/InstalledVersions.php; do
        if [[ ! -f "$f" ]]; then
          echo "mkBedrockSite: $f is missing from the package" >&2
          exit 1
        fi
      done

      # Committed code is the one part of web/app no registry vouches for.
      (cd ${cleanedSrc} && find web/app -name '*.php' -print0) \
        | (cd ${cleanedSrc} && xargs -0 -r -n 100 -P "$NIX_BUILD_CORES" php -n -l) \
        | { grep -v '^No syntax errors detected' || true; } > "$TMPDIR/lint"
      if [[ -s "$TMPDIR/lint" ]]; then
        cat "$TMPDIR/lint" >&2
        echo "mkBedrockSite: committed PHP under web/app does not parse" >&2
        exit 1
      fi
      runHook postInstall
    '';

    passthru = {
      inherit vendor;
      wordpressNix = {
        inherit
          php
          wordpressVersion
          vendor
          uploadsDir
          envFile
          productionOnlyPlugins
          developmentOnlyPlugins
          activeTheme
          ;
      };
    };

    meta.description = "${name}: a Bedrock WordPress site (Composer-built, read-only)";
  }
)
