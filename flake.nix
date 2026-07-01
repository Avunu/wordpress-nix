{
  description = "WordPress FrankenPHP — OCI images and a NixOS module (customizable PHP; state or git source)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs =
    { self, nixpkgs, flake-utils }:
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
      in
      {
        packages = {
          wordpress-php82 = mkImage pkgs.php82 "wordpress-php82";
          wordpress-php83 = mkImage pkgs.php83 "wordpress-php83";
          wordpress-php84 = mkImage pkgs.php84 "wordpress-php84";
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
