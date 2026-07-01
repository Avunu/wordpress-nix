# Optional helper: assemble a WordPress document root (in the Nix store) from a
# source tree plus declaratively-added plugins/themes. The result is meant to be
# passed as `services.wordpress-nix.source.path` for a "source-managed" (git)
# deployment.
#
# Users pointing the module at a plain repo (a flake input that already is a full
# document root) do NOT need this — they can pass the flake input directly. Reach
# for this only when you want to graft extra plugins/themes onto a base tree.
#
#   mkWordPressSite {
#     inherit pkgs;
#     src = inputs.my-wp;                 # base document root
#     plugins.woocommerce = inputs.woo;   # -> wp-content/plugins/woocommerce
#     themes.storefront   = inputs.sf;    # -> wp-content/themes/storefront
#   }
{
  pkgs,
  src,
  php ? (import ./php.nix { inherit pkgs; }),
  plugins ? { },
  themes ? { },
}:
let
  inherit (pkgs) lib;
  frankenphp = import ./frankenphp.nix { inherit pkgs php; };

  copyInto =
    dir: set:
    lib.concatStringsSep "\n" (
      lib.mapAttrsToList (name: s: ''
        mkdir -p "$out/wp-content/${dir}"
        cp -r ${s} "$out/wp-content/${dir}/${name}"
      '') set
    );
in
pkgs.runCommandLocal "wordpress-site"
  {
    passthru = { inherit php frankenphp; };
    meta.description = "Assembled WordPress document root";
  }
  ''
    mkdir -p "$out"
    cp -r ${src}/. "$out/"
    chmod -R u+w "$out"
    ${copyInto "plugins" plugins}
    ${copyInto "themes" themes}
  ''
