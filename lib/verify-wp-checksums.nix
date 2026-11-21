# lib/verify-wp-checksums.nix — a built Bedrock tree against what wordpress.org
# published: core and wp-plugin/* by their checksum APIs, wp-theme/* by the
# locked zip. Fails on any PHP file that differs or that wordpress.org never
# shipped. Needs the network, so it runs as a CI step and as the site flake's
# app, never as a sandboxed check (tests/verify-wp-checksums.nix serves it
# fixtures instead).
#
#   nix run .#verify-wp-checksums                 # a site flake: its package
#   verify-wp-checksums --root result             # any built tree
{ pkgs }:
pkgs.writers.writePython3Bin "verify-wp-checksums" {
  libraries = [ ];
  flakeIgnore = [ "E501" ];
} (builtins.readFile ../tools/verify-wp-checksums.py)
