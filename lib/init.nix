# Bootstrapper for wordpress-nix sites — the `nix run` entry point.
#
# Produces a `wordpress-init` executable that scaffolds a new site in an empty
# directory, adopts an existing WordPress install in place, or reconciles a
# site that is already wordpress-nix's. The mode is detected from the target
# directory. The site it lays down is templates/site, the same tree
# `nix flake init -t …#site` hands out.
{ pkgs }:

let
  inherit (pkgs) lib;

  # Concatenated rather than sourced at runtime: writeShellApplication runs
  # shellcheck over the produced file, and a `source` would hide every
  # cross-file definition from it. main.sh must come last — it is the only file
  # with top-level code.
  sources = [
    ./sh/common.sh
    ./sh/detect.sh
    ./sh/scaffold.sh
    ./sh/main.sh
  ];
in
pkgs.writeShellApplication {
  name = "wordpress-init";
  # `nix` itself is deliberately not here: it is on the PATH of whoever ran
  # `nix run`, and the flake lock must be written by their nix.
  runtimeInputs = with pkgs; [
    git
    gum
    gawk
    gnused
    gnugrep
    coreutils
    findutils
    diffutils
  ];
  # The scripts are plain .sh files (no Nix-string escaping); bake the
  # template's store path in through a placeholder.
  text =
    builtins.replaceStrings
      [ "@TEMPLATE@" ]
      [ "${../templates/site}" ]
      (lib.concatMapStringsSep "\n" builtins.readFile sources);
}
