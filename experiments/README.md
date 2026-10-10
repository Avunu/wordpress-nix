# Experiments

Code parked here is not part of the platform: nothing in `flake.nix`, the
NixOS module, the OCI images, or CI builds or tests it.

- `worker/` — the Cloudflare Worker prototype (formerly the top-level `worker/`),
  with its Nix bundler (`bundle.nix`) and static-asset tree (`static-assets.nix`).
  Its tests run only if you run them by hand (`cd experiments/worker && npm ci && npm test`).
