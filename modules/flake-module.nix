# Top-level flake-parts module for wordpress-nix: what a site flake imports.
#
#   outputs = { self, wordpress-nix, ... }@inputs:
#     wordpress-nix.lib.mkFlake { inherit inputs; } ({ ... }: {
#       imports = [ wordpress-nix.flakeModules.default ];
#       systems = [ "x86_64-linux" ];
#       perSystem = { wordpress-nix = { enable = true; siteName = "..."; }; };
#     });
{ inputs, ... }:
{
  imports = [
    inputs.devenv.flakeModule
    ./devenv.nix
  ];
  # NOTE: ./nixos.nix is a standalone NixOS module surfaced as
  # flake.nixosModules.default by ../flake.nix; it is not a flake-parts module.
}
