# The pinned WordPress core, extracted (fetchzip strips the leading
# "wordpress/" directory). One place to bump the platform's core version:
# the OCI image bake (modules/containers.nix) and the Worker-Assets static
# tree (lib/static-assets.nix) both build from this, so the image and the
# edge-served statics can never drift apart.
{
  pkgs,
  version ? null,
  hash ? null,
}:
let
  pinnedVersion = "7.0.1";
  pinnedHash = "sha256-vkzmfQpcj/qYT26PXi+V2ji/F5tKJhk0zZJ9QkHwQoY=";
in
pkgs.fetchzip {
  url = "https://wordpress.org/wordpress-${if version != null then version else pinnedVersion}.zip";
  hash = if hash != null then hash else pinnedHash;
}
