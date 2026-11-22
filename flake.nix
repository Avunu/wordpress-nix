{
  description = "WordPress on Nix: stock Bedrock sites built from composer.lock and served read-only by FrankenPHP";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # The site dev shell (modules/devenv.nix) is a flake-parts + devenv module,
    # as in frappe-nix and odoo-nix; site flakes consume it through
    # lib.mkFlake and flakeModules.default.
    flake-parts.url = "github:hercules-ci/flake-parts";
    devenv.url = "github:cachix/devenv";

    # Stock Roots Bedrock, the layout every v3 site repository is: the
    # template (templates/bedrock-site) carries its files unedited, and the
    # `bedrock-template` check holds them to this tag. Dependabot's nix
    # ecosystem bumps it here, and nowhere else.
    bedrock = {
      url = "github:roots/bedrock/1.31.9";
      flake = false;
    };

    # Shared building blocks with frappe-nix and odoo-nix: lib.checks.mk names
    # and builds the checks. Its own inputs are nixpkgs and flake-parts, both
    # followed, so it adds one node to the lock.
    python-devops-nix = {
      url = "github:Avunu/python-devops-nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.flake-parts.follows = "flake-parts";
    };
  };

  # Nix honours this for a trusted user who accepts it (a prompt the first time, or
  # `--accept-flake-config`); everyone else gets a warning and a local build. The wordpress-nix
  # cache holds the optimized PHP builds and the native extensions, which cache.nixos.org lacks.
  # Same key as modules/nixos.nix and templates/bedrock-site/flake.nix; keep them equal.
  nixConfig = {
    extra-substituters = [
      "https://devenv.cachix.org"
      "https://wordpress-nix.cachix.org"
    ];
    extra-trusted-public-keys = [
      "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw="
      "wordpress-nix.cachix.org-1:CYaSELCIUp9U26Ko+ajzE7M+l/dtiG5hBLEtvFXLUeA="
    ];
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      flake-parts,
      bedrock,
      python-devops-nix,
      ...
    }:
    {
      # A site flake's outputs, as a flake-parts module: the Bedrock build
      # (wordpress-nix.site, modules/site.nix; templates/bedrock-site) and the
      # dev shell (modules/devenv.nix). Applied to this flake, so the module
      # reaches wordpress-nix's own lib and packages.
      flakeModules.default = nixpkgs.lib.modules.importApply ./modules/flake-module.nix self;

      # One Bedrock site on NixOS, served read-only (docs/reference/nixos-module.md).
      # It takes python-devops-nix's lib: the unit hardening, the migrate flow,
      # the socket directory and the journald fields.
      nixosModules.default = import ./modules/nixos.nix { base = python-devops-nix.lib; };
      nixosModules.wordpress-nix = self.nixosModules.default;

      # Composable builders, reused by the container build and available to consumers.
      lib = {
        # flake-parts entry point for site flakes: wordpress-nix's own inputs
        # (nixpkgs, devenv, python-devops-nix) are merged under the site's, so
        # a site declares only wordpress-nix.
        #   outputs = { self, wordpress-nix, ... }@inputs:
        #     wordpress-nix.lib.mkFlake { inherit inputs; } ({ ... }: { imports = [ wordpress-nix.flakeModules.default ]; ... });
        mkFlake =
          {
            inputs ? { },
            ...
          }:
          config:
          flake-parts.lib.mkFlake {
            inputs = self.inputs // inputs;
          } config;

        mkPhp = import ./lib/php.nix; # { pkgs, php ? pkgs.php83, optimize ? true, ... }
        mkFrankenphp = import ./lib/frankenphp.nix; # { pkgs, php }

        # A stock Bedrock site: php.mkComposerVendor (the FOD, hash in
        # nix/vendor-hash) into php.buildComposerProject2. The package is the
        # read-only tree production serves from ${package}/web.
        #   mkBedrockSite { inherit pkgs; name = "mysite"; src = ./.; php = pkgs.php84; }
        mkBedrockSite = import ./lib/mk-bedrock-site.nix;
      };

      # `nix flake init -t github:Avunu/wordpress` scaffolds a v3 site: stock
      # Bedrock (held to the `bedrock` input by the bedrock-template check),
      # the composer.json delta, a thin flake on wordpress-nix.site, and the
      # managed Dependabot file. Then `composer update && nix run .#relock`.
      templates.default = {
        path = ./templates/bedrock-site;
        description = "A WordPress site: stock Roots Bedrock, Composer-built and served read-only by wordpress-nix";
      };

    }
    // flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        packages =
          let
            # FrankenPHP on the optimized PHP, as nixosModules.default builds it
            # (the per-site ini is a separate scan directory): what the binary
            # cache serves (.github/workflows/cache.yml).
            frankenphp =
              php:
              import ./lib/frankenphp.nix {
                inherit pkgs;
                php = import ./lib/php.nix { inherit pkgs php; };
              };
          in
          {
            frankenphp-php83 = frankenphp pkgs.php83;
            frankenphp-php84 = frankenphp pkgs.php84;
            frankenphp-php85 = frankenphp pkgs.php85;
          }
          // {
            # Migration, step zero when a plugin rewrote the core tables' keys.
            restore-core-keys = import ./lib/restore-core-keys.nix {
              inherit pkgs;
              php = pkgs.php84;
            };
            # The migration's gate on an untrusted dump: refuse, or strip, the
            # objects that would EXECUTE on the new server once restored.
            audit-mysql-dump = import ./lib/audit-mysql-dump.nix { inherit pkgs; };
            # A built Bedrock tree against wordpress.org's published checksums
            # (core, wp-plugin/*) and locked zips (wp-theme/*). Needs the network.
            verify-wp-checksums = import ./lib/verify-wp-checksums.nix { inherit pkgs; };
          }
          // (
            let
              tools = import ./lib/tools.nix {
                inherit pkgs;
                base = python-devops-nix.lib;
                coreRev = python-devops-nix.rev or null;
                auditMysqlDump = self.packages.${system}.audit-mysql-dump;
                verifyWpChecksums = self.packages.${system}.verify-wp-checksums;
              };
            in
            {
              # `nix run github:Avunu/wordpress`: the scaffolder (docs/reference/scaffolder.md).
              default = tools.wordpressInit;
              wordpress-init = tools.wordpressInit;
              # The conversion commands and the site CI steps (docs/reference/wordpress-nix-cli.md).
              wordpress-nix = tools.wordpressNix;
              wordpress-nix-tools = tools.package;
            }
          );

        apps = {
          default = {
            type = "app";
            program = pkgs.lib.getExe self.packages.${system}.wordpress-init;
            meta.description = "Scaffold a wordpress-nix site, or convert a pulled classic WordPress install";
          };
          wordpress-nix = {
            type = "app";
            program = pkgs.lib.getExe self.packages.${system}.wordpress-nix;
            meta.description = "wordpress-nix's conversion commands and site CI steps";
          };
        };

        # Toolchain for platform development: `nix develop`.
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.php84
            pkgs.php84.packages.composer
          ];
        };
      }
      // {
        # NOTE: one `checks` attrset — `//` is shallow, so a second
        # `// { checks.x = ...; }` would drop the earlier checks entirely.
        checks =
          let
            # The v3 checks are registered through python-devops-nix's helper,
            # which names each wordpress-nix-<x>-check.
            mk = python-devops-nix.lib.checks.mk {
              inherit pkgs;
              product = "wordpress-nix";
            };
            # Stock Bedrock over a local Composer artifact repository: the
            # offline fixture both Bedrock suites build.
            fixtureSite = self.lib.mkBedrockSite {
              inherit pkgs;
              name = "bedrock-fixture";
              src = ./tests/fixtures/bedrock-site;
              php = pkgs.php84;
              composerRepositoryPaths = [ "artifacts" ];
              productionOnlyPlugins = [ "fixture-plugin" ];
              activeTheme = "fixture-theme";
            };
            bedrockBuild = import ./tests/bedrock-build.nix {
              inherit pkgs;
              checks = mk;
              site = fixtureSite;
            };
          in
          bedrockBuild
          // {
            # The template and the fixture are stock Bedrock, held to the input.
            bedrock-template = import ./tests/bedrock-template.nix {
              inherit pkgs bedrock;
              checks = mk;
            };
            # Tampered and extra PHP files fail, against a local stand-in for
            # wordpress.org serving the fixture's checksums.
            verify-wp-checksums = import ./tests/verify-wp-checksums.nix {
              inherit pkgs;
              checks = mk;
              site = fixtureSite;
              verifier = self.packages.${system}.verify-wp-checksums;
            };
            # Key restoration on a plugin-mangled dump.
            restore-core-keys = import ./tools/test/restore-core-keys-test.nix {
              inherit pkgs;
              restoreCoreKeys = self.packages.${system}.restore-core-keys;
            };
            # An untrusted dump is refused, reported, and stripped clean.
            audit-mysql-dump = import ./tools/test/audit-mysql-dump-test.nix {
              inherit pkgs;
              auditor = self.packages.${system}.audit-mysql-dump;
            };
            # wordpress-nix-tools' pytest suite (its check phase).
            tools = self.packages.${system}.wordpress-nix-tools;
            # `wordpress-init --migrate` over a synthetic classic install, offline:
            # dispositions, the backup, idempotency, the guard rails' exit codes,
            # and the lock step's ordering against stand-in composer and nix.
            migrate-classic = import ./tests/migrate-classic.nix {
              inherit pkgs;
              checks = mk;
              wordpress-init = self.packages.${system}.wordpress-init;
            };
          }
          // pkgs.lib.optionalAttrs pkgs.stdenv.hostPlatform.isLinux {
            # The journald fields and the Caddyfile, evaluated and adapted (no VM).
            logging = import ./tests/logging.nix {
              inherit pkgs;
              inherit (nixpkgs.lib) nixosSystem;
              wordpressModule = self.nixosModules.default;
              site = fixtureSite;
            };
            # `nix build .#checks.<system>.module` runs the NixOS VM test (needs
            # KVM). The template's site, whose vendor FOD fetches from Composer's
            # registries (allowed to a fixed-output derivation).
            module = import ./tests/module.nix {
              inherit pkgs;
              wordpressModule = self.nixosModules.default;
              mkBedrockSite = self.lib.mkBedrockSite;
            };
          };
      }
    );
}
