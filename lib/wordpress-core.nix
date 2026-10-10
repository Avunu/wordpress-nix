# The pinned WordPress core, extracted (fetchzip strips the leading
# "wordpress/" directory). One place to bump the platform's core version:
# the OCI image bake (modules/containers.nix) builds from this, so every
# image is pinned to the same core.
{
  pkgs,
  version ? null,
  hash ? null,
}:
let
  pinnedVersion = "7.1";
  pinnedHash = "sha256-UcQwR1rtmJp0caeviabt/dGztSIIadMBb905vmweZCo=";
in
pkgs.fetchzip {
  url = "https://wordpress.org/wordpress-${if version != null then version else pinnedVersion}.zip";
  hash = if hash != null then hash else pinnedHash;
  passthru.version = if version != null then version else pinnedVersion;
}
