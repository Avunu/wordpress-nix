# Releases and the cluster

A site is deployed by the cluster (`nixos-cluster-orchestration`'s `wordpress`
kind, in the private cluster-ovh-01), never released. Each site flake is an
input of cluster-ovh-01 that follows the cluster's wordpress-nix:

```nix
# cluster-ovh-01/flake.nix
site-<slug> = {
  url = "git+https://github.com/Avunu/site-<slug>.git?ref=main";
  inputs.wordpress-nix.follows = "cluster/wordpress-nix";   # the site has no nixpkgs input
};
```

So production builds every site with the **cluster's** wordpress-nix and
nixpkgs, not the site's own `flake.lock`, which pins only what the site's CI and
dev shell use. The two package derivations differ; the vendor tree is shared,
because its output path depends only on its name and hash: site CI uploads it on
the merge to `main`, and the nodes substitute it.

## A site change

1. A PR merges to the site's `main` (a person's, or Dependabot's through
   [the pipeline](site-ci.md)).
2. The cluster-ci Worker dispatches cluster-ovh-01's `flake-update.yml`, which
   updates every branch-following input but those in `.github/flake-update-hold`.
3. `merge-gate.yml` builds every node closure, the vendor tree substituted from
   the cache, and auto-merges; nodes pull the new system and restart only the
   site's `wordpress-*` units, after `wordpress-migrate`.

If one site breaks the merge gate, `flake-update-split` opens one PR per input,
so the others merge, and proposes a hold for the broken one.

## A wordpress-nix release

In this order, and no other:

1. **wordpress-nix** releases `vX.Y.Z`.
2. **nixos-cluster-orchestration** bumps `wordpress-nix` in its `flake.lock`
   (library CI).
3. **cluster-ovh-01**: a hand-made PR bumps the `cluster` revision. Its merge
   gate builds every node closure, every site package included, and
   `checks.wordpress-site-smoke` boots a Bedrock site.
4. **Sites**, once that merged:

   ```sh
   gh workflow run rollout.yml -f version=vX.Y.Z            # every WORDPRESS_NIX_ROLLOUT_REPOS site
   gh workflow run rollout.yml -f version=vX.Y.Z -f repos=site-fixture
   ```

   `rollout.yml` checks that cluster-ovh-01 already builds with the release,
   then, per site, moves `inputs.wordpress-nix.url` to the tag, runs `nix flake
   update wordpress-nix`, `nix run .#relock` and `wordpress-nix sync`, and opens
   `chore(deps): wordpress-nix vX.Y.Z` on `standards/wordpress-nix-vX.Y.Z`,
   which auto-merges once the site's gates pass.

Within a major, `flakeModules.default` options and `lib.mkBedrockSite` arguments
only grow, with defaults, so the cluster's wordpress-nix always understands what
a rollout renders. The nightly `cluster-compat` job of `site-audit.yml` builds
each site against the cluster's pins and reports skew early.

The rollout's App is the environment `wordpress-nix-rollout`'s own
(`WORDPRESS_NIX_ROLLOUT_APP_ID` variable, `WORDPRESS_NIX_ROLLOUT_PRIVATE_KEY`
secret), with Contents, Pull requests and Workflows write on the site
repositories and Contents read on cluster-ovh-01. No other job sees it.

## Rollback

`git revert` in the site repository, which takes the same path. For an urgent
fix, revert the site's lock bump in cluster-ovh-01. If the database was
migrated, `cluster-restore <slug> --db-only` to the pre-migrate snapshot.
