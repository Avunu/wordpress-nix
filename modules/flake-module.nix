# Top-level flake-parts module for wordpress-nix: what a site flake imports.
# flake.nix applies it to this flake (lib.modules.importApply), so `localFlake`
# is wordpress-nix itself, whatever the site calls its input.
#
#   outputs = inputs@{ wordpress-nix, ... }:
#     wordpress-nix.lib.mkFlake { inherit inputs; } {
#       imports = [ wordpress-nix.flakeModules.default ];
#       wordpress-nix.site = { name = "mysite"; src = ./.; };   # modules/site.nix
#     };
localFlake:
{ inputs, lib, ... }:
{
  imports = [
    inputs.devenv.flakeModule
    ./devenv.nix
    (lib.modules.importApply ./site.nix localFlake)
  ];
  # NOTE: ./nixos.nix is a standalone NixOS module surfaced as
  # flake.nixosModules.default by ../flake.nix; it is not a flake-parts module.
}
