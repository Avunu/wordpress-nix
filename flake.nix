{
  description = "WordPress FrankenPHP — OCI images and a NixOS module (customizable PHP; state or git source)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # The site dev shell (modules/devenv.nix) is a flake-parts + devenv module,
    # as in frappe-nix and odoo-nix; site flakes consume it through
    # lib.mkFlake and flakeModules.default.
    flake-parts.url = "github:hercules-ci/flake-parts";
    devenv.url = "github:cachix/devenv";

    # WordPress SQLite Anywhere: the SQLite Database Integration driver with
    # the Turso and Cloudflare D1 backends, as one plugin with one db.php
    # drop-in. The driver is a git submodule of that repo, hence the git URL
    # with submodules=1 (a GitHub tarball would not carry it).
    wordpress-sqlite-anywhere = {
      url = "git+https://github.com/Avunu/wordpress-sqlite-anywhere?ref=main&submodules=1";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  nixConfig = {
    extra-substituters = [ "https://devenv.cachix.org" ];
    extra-trusted-public-keys = [
      "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw="
    ];
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      flake-parts,
      wordpress-sqlite-anywhere,
      ...
    }:
    {
      # The site dev shell + image, as a flake-parts module. See templates/site.
      flakeModules.default = ./modules/flake-module.nix;

      # Deploy WordPress directly on NixOS. See readme.md for usage.
      # The flake wiring injects the SQLite plugin flake + Rust toolchain pin,
      # enabling `database.type = "d1"` / `"turso"` and the managed backend mode.
      nixosModules.default = import ./modules/nixos.nix {
        sqliteAnywhere = wordpress-sqlite-anywhere;
        rustNixpkgs = nixpkgs;
      };
      nixosModules.wordpress-nix = self.nixosModules.default;

      # Composable builders, reused by the container build and available to consumers.
      lib = {
        # flake-parts entry point for site flakes: wordpress-nix's own inputs
        # (nixpkgs, devenv, the plugin flake) are merged under the site's, so
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
        mkWordPressSite = import ./lib/site.nix; # { pkgs, src, php ? ..., plugins ? {}, themes ? {} }

        # Per-site OCI image: the pinned core + the site repo's wp-content,
        # with the D1 driver stack included by default. The primary builder
        # for site flakes; this flake's own package variants use it too.
        # The SQLite plugin requires PHP 8.5, hence the default.
        #   mkSiteImage { inherit pkgs; imageName = "site-foo"; wpContent = ./wp-content; }
        mkSiteImage =
          {
            pkgs,
            php ? pkgs.php85,
            imageName,
            tag ? "latest",
            wpContent ? null,
            plugins ? { },
            themes ? { },
            d1 ? true,
            wordpressVersion ? null,
            wordpressHash ? null,
          }:
          import ./modules/containers.nix {
            inherit
              pkgs
              php
              imageName
              tag
              wpContent
              plugins
              themes
              wordpressVersion
              wordpressHash
              ;
            sqliteAnywhere = if d1 then wordpress-sqlite-anywhere else null;
            rustPkgs = nixpkgs.legacyPackages.${pkgs.stdenv.hostPlatform.system};
          };

        # The Worker-Assets static tree for the same pinned core + wp-content.
        #   mkStaticAssets { inherit pkgs; wpContent = ./wp-content; }
        mkStaticAssets = import ./lib/static-assets.nix;

        # The Turso snapshot publisher, from the pinned plugin flake.
        #   mkTursoPublisher { inherit pkgs; }
        mkTursoPublisher =
          { pkgs, rustPkgs ? pkgs }:
          wordpress-sqlite-anywhere.lib.mkTursoPublisher { inherit pkgs rustPkgs; };

        # The bundled edge Worker (site-agnostic; one artifact per platform
        # version). `entry` is the escape hatch for site-custom routes.
        #   mkSiteWorker { inherit pkgs; }
        mkSiteWorker =
          {
            pkgs,
            entry ? null,
          }:
          import ./lib/worker.nix {
            inherit pkgs entry;
            d1ProxyWorkerSrc = wordpress-sqlite-anywhere.lib.srcs.d1ProxyWorker;
          };
      };

      # `nix flake init -t github:Avunu/wordpress#site` scaffolds a new
      # thin site repo (payload + identity + pins only).
      templates.site = {
        path = ./templates/site;
        description = "A WordPress-on-Cloudflare site: wp-content payload, wrangler identity, flake pin, CI caller";
      };
    }
    // flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        mkImage =
          php: imageName:
          self.lib.mkSiteImage {
            inherit pkgs php imageName;
            d1 = false;
          };
        mkD1Image = php: imageName: self.lib.mkSiteImage { inherit pkgs php imageName; };
      in
      {
        packages = {
          wordpress-php82 = mkImage pkgs.php82 "wordpress-php82";
          wordpress-php83 = mkImage pkgs.php83 "wordpress-php83";
          wordpress-php84 = mkImage pkgs.php84 "wordpress-php84";
          wordpress-php85 = mkImage pkgs.php85 "wordpress-php85";
          # Cloudflare D1 variant: bundles the WordPress SQLite Anywhere plugin,
          # its db.php drop-in, and the native wp_mysql_parser + wp_d1_client
          # extensions. Configure with WP_D1_PROXY_URL. PHP 8.5: the plugin
          # requires it.
          wordpress-d1-php85 = mkD1Image pkgs.php85 "wordpress-d1-php85";
          # The bundled site-agnostic edge Worker.
          worker = self.lib.mkSiteWorker { inherit pkgs; };
          # Migration: replay a MySQL dump through the driver into SQLite.
          mysql-to-sqlite =
            let
              # A plain build: this runs once per migration on an operator's
              # machine, so skip the slow clang/LTO pass. pdo_sqlite is not in
              # the platform extension set (the runtime targets D1).
              php = import ./lib/php.nix {
                inherit pkgs;
                php = pkgs.php85;
                optimize = false;
                extraExtensions = all: [ all.pdo_sqlite ];
              };
            in
            import ./lib/mysql-to-sqlite.nix {
              inherit pkgs php;
              # The assembled driver package (upstream + the plugin's patches).
              driverSrc = "${wordpress-sqlite-anywhere.packages.${system}.driver}/src";
              # The same Rust parser the site runs on.
              parserExtension = wordpress-sqlite-anywhere.lib.mkMysqlParserExtension {
                inherit pkgs php;
                rustPkgs = nixpkgs.legacyPackages.${system};
              };
            };
          # Migration, step zero when a plugin rewrote the core tables' keys.
          restore-core-keys = import ./lib/restore-core-keys.nix {
            inherit pkgs;
            php = pkgs.php85;
          };
          # Migration, step two: load the SQLite file into a Turso database.
          sqlite-to-turso = import ./lib/sqlite-to-turso.nix { inherit pkgs; };
          # The Turso snapshot publisher: the front end's read path. A live Turso
          # replica cannot be read by pdo_sqlite, so this hands PHP a plain
          # SQLite file instead. See the package's README.
          turso-snapshot-publisher = self.lib.mkTursoPublisher { inherit pkgs; };
          # The pinned wordpress-sqlite-anywhere source, materializable in CI
          # (worker tests alias @wp-sqlite/d1-proxy-worker from it).
          sqlite-driver-src = pkgs.runCommandLocal "sqlite-driver-src" { } ''
            ln -s ${wordpress-sqlite-anywhere} $out
          '';
          default = self.packages.${system}.wordpress-php83;
        };

        # Toolchain for site repos and platform development: `nix develop`.
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            nodejs_22
            wrangler
            gum
            skopeo
          ];
        };
      }
      // {
        # NOTE: one `checks` attrset — `//` is shallow, so a second
        # `// { checks.x = ...; }` would drop the earlier checks entirely.
        checks = {
          # Converter round-trip: data + MySQL type metadata fidelity.
          mysql-to-sqlite = import ./tools/test/mysql-to-sqlite-test.nix {
            inherit pkgs;
            converter = self.packages.${system}.mysql-to-sqlite;
          };
          # Key restoration on a plugin-mangled dump, then conversion.
          restore-core-keys = import ./tools/test/restore-core-keys-test.nix {
            inherit pkgs;
            restoreCoreKeys = self.packages.${system}.restore-core-keys;
            converter = self.packages.${system}.mysql-to-sqlite;
          };
          # Loader round-trip against a local tursodb sync server.
          sqlite-to-turso = import ./tools/test/sqlite-to-turso-test.nix {
            inherit pkgs;
            converter = self.packages.${system}.mysql-to-sqlite;
            loader = self.packages.${system}.sqlite-to-turso;
          };
        }
        // pkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
          # `nix build .#checks.<system>.module` runs the NixOS VM test (needs KVM).
          module = import ./tests/module.nix {
            inherit pkgs;
            wordpressModule = self.nixosModules.default;
          };
        };
      }
    );
}
