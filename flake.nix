{
  description = "WordPress FrankenPHP — OCI images and a NixOS module (customizable PHP; state or git source)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";

    # A fresh nixpkgs used only for the Rust toolchain building the native
    # PHP extensions (their dependency trees need a newer cargo than the
    # main pin provides).
    nixpkgs-rust.url = "github:NixOS/nixpkgs/nixos-unstable";

    # The SQLite Database Integration project with the Cloudflare D1 backend.
    # Fetched over git (not the GitHub tarball API): the project's
    # .gitattributes marks /packages as export-ignore for WordPress.org
    # release exports, which would exclude it from archive downloads.
    sqlite-database-integration = {
      url = "git+https://github.com/Avunu/sqlite-database-integration?ref=d1-support";
      flake = false;
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      nixpkgs-rust,
      sqlite-database-integration,
    }:
    {
      # Deploy WordPress directly on NixOS. See readme.md for usage.
      nixosModules.default = import ./modules/nixos.nix;
      nixosModules.wordpress-nix = import ./modules/nixos.nix;

      # Composable builders, reused by the container build and available to consumers.
      lib = {
        mkPhp = import ./lib/php.nix; # { pkgs, php ? pkgs.php83, optimize ? true, ... }
        mkFrankenphp = import ./lib/frankenphp.nix; # { pkgs, php }
        mkWordPressSite = import ./lib/site.nix; # { pkgs, src, php ? ..., plugins ? {}, themes ? {} }
      };
    }
    // flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        mkImage = php: imageName: import ./modules/containers.nix { inherit pkgs php imageName; };
        mkD1Image =
          php: imageName:
          import ./modules/containers.nix {
            inherit pkgs php imageName;
            d1DriverSrc = sqlite-database-integration;
            rustPkgs = nixpkgs-rust.legacyPackages.${system};
          };
      in
      {
        packages = {
          wordpress-php82 = mkImage pkgs.php82 "wordpress-php82";
          wordpress-php83 = mkImage pkgs.php83 "wordpress-php83";
          wordpress-php84 = mkImage pkgs.php84 "wordpress-php84";
          # Cloudflare D1 variants: bundle the SQLite Database Integration
          # plugin, the D1 db.php drop-in, and the native wp_mysql_parser +
          # wp_d1_client extensions. Configure with WP_D1_PROXY_URL.
          wordpress-d1-php83 = mkD1Image pkgs.php83 "wordpress-d1-php83";
          wordpress-d1-php84 = mkD1Image pkgs.php84 "wordpress-d1-php84";
          default = self.packages.${system}.wordpress-php83;
        };
      }
      // pkgs.lib.optionalAttrs pkgs.stdenv.isLinux {
        # `nix build .#checks.<system>.module` runs the NixOS VM test (needs KVM).
        checks.module = import ./tests/module.nix {
          inherit pkgs;
          wordpressModule = self.nixosModules.default;
        };
      }
    );
}
