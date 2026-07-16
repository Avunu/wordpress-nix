# Bundle the platform edge Worker (worker/) into a single-file ESM module
# for `wrangler deploy`. The bundle is fully site-agnostic — one artifact
# serves every site at a given platform version — so site repos carry no
# Worker code at all; they get the bundle by building `.#worker` (or calling
# lib.mkSiteWorker) at their pinned wordpress-nix revision.
#
# Dependency strategy:
#   - @cloudflare/containers: zero-runtime-dependency npm package, vendored
#     by fetching its tarball (no lockfile machinery needed).
#   - @wp-sqlite/d1-proxy-worker: resolved by esbuild alias directly from
#     the sqlite-database-integration flake input — publishing it to npm is
#     unnecessary.
#   - cloudflare:* runtime modules stay external.
#
#   mkSiteWorker { inherit pkgs; }                      # the platform worker
#   mkSiteWorker { inherit pkgs; entry = ./my.js; }     # site escape hatch:
#     a custom entry module importing the platform modules for extra routes.
{
  pkgs,
  # The sqlite-database-integration source (the flake passes its input).
  sqliteDriverSrc,
  # Optional custom entry module (a path). Defaults to the platform worker.
  entry ? null,
}:
let
  containersNpm = pkgs.fetchzip {
    url = "https://registry.npmjs.org/@cloudflare/containers/-/containers-0.3.7.tgz";
    hash = "sha256-ehJQWGAwZDUMrs1nOOcXqls1K8S6driJOHb8ALFoS3g=";
  };
in
pkgs.runCommandLocal "wordpress-edge-worker"
  {
    nativeBuildInputs = [ pkgs.esbuild ];
    meta.description = "Bundled Cloudflare Worker for WordPress-on-Cloudflare sites";
  }
  ''
    mkdir -p build/node_modules/@cloudflare
    cp -r ${../worker} build/src
    chmod -R u+w build
    ln -s ${containersNpm} "build/node_modules/@cloudflare/containers"
    ${pkgs.lib.optionalString (entry != null) ''
      cp ${entry} build/src/site-entry.js
    ''}

    mkdir -p $out
    esbuild "build/src/${if entry != null then "site-entry.js" else "index.js"}" \
      --bundle \
      --format=esm \
      --platform=browser \
      --alias:@wp-sqlite/d1-proxy-worker=${sqliteDriverSrc}/packages/d1-proxy-worker/src/handler.js \
      "--external:cloudflare:*" \
      --outfile=$out/index.js
  ''
