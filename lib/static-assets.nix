# The Worker-Assets static tree for a site: every static file WordPress can
# serve from the edge, drawn from the SAME pinned core as the OCI image
# (lib/wordpress-core.nix) plus the site's own wp-content — so the asset
# tree and the image can never drift apart.
#
# Deliberately excluded:
#   - wp-admin/** entirely: Worker Assets are served before the Worker runs,
#     so anything present here cannot be blocked by worker code, and wp-admin
#     must never be exposed on the frontend.
#   - all .php (and every extension not on the whitelist).
#   - .html/.txt/.xml: they would statically shadow WordPress's virtual
#     routes (sitemaps, robots.txt) and pretty permalinks.
#
#   mkStaticAssets { inherit pkgs; wpContent = ./wp-content; }
{
  pkgs,
  wpContent ? null,
  wordpressVersion ? null,
  wordpressHash ? null,
}:
let
  wordpressCore = import ./wordpress-core.nix {
    inherit pkgs;
    version = wordpressVersion;
    hash = wordpressHash;
  };

  staticExtensions = [
    "css"
    "js"
    "mjs"
    "map"
    "png"
    "jpg"
    "jpeg"
    "gif"
    "svg"
    "webp"
    "avif"
    "ico"
    "woff"
    "woff2"
    "ttf"
    "otf"
    "eot"
    "mp3"
    "mp4"
    "webm"
    "json"
  ];

  findExpr = pkgs.lib.concatMapStringsSep " -o " (ext: "-iname '*.${ext}'") staticExtensions;
in
pkgs.runCommandLocal "wordpress-static-assets"
  {
    meta.description = "WordPress static files for Cloudflare Worker Assets";
  }
  ''
    # Assemble the servable tree: core wp-includes + wp-content, with the
    # site's wp-content merged on top (site wins). Never wp-admin.
    mkdir -p tree/wp-content
    cp -rL ${wordpressCore}/wp-includes tree/wp-includes
    cp -rL ${wordpressCore}/wp-content/. tree/wp-content/
    chmod -R u+w tree
    ${pkgs.lib.optionalString (wpContent != null) ''
      cp -rL ${wpContent}/. tree/wp-content/
      chmod -R u+w tree
    ''}

    # Copy only whitelisted static files, preserving paths.
    cd tree
    find . -type f \( ${findExpr} \) | while IFS= read -r f; do
      mkdir -p "$out/$(dirname "$f")"
      cp "$f" "$out/$f"
    done

    # Belt and braces: the whitelist can never admit executable code.
    if find "$out" -type f \( -iname '*.php' -o -iname '*.phtml' \) | grep -q .; then
      echo "static-assets: refusing to package PHP files" >&2
      exit 1
    fi
  ''
