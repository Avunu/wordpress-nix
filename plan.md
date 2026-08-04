# Project Moonshot — split-plane WordPress hosting

> **Implementation status (2026-07-16)** — Phases 0–2 are implemented on branches:
> `wordpress-nix@moonshot` (Phase 0: mkSiteImage/mkStaticAssets/mkSiteWorker, WPCONF/salts/no-download,
> worker/ + 34 tests, reusable site-deploy workflow, templates.site, managed+d1 module modes),
> `wordpress-cloudflare@master` (Phase 1: thinned to payload+identity+pins; dry-run green;
> GUARD_ALLOW_WP_ADMIN=1 transition posture), `nixos-hosting-cloud@moonshot` (Phase 2 code:
> cloudflared cluster-ingress module, generic clusterApps.ingress option, mk-wordpress-backend,
> sites/ catalogue with pilot.nix.example; Traefik/Octavia removal + demo migration are
> deliberate flip steps in the pilot checklist, not yet performed; node evals green).
> Resolved VERIFYs: registry image ref accepted by wrangler deploy; nixpkgs services.cloudflared
> option shape; gitium GITIGNORE/GIT_KEY_FILE/origin. Outstanding: `wrangler containers push`
> command shape (first CI run), `wrangler d1 import` (first migration), cross-node routing
> (when node2 lands), jwt-auth JWT-less passthrough (pilot). Phase 3 (pilot) is next: the
> user-side checklist below, then merge the branches to main.

## Context

Stateful, monolithic WordPress hosting is a security liability: every site exposes wp-admin, a writable filesystem, and a database to the public internet. This plan establishes a new paradigm on top of the D1/Cloudflare work just completed: **each site splits into a stateful control plane and a stateless public frontend**, connected only through git, D1, and R2.

-   **Control plane (backend)**: full wp-admin + filesystem edits (plugin/theme installs, file editor) in a per-site NixOS container on the Avunu cluster (`nixos-container-orchestration`), reachable only at `<slug>.avunu.io` through a Cloudflare Tunnel gated by **Zero Trust Access**. Code changes are captured by **gitium** and pushed to the site's GitHub repo.
-   **Public frontend**: the existing Worker + container + D1 stack (proven in the `wordpress-cloudflare` PoC; the Worker source and deploy pipeline move into wordpress-nix as platform code, and the PoC becomes a thin site template). Merge to `main` → GitHub CI builds the site's image + assets + worker bundle with Nix → pushes to Cloudflare's registry → `wrangler deploy` → edge cache purge. wp-admin is **never exposed**; REST API and frontend sign-in remain.
-   **Shared state**: content = **one D1 database** (frontend in-Worker; backend via an authenticated `/__d1` proxy route), media = **R2** (`wp-cloud-files`), email = Cloudflare Email Sending API (`wordpress-cloudflare-email`, direct from PHP — no Worker needed), auth = `wordpress-jwt-auth` (proxy mode behind Access on the backend; OIDC email-PIN IdP Worker for frontend sign-in). Code = the site git repo. Attack surface per site: a token-gated D1 route + a Zero-Trust-gated tunnel.

**Decisions locked** (user-confirmed): ① backend shares the frontend's D1 via authenticated proxy (no sync machinery); ② direct CI deploy on merge to main (a dedicated `cloudflare` promotion branch remains a one-line trigger change later); ③ **one repo per site rooted at the WP root** — verified: gitium sets `GIT_DIR = dirname(WP_CONTENT_DIR)` and its hardcoded gitignore excludes core/`wp-config.php`/uploads/drop-ins/`/index.php`, so `flake.nix`, `wrangler.jsonc`, `.github/` live at the root untouched by WordPress; ④ **pilot site first**, bootstrap app extracted afterward; ⑤ **platform scripts are centrally managed in wordpress-nix, not copied into site repos** — the Worker source, deploy pipeline, image/asset builders, entrypoint, and mu-plugins all version with the wordpress-nix flake input; a site repo carries only its payload (`wp-content/`), its identity (`wrangler.jsonc`), its version pin (`flake.lock`), and a ~10-line CI caller. The template is a seed, not a source of truth: platform updates reach every site via a flake-input bump (automatable PR), never by re-syncing template files.

**Facts verified this session**: gitium honors `GIT_KEY_FILE`/`GIT_SSH` constants on all git calls and hardcodes remote `origin` (gitium/inc/class-git-wrapper.php:149-161, 274); the D1 drop-in (`db.copy`) reads `WP_D1_PROXY_URL` + `WP_D1_PROXY_TOKEN` (constant **or env**) and has a substitutable `{SQLITE_IMPLEMENTATION_FOLDER_PATH}`; the native client sends `Authorization: Bearer` when a token is configured; a pure-PHP HTTP transport fallback exists.

## Target architecture

```
            ┌─ site.com ──────────────────────────────────────────────────┐
Public ──→  │ Worker Assets (statics, no wp-admin) → guard (block wp-admin,│
            │ xmlrpc, wp-content/*.php) → edge page cache → container(WP,  │
            │ DISALLOW_FILE_MODS, cron off) → d1.internal → D1             │
            │ /__d1/v1/* (bearer) ─────────────┐    /__cache/purge (bearer)│
            └──────────────────────────────────┼──────────────────────────┘
                                               │ same D1, session bookmarks
            ┌─ slug.avunu.io ─ CF Access ─ cloudflared tunnel (replicated, ─┐
Operators ─→│ ingress generated from sites/*.nix, direct to container IP): │
            │ nspawn container: FrankenPHP + pinned store core (symlinks) + │
            │ mutable wp-content on JuiceFS + gitium (.git at WP root) +    │
            │ jwt-auth proxy mode + wp-cron owner + plugin installs         │
            └── git push ─→ GitHub main ─→ CI: nix build image + assets ───→│
                              Cloudflare registry → wrangler deploy → purge ┘
Media: wp-cloud-files → R2 (both planes, same S3_PUBLIC_URL). Email: CF Email API (both).
```

## Phase 0 — Platform library work (prerequisites for the pilot)

### 0a. wordpress-nix: image/asset builders (`/home/batonac/Development/wordpress-nix`)

1.  **Export `lib.mkSiteImage`** — refactor `modules/containers.nix` + the flake-internal `mkImage`/`mkD1Image` into an exported function: `{ pkgs, php ? "php84", imageName, wpContent ? null, plugins ? {}, themes ? {}, d1 ? true, configExtra ? "" }`. Graft `wpContent` (the site repo's `wp-content/`) over the baked core using `lib/site.nix`'s grafting approach. Existing `packages.*` re-expressed via `mkSiteImage` (`wpContent = null`) — byte-compatible, `oci.yml` untouched.
2.  **Export `lib.mkStaticAssets`** (new `lib/static-assets.nix`): builds the Worker-Assets `public/` tree from the same pinned core + site wp-content — extension whitelist (css/js/images/fonts/etc.), **never `.php`, exclude `wp-admin/**` entirely** (assets serve before the Worker runs, so wp-admin must not exist there). Replaces `scripts/build-assets.sh`'s curl-a-zip approach; guarantees asset tree == image tree.
3.  **`conf/wp-config.php`**: add a generic `WPCONF_<NAME>` env→`define()` mapping (with `true`/`false` coercion) so all plugin constants (S3\_\*, CLOUDFLARE\_EMAIL\_\*, JWT\_AUTH\_\*, DISALLOW\_FILE\_MODS, DISABLE\_WP\_CRON) flow through one mechanism; keep `WORDPRESS_CONFIG_EXTRA` as escape hatch.
4.  **`docker-entrypoint.sh`**: salts from a `WORDPRESS_SALTS` env/secret (the 8-define api.wordpress.org block; validate 8 defines) with local `/dev/urandom` generation as dev-only fallback — **delete the api.wordpress.org fetch**; **delete the `WORDPRESS_SOURCE_URL` download path** (hard-fail if baked core missing) — per the "everything in git/Nix, no runtime downloads" principle.
5.  Rename shipped mu-plugins → `platform-nonce.php`, `platform-cloudflare-page-cache.php` (enables the `wp-content/mu-plugins/platform-*.php` gitignore rule so gitium never commits them; both planes get them from the platform).

### 0b. wordpress-nix: NixOS module backend role (`modules/nixos.nix`)

1.  **`source.type = "managed"`** (new mode alongside `state`/`git`): WP root = `stateDir` (JuiceFS-backed); **core as symlinks into a pinned store docroot** (wp-config.php generated into the store docroot defines `WP_CONTENT_DIR`/`WP_CONTENT_URL` → stateDir, same resolution trick as existing git mode); wp-content fully mutable, **file mods allowed**; options `source.siteRepo.{url,branch,deployKeyFile}`. `wordpress-init` seeds on first boot: `git init -b main && git remote add origin … && git fetch && git checkout -f main` using the deploy key (`GIT_SSH_COMMAND`), then (re)points core symlinks (`ln -sfn`, handles core bumps), materializes salts, generates the db.php drop-in. Git-safety: symlinked core paths are all in gitium's hardcoded gitignore (verified), so store paths never enter the index; the site template's committed `.gitignore` duplicates them belt-and-braces.
2.  **`database.type = "d1"`**: options `d1.{proxyUrl, tokenFile, requestTimeoutMs, connectTimeoutMs}`. Skips MariaDB entirely (assert `!createLocally`). Wires native extensions via existing `lib/php-extensions.nix` + the flake's `sqlite-database-integration` input; sets `PHP_INI_SCAN_DIR = "${phpBuild}/lib:${extensions.iniDir}"` on **both** the FrankenPHP service and the cron unit (mirror of containers.nix). Generates `wp-content/db.php` from `db.copy` with `{SQLITE_IMPLEMENTATION_FOLDER_PATH}` substituted to the store copy (drop-in is gitium-ignored, regenerated each boot). wp-config: `WP_D1_PROXY_URL` define (non-secret), `WP_D1_PROXY_TOKEN` read at runtime from `tokenFile` (`/run/agenix/<slug>-d1-proxy-token` — never in the store); placeholder `DB_NAME`/`DB_USER`/`DB_HOST` defines.
3.  **Backend-role config**: `saltsFile` option (agenix path; materialized to `/run/wordpress/wp-salts.php` mode 0400; replaces self-generation — same salts as the frontend's `WORDPRESS_SALTS` secret); `AUTOMATIC_UPDATER_DISABLED`, `WP_AUTO_UPDATE_CORE=false` (core comes from the flake pin), `DISABLE_WP_CRON=true` (systemd timer owns cron), `WP_HOME`/`WP_SITEURL = https://<slug>.avunu.io` (constants override the frontend siteurl stored in shared D1); `git`, `openssh`, `bash`, `coreutils` on service+cron unit PATH for gitium; assert `proc_open` not in `disable_functions`.

### 0c. wordpress-nix: the edge Worker, centrally owned (`worker/` — new)

The Worker code is already fully site-agnostic (all identity comes from `env` vars/secrets — verified), so no site ever needs its own copy. Move it from `wordpress-cloudflare/src/` into **`wordpress-nix/worker/`** and treat it like the mu-plugins and entrypoint: platform code, versioned by the flake.

1.  **Modules**: `worker/{index.js,cache.js,guard.js,auth.js}` + `worker/test/` (the 17 existing cache tests + new guard/`__d1` tests) + `worker/package.json`.
    -   `guard.js` (runs before cache): `/wp-admin/{admin-ajax,admin-post}.php` pass through uncached; **all other `/wp-admin/*` → 302 to `${env.BACKEND_ADMIN_URL}`**; `/wp-login.php` pass through (jwt-auth OIDC frontend sign-in; password auth is blocked by the plugin anyway); `/wp-json/*` pass through except anonymous `wp/v2/users` → 401; `/xmlrpc.php` → 403; `/wp-trackback.php`, `/wp-signup.php`, `/wp-activate.php`, external `/wp-cron.php` → 404; **`/wp-content/**/*.php`, `/wp-includes/**/*.php` → 404**. `auth.js`: timing-safe bearer check extracted from cache.js's `handlePurge`.
    -   **`/__d1/v1/*` route** (decision: on the site Worker, no separate Worker — mirrors `/__cache/purge`): bearer check vs `env.D1_PROXY_TOKEN` → 401; 1 MiB body cap → 413; strip `/__d1` prefix → `createD1ProxyHandler(() => env.DB)` (no `allowInsecure`; D1 session bookmarks flow through unchanged). The container's `d1.internal` outbound path is untouched (**keep the `outboundByHost` assignment form** — the static-field shadowing footgun).
2.  **`lib.mkSiteWorker`** (new `lib/worker.nix`): nix-builds a single-file ESM bundle `dist/index.js` via esbuild — `@cloudflare/containers` vendored via `buildNpmPackage`/`npmDepsHash`; `@wp-sqlite/d1-proxy-worker` resolved by esbuild alias **directly from the existing `sqlite-database-integration` flake input** (`${input}/packages/d1-proxy-worker/src/…`) — which makes publishing that package to npm **optional**, not a prerequisite; `cloudflare:*` kept external. One bundle serves every site at a given platform version. Escape hatch for a site needing custom routes: `mkSiteWorker { entry = ./worker-extra.js; }` importing the platform modules.
3.  **Reusable deploy pipeline**: `.github/workflows/site-deploy.yml` in this repo with `on: workflow_call` — site repos invoke it with `uses: Avunu/wordpress/.github/workflows/site-deploy.yml@main` (+ `secrets: inherit`). Pipeline fixes propagate to all sites without touching site repos (pin to a tag instead of `@main` once the fleet grows). Steps: nix build `.#image` + `.#static-assets` + `.#worker` → `docker load` → `wrangler containers push <name>:${SHA::12}` (VERIFY exact wrangler 4.x command/ref format) → substitute the image ref into wrangler.jsonc → `wrangler deploy` → `curl POST /__cache/purge`. `concurrency: cancel-in-progress` per site (collapses gitium push storms — last one wins); `workflow_dispatch(ref)` for **rollback** (rebuild at any prior commit — reproducible from flake.lock; D1 doesn't roll back with code → D1 Time Travel is the data undo).
4.  **Update propagation to sites**: the site's `flake.lock` is the version pin and the deliberate-rollout lever — one bump updates WP core, PHP, native extensions, D1 driver, mu-plugins, entrypoint, image, static assets, AND worker logic atomically. Automate with a scheduled flake-lock-bump PR job (e.g. `DeterminateSystems/update-flake-lock`) included in the reusable workflow; merging the PR deploys the new platform. CI-pipeline changes propagate independently via the `workflow_call` ref.
5.  **`templates.site` flake output** (`nix flake init -t github:Avunu/wordpress#site`) + `devShells` export (wrangler, node, gum) so site repos don't manage their own toolchain. Worker vitest runs in **this repo's CI** (plain npm job, like the sqlite repo's) — tests run once centrally, not N times.

### 0d. sqlite-database-integration (`d1-support` branch)

1.  Optional (demoted by 0c.2): publish `@wp-sqlite/d1-proxy-worker` to npm; the worker bundle consumes it from the flake input either way.
2.  Optional: `authorize(request)` option on `createD1ProxyHandler` (auth lives in the `/__d1` wrapper otherwise).

## Phase 1 — Site template, thin (evolve `/home/batonac/Development/wordpress-cloudflare` → `Avunu/wordpress-site-template`)

With 0c in place the template carries **no scripts** — only payload, identity, and pins (drift surface ≈ four small files):

1.  **Repo layout** (repo root = WP root): `wp-content/{plugins,themes,mu-plugins}` (the payload — seeded with companion plugins **gitium, wp-cloud-files, wordpress-cloudflare-email, wordpress-jwt-auth** as normal git-tracked plugins; they self-update on the backend → gitium commit → CI → frontend converges; drop-ins + D1 machinery stay image-baked) · `flake.nix` (~20 lines: input `wordpress-nix`; outputs `packages.{image,static-assets,worker}` = thin `lib.mkSiteImage`/`mkStaticAssets`/`mkSiteWorker` calls with `wpContent = ./wp-content`) · `flake.lock` (the platform pin) · `wrangler.jsonc` (identity only) · `.github/workflows/deploy.yml` (~10-line `workflow_call` caller) · `.gitignore` (gitium's rules + `public/ result .worker/ .wrangler/ .dev.vars wp-content/mu-plugins/platform-*.php wp-content/db.php`). No `package.json`, no `worker/`, no `test/`, no scripts.
2.  **`wrangler.jsonc`** is the one genuinely per-site file (wrangler needs a concrete config; scaffolded once by template/bootstrap, thereafter site-owned): worker name, `database_id`, KV id, routes/custom domain, `WORDPRESS_URL`, `BACKEND_ADMIN_URL`, `WPCONF_*` vars (`WPCONF_DISALLOW_FILE_MODS=true`, `WPCONF_DISABLE_WP_CRON=true` for the frontend plane); `main` → the nix-built worker bundle (CI/`nix build .#worker -o .worker` symlink); `containers[].image = "__IMAGE_REF__"` placeholder substituted by the reusable workflow (no Dockerfile, no deploy-time docker build).
3.  **Local dev**: `nix develop` (platform devShell) → `nix build .#worker -o .worker && wrangler dev` — Worker + container + miniflare-local D1.
4.  GH secrets per site: `CLOUDFLARE_API_TOKEN`, `CACHE_PURGE_SECRET`; var `CLOUDFLARE_ACCOUNT_ID` (consumed by the reusable workflow via `secrets: inherit`).

## Phase 2 — Orchestration (backend plane) (`/home/batonac/Development/nixos-container-orchestration`)

1.  **`modules/cloudflared.nix` — tunnels REPLACE Traefik as cluster ingress** (decision: no local proxy layer at all; TLS at the edge, Access enforced before traffic reaches the origin, and the WAN firewall closes to SSH-only): **one cluster-wide tunnel (`cluster-ingress`), replicated on every node** — each node runs an identical `cloudflared` connector with the same tunnel ID + credentials (agenix `cloudflared-cluster`, encrypted to all node keys). The **ingress table is Nix-aggregated from every app's ingress declaration** (item 2), default 404 — no discovery layer needed because container IPs are statically declared and travel with the app to whatever node runs it. Cloudflare spreads requests across healthy connectors and sheds a node's on failure — **multi-node = add a replica; app moves between nodes need zero Cloudflare changes** (cloudflared reaches container IPs on the local br-app or cross-node via the overlay's app-VLAN routes; an off-home-node hop over the vRack is negligible). DNS: wildcard CNAME `*.avunu.io → <tunnel-id>.cfargotunnel.com` for admin/platform hostnames + per-zone CNAMEs for public workload domains (Access apps stay per-hostname for per-site policy/aud). Trade-offs accepted: one shared tunnel credential (Access authenticates at the edge *before* the tunnel for gated hostnames, jwt-auth validates in-app; document rotation); Cloudflare becomes the sole ingress path (public frontends already are Cloudflare-or-nothing; keep Octavia re-enablement as documented break-glass); one hostname → one origin URL (the day a service needs N replicas behind one hostname, reintroduce a thin proxy for that service or use CF Load Balancer); Cloudflare proxy semantics apply to all migrated workloads (~100 MB bodies, ~100 s requests). **Fallback if cross-node routing disappoints**: per-node tunnels with per-app CNAME pinning (DNS update on app moves — automatable in bootstrap). Monitor cloudflared via its Prometheus metrics endpoint (scrape from the existing Prometheus).
2.  **Generic per-app ingress option, superseding Traefik `service.tags`** (`modules/app-container.nix` + `lib/mk-app-container.nix`): new `clusterApps.<name>.ingress = { "<hostname>" = { port = 80; path = "/"; }; }` (or hostname → URL shorthand). `modules/cloudflared.nix` aggregates all apps' declarations into the connector config; Nomad service registration stays for lifecycle/observability but `traefik.*` tags are removed from the schema. WordPress backends (item 5) and every other workload declare ingress the same way — this is the orchestration project's ingress API from now on.
3.  **Migrate existing workloads, then remove Traefik + Octavia**: convert `demo` (and `littlecocalico` when it lands) to `ingress` declarations as public (non-Access) tunnel hostnames — VERIFY their domains are zones on the CF account and check Frappe attachment sizes against the ~100 MB proxy body limit. Once each serves correctly through the tunnel: delete `modules/ingress.nix` (Traefik) from the module set, drop 80/443 from the WAN nftables allowlist in `modules/overlay-network.nix` (leaving SSH + ICMP), decommission the OVH Octavia LB (manual OVH step), and update `docs/ovh-provisioning-runbook.md` (§5 currently documents Octavia setup) to the tunnel-based flow.
4.  **Zero Trust Access** (pilot = documented dashboard steps; bootstrap automates later via API): app for `<slug>.avunu.io` (staff allow, 24 h session; record the **aud tag**), Bypass app for the **path** `/gitium-webhook.php` (GitHub webhooks can't present Access creds; gitium's own `?key=` remains the auth for that path — **Access application paths cannot match query strings**, so the bypass is path-scoped only, and a bypass is a *separate, more-specific Access application*, not a policy on the main app; VERIFY jwt-auth proxy mode passes JWT-less requests through — fallback: skip the webhook, gitium reconciles on its next push). In-container: jwt-auth **proxy mode** — `JWT_AUTH_JWKS_URI=https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`, `JWT_AUTH_AUD=<aud>`, cookie `CF_Authorization`, `JWT_AUTH_DEFAULT_ROLE=administrator` (staff-only policy for the pilot; switch to claim-mapped roles before client access), `JWT_AUTH_LOGOUT_URL=/cdn-cgi/access/logout`. Break-glass: wp-cli is exempt from the password-login block (`nixos-container run <slug> -- wp …`).
5.  **`lib/mk-wordpress-backend.nix` + `modules/sites.nix`** (readDir-imports `sites/<slug>.nix` param files — no more hand-editing workloads.nix): expands slug/domains/node/address/repoUrl/aud/R2/email params into the full `clusterApps.<slug>` entry — wordpress-nix module import (managed source + d1 database + saltsFile + configExtra with the JWT/S3/email/cache-purge defines), `juicefsPaths."/var/lib/wordpress" = "clients/<slug>/wordpress"`, **no mysql localState**, secrets list, and the generic `ingress = { "<slug>.avunu.io" = { port = 80; }; }` declaration from item 2 — WordPress backends are just another consumer of the cluster ingress API.
6.  **Per-site agenix secrets** (via existing `secrets` gum TUI; frontend twins noted): `<slug>-wp-salts` (= Worker `WORDPRESS_SALTS`), `<slug>-d1-proxy-token` (= `D1_PROXY_TOKEN`), `<slug>-cache-purge-secret` (= `CACHE_PURGE_SECRET`), `<slug>-git-deploy-key` (ed25519; pub half = GitHub deploy key w/ write; wp-config `GIT_KEY_FILE` define — gitium honors it, verified), `<slug>-r2-media-key/-secret`, `<slug>-cf-email-token`, `<slug>-gitium-webhook-key`.
7.  **Backend owns wp-cron** (`cron.enable = true`; frontend cron off; the wp-cron systemd timer gets the same PHP\_INI\_SCAN\_DIR + PATH as the web unit).

## Phase 3 — Pilot site (manual, end-to-end)

1.  Land Phases 0–2; bump orchestration's wordpress-nix input (same rev as the site repo's pin — add a CI same-rev check later).
2.  `cloudflared tunnel create cluster-ingress` → agenix `cloudflared-cluster` (encrypt to all node keys); wildcard CNAME `*.avunu.io → <tunnel-id>.cfargotunnel.com`; migrate `demo` to a tunnel ingress declaration and confirm it serves, then remove Traefik/Octavia + close 80/443 (Phase 2 item 3) — the pilot runs on the Traefik-free cluster; `gh repo create Avunu/site-pilot --template Avunu/wordpress-site-template`; deploy key → GitHub + agenix.
3.  `wrangler d1 create site-pilot`, `wrangler kv namespace create`, `wrangler r2 bucket create site-pilot-media` (all covered by the existing wrangler OAuth login), per-site R2 S3 keys + Email Sending token via REST with the provisioning token; stamp wrangler.jsonc; `wrangler secret bulk` × (WORDPRESS\_SALTS, D1\_PROXY\_TOKEN, CACHE\_PURGE\_SECRET, WPCONF\_S3\_KEY/SECRET, WPCONF\_CLOUDFLARE\_EMAIL\_API\_TOKEN); GH secrets. (Phase 4's `moonshot` automates this whole step.)
4.  Access app + webhook bypass (record aud) — hostname already resolves via the wildcard CNAME; write `sites/pilot.nix`; push; wait for pull-deploy.
5.  **Deploy frontend first** (the `/__d1` route must exist), then `nixos-container run pilot -- wp core install --url=https://pilot.avunu.io …` — proves backend→D1 end-to-end. Seed gitium (`wp option update gitium_webhook_key`, GitHub webhook).
6.  Smoke: Access login → wp-admin auto-provisioned admin; **install a test plugin → gitium commit+push → CI image build → frontend redeploy → cache purge** (the whole loop); media upload → R2 URL; wp\_mail; cron; guard matrix on the frontend (wp-admin 302 → backend, xmlrpc 403, wp-content php 404); `curl /__d1/v1/query` with/without token. Record admin page latency (see Risks).

## Phase 4 — `moonshot`: site bootstrap + migration tooling

The per-site Cloudflare/GitHub/DNS choreography is the most arduous part of adopting this
paradigm, so it gets a real tool: **`moonshot`**, a gum-based TUI shipped as a Nix app in the
orchestration repo (`nix run .#moonshot`, plus a devShell command, following the existing
`secrets` TUI precedent). Nix packaging is not optional here — the workflow needs `gum`,
`sqlite3`, `mysql`, `wp`, `rclone`, `wrangler`, `gh`, `cloudflared`, `jq`, `ssh`, and a
driver-enabled PHP, several of which are absent from a stock dev machine. PHP-side work is a
separate wordpress-nix app (`nix run github:Avunu/wordpress#mysql-to-sqlite`) so the tool stays
a thin orchestrator.

### Verified capability matrix (probed 2026-08-03 against live Cloudflare)

| Capability | How | Verified |
| --- | --- | --- |
| D1 create/query, KV create, R2 **bucket** create, Workers deploy, containers push, `secret bulk` | **wrangler, existing OAuth login** — no token needed | ✅ REST + `r2 bucket list` succeeded on the OAuth token |
| Custom domain → **DNS record + TLS cert created automatically** by `wrangler deploy` | wrangler | ✅ documented; fails if a record already exists at that hostname (always true for a migration → must delete first) |
| Zero Trust Access apps/policies | **REST only** (`/accounts/{id}/access/apps`) | ✅ wrangler OAuth **rejected** ("Authentication error"); no `wrangler access` command exists |
| DNS record create/delete (cutover, wildcard tunnel CNAME) | **REST only** | ✅ wrangler OAuth **rejected** |
| R2 S3 access keys | **REST** `POST /accounts/{id}/tokens`; **secret = SHA-256 of the token value** | ✅ wrangler OAuth **rejected** on `/user/tokens/permission_groups` |
| Bulk media upload | **`rclone copy`** to the S3 endpoint | wrangler is one-object-only (315 MB cap); Super Slurper is **cloud-source-only** (no local dir) — has a REST API but doesn't fit a legacy-host migration |
| SQL import | **`wrangler d1 execute --remote --file`** (staged upload + polling, **5 GB** limit) | ⚠️ **`wrangler d1 import` does not exist** — corrects the earlier plan |

**"OAuth for all" verdict**: it is Cloudflare acting as an OAuth *provider* (self-managed clients,
Authorization Code + PKCE, public/CLI clients supported), and it *can* mint user-scoped tokens
carrying `access:write`/`dns_records:edit`. But `wrangler login`'s own 29-scope catalogue omits
Access, DNS, and token-minting, and registering a custom client itself requires a token or the
dashboard — so **OAuth does not remove the bootstrap, it only improves credential hygiene**.
Decision: **one provisioning API token** (below). Revisit OAuth if we ever provision into
*client-owned* Cloudflare accounts, where delegated consent is the only sane model.

### Credential model

- **wrangler's existing OAuth login** does ~85% of the work, unchanged and unmanaged by us.
- **One provisioning API token** in agenix as `cloudflare-provision-token`, used *only* by
  `moonshot` for the three gaps: Access apps, DNS records, and per-site R2 key minting.
  Scopes: `Access: Apps and Policies Write`, `DNS Write` (zone), `API Tokens Write`,
  `Account Settings Read`, `Zone Read`.
  ⚠️ **`API Tokens Write` is privilege-escalating** (a token that mints tokens) — the accepted
  cost of per-site R2 credential isolation. Mitigations: scope the token to the specific
  accounts we host, keep it out of CI (operator workstation + agenix only), and rotate on
  operator change. `moonshot doctor` verifies scope coverage by probing each endpoint.
- **Per-site R2 keys**: `POST /accounts/{id}/tokens` with a bucket-scoped
  `Workers R2 Storage Bucket Item Write` policy (resolve permission-group IDs at runtime via
  `/user/tokens/permission_groups`, never hardcode). Access Key ID = token `id`;
  **Secret Access Key = SHA-256 of the token `value`**. A leaked site key exposes one bucket.
- **Multi-account is a first-class requirement**: the operator has 5 Cloudflare accounts
  (client-owned brands). Every site carries its `accountId`; `moonshot` sets
  `CLOUDFLARE_ACCOUNT_ID` per invocation and never relies on a default.

### Site configuration as JSON (adopting the nixos-router pattern)

Replace the `sites/<slug>.nix` parameter files from Phase 2 with **`sites/<slug>.json`**, read by
`modules/sites.nix` via `builtins.fromJSON` and applied with **`lib.mkDefault`** — exactly the
`nixos-router/local/router-settings.json` pattern. An optional `sites/<slug>.nix` overlay stays
available for locked or non-serializable settings (extra modules, packages), and anything set
there wins over the JSON. Rationale: the tool reads and writes site config with `jq` instead of
generating Nix, config is diffable/inspectable by any tooling, and a future web UI can edit the
same file — while Nix remains the thing that *deploys* it.

Two files per site, one in each plane, both written from the tool's single in-memory answer set
(no generated-file indirection): **`wrangler.jsonc`** in the site repo (frontend identity: worker
name, D1/KV IDs, URLs, `WPCONF_*`) and **`sites/<slug>.json`** in the orchestration repo (backend
params: node, address, adminHost, accessAud, R2/email config). Overlap is deliberately small
(slug, hostnames, accountId).

### The database path (highest-fidelity conversion)

The driver *is* the MySQL→SQLite translator, so the conversion runs **through the driver** rather
than through a generic converter — every statement passes the same translation layer that will
serve the site at runtime:

1. On the source host: `wp db export` (or `mysqldump --single-transaction --no-tablespaces`),
   after a `wp search-replace` dry-run report of URL changes.
2. `nix run github:Avunu/wordpress#mysql-to-sqlite -- dump.sql out.sqlite` — a PHP CLI harness
   that boots `WP_MySQL_On_SQLite` against a fresh local SQLite file and replays the dump
   statement-by-statement, splitting statements with the project's own **MySQL parser** (safe
   across quoted strings and delimiters). Result: canonical schema *and* populated
   `information_schema` shadow tables, with exact type fidelity for plugin tables too.
   Safety net: the driver already ships
   `WP_SQLite_Information_Schema_Reconstructor::ensure_correct_information_schema()`, which the
   configurator calls automatically on first connect and which rebuilds metadata from whatever
   tables physically exist (authoritative for core tables via `wp_get_db_schema()`, inferred for
   plugin tables) — so a partial conversion self-heals rather than bricking the site.
3. `sqlite3 out.sqlite .dump` → **strip `BEGIN TRANSACTION`/`COMMIT`**, drop any `_cf_*`/`sqlite_sequence`
   noise, prepend `PRAGMA defer_foreign_keys = true;` → `wrangler d1 execute <db> --remote --file`.
4. Preflight gates before touching D1: **10 GB** database ceiling, **5 GB** file-import ceiling,
   **100 KB** per-statement ceiling (split or fail loudly), and a `d1 time-travel info` bookmark
   captured first — the free rollback for the load step.

### The media path

`rclone copy` from the source host's `wp-content/uploads` straight to the R2 bucket over the S3
endpoint (`https://<account>.r2.cloudflarestorage.com`), using the freshly minted per-site key.
Handles tens of GB of small files with resume and `--checksum` re-runs. The tool then verifies a
sample of objects over the public URL before rewriting any URLs. Super Slurper is only wired in
if the source media already lives in S3/GCS/Spaces (its REST API is documented in the research if
that case arises).

### Command surface

```
moonshot doctor                    # tool + auth + token-scope probes (each gap endpoint)
moonshot site new <slug>           # greenfield site, all planes
moonshot site migrate <slug>       # the full pipeline below (resumable)
moonshot site cutover <slug>       # DNS flip — deliberate, separate, reversible
moonshot site status|list|destroy <slug>
moonshot secrets                   # existing agenix TUI (unchanged)
```

Every run is a **resumable state machine**: answers and step results live in
`.moonshot/<slug>.state.json`, each step is idempotent and re-runnable, and a failed step resumes
in place rather than restarting the migration. Long steps (rclone, D1 load) stream progress
through gum.

### `site migrate` — step order

1. **Preflight** — `doctor` checks; gum interview (slug, source SSH target, domains, target
   Cloudflare account from the 5, node, backend hostname); write state file.
2. **Survey the source over SSH** — locate the WP root, read `wp-config.php` for DB credentials,
   `wp core version`, `wp plugin list`, uploads size, and a `wp search-replace --dry-run` URL
   report. Print a go/no-go summary (D1 ceiling, upload volume, PHP version, oddities like
   multisite or must-use plugins).
3. **Pull code** — rsync `wp-content` (themes/plugins/mu-plugins/languages, **excluding
   uploads/cache/upgrade**) into a scratch tree.
4. **Assemble the site repo** — `nix flake init -t github:Avunu/wordpress#site`, drop in the
   pulled `wp-content`, add the companion plugins (gitium, wp-cloud-files,
   wordpress-cloudflare-email, wordpress-jwt-auth) at pinned releases, stamp `wrangler.jsonc`,
   `gh repo create` + push, generate the ed25519 deploy key → GitHub deploy key + agenix.
5. **Provision Cloudflare** (wrangler, per-account) — D1 database, KV namespace, R2 bucket;
   mint the per-site R2 token (REST); generate salts / D1 proxy token / cache purge secret /
   gitium webhook key once and write **both** twins: `wrangler secret bulk` (stdin JSON) for the
   Worker and agenix for the backend.
6. **Migrate the database** — the four-step path above.
7. **Migrate media** — rclone to R2; verify samples.
8. **Deploy the frontend** — GitHub Actions runs the reusable pipeline (or a local
   `nix build`+`wrangler deploy` for the first run), on the **staging hostname**; the `/__d1`
   route must exist before the backend can boot.
9. **Provision the backend** — write `sites/<slug>.json`, create the Access application (+ the
   separate path-scoped **bypass app** for `/gitium-webhook.php`; note Access paths **do not
   support query strings**, so bypass `/wp-json/` style paths, never `?rest_route=`), record the
   `aud` tag into the JSON, commit and push the orchestration repo, wait for pull-deploy, then
   seed gitium options over `nixos-container run`.
10. **Verify** — automated smoke: staging homepage 200 + `CF-Cache-Status` MISS→HIT, admin login
    through Access, a media URL resolving from R2, `/__d1` authenticated round trip, guard matrix
    (wp-admin 302, xmlrpc 403, wp-content PHP 404), and a wp-cli row-count diff between source
    MySQL and D1 per table. Print the report; **stop here**.

### Cutover (separate command, reversible)

`moonshot site cutover <slug>` is the only step that touches live traffic: lower the source
domain's DNS TTL beforehand, re-run the delta sync (rclone `--checksum` + an incremental DB
re-load from a fresh dump, since the source stayed live), delete the pre-existing DNS record
(required — `custom_domain` refuses an existing CNAME), add the custom-domain route,
`wrangler deploy`, flip `WORDPRESS_URL`/`WP_HOME`, run `wp search-replace` on D1, purge the edge
cache, and re-run the smoke suite against the real domain. **Rollback** = re-point DNS at the
untouched source host; the old site is never modified or decommissioned by the tool.

### Work items

- **wordpress-nix**: `packages.mysql-to-sqlite` (PHP CLI harness + driver + parser, exposed as a
  flake app); extend the site template with a `site.json`-shaped answer file if useful.
- **nixos-hosting-cloud**: `packages.moonshot` + `apps.moonshot` (bash + gum, all deps pinned);
  convert `modules/sites.nix` to `fromJSON` + `mkDefault` with the optional `.nix` overlay;
  `sites/pilot.json.example`; agenix `cloudflare-provision-token`; extract the shared agenix
  helpers out of `scripts/secrets.sh` for reuse.
- **Docs**: a one-time setup runbook (provisioning token scopes, the 5 accounts, wildcard CNAME)
  and a per-site migration runbook generated by `moonshot doctor`.

## Risks & mitigations (accepted posture)

-   **Version skew** (plugin updated on backend migrates shared D1; frontend runs old code for one CI cycle ~2–5 min): edge cache shields most anon traffic; WP migrations are overwhelmingly additive; CI post-deploy purge is the correctness anchor. Optional later: KV `maintenance` flag (misses get 503, hits still serve) for risky upgrades. Core bumps: flake pin on both planes, `wp core update-db` once on the backend.
-   **D1-over-WAN admin latency**: ~5–15 ms × dozens of serialized queries ≈ 0.3–1.5 s/page — tolerable for admin; mitigated by the native client's connection pool + APCu object cache (both already built). Measure at the pilot before optimizing.
-   **JuiceFS PHP file ops**: opcache with `revalidate_freq=60` (WP invalidates on upgrade), realpath cache, JuiceFS local cache-dir. gitium's `.git` on JuiceFS is fine at code-repo scale; `--separate-git-dir` onto local ZFS is a later optimization (VERIFY gitium tolerates a `.git` gitfile).
-   **gitium sweep**: gitium tracks the whole root minus ignores, so `flake.nix`/`wrangler.jsonc` edits merged via its webhook land in the backend checkout — harmless (inert there), but its auto-commits may bundle them; acceptable. The thin-repo design (0c/Phase 1) keeps this surface to ~four files.
-   **Sessions**: shared salts, but `COOKIEHASH = md5(siteurl)` differs per plane → cookies are hostname-scoped, no cross-plane replay. Access session (24 h) is the effective backend session; logout → `/cdn-cgi/access/logout`.
-   **Single-node pilot**: JuiceFS-metadata SPOF and the lone tunnel replica affect the **admin plane only** — the public frontend is unaffected by backend outages, which is the paradigm's headline win. Multi-node is already designed in (replicated `cluster-ingress` tunnel: node2/node3 just add connectors; JuiceFS state follows rescheduled containers; static container IPs mean the Nix-generated ingress table is already correct — no Cloudflare-side changes).
-   **All ingress on Cloudflare** (Traefik + Octavia removed in Phase 2): a Cloudflare outage takes cluster ingress down entirely — concentrated risk, not new risk (public frontends are Workers-hosted already). Break-glass: re-enable Octavia + a temporary proxy, documented not automated. cloudflared config changes restart the connector (~seconds blip; seamless once multi-node replicas exist).

## Consolidated VERIFY list (resolve during implementation)

`wrangler containers push` exact semantics + registry ref format in `containers[].image` (host is `registry.cloudflare.com`; path structure unconfirmed) · **cross-node admin routing that gates the replicated-tunnel topology: any node's cloudflared → any node's container IP over br-app/the app-VLAN routes** (fallback: per-node tunnels + per-site CNAME pinning, or a thin per-node proxy) · jwt-auth proxy-mode behavior on JWT-less requests (webhook bypass path) · gitium leaving a pre-seeded `.gitignore` intact · native extensions under the NixOS FrankenPHP unit (ZTS — expected fine, proven in the container) · avunu.io + every migrated workload domain (demo, littlecocalico) present as CF zones · image-size headroom vs Cloudflare's compressed limit (add a CI guard ~1.5 GB) · the R2 bucket-item permission-group ID (resolve at runtime via `/user/tokens/permission_groups`, never hardcode) · Cloudflare Email Service **sending-domain onboarding appears dashboard-only** (no REST/wrangler path found) — treat as a manual per-domain step.

**Resolved by the 2026-08-03 capability probe** (see Phase 4): `wrangler d1 import` does not exist (use `d1 execute --remote --file`, 5 GB) · registry image refs are accepted in `containers[].image` · nixpkgs `services.cloudflared` option shape · wrangler OAuth covers D1/R2-buckets/KV/Workers/containers/secrets but **not** Access, DNS, or token minting · custom-domain routes create DNS + cert automatically but refuse a pre-existing record · `cloudflared tunnel route dns` should not be used for wildcards (create the CNAME via the DNS API) · Access application paths cannot match query strings.

## Verification (per phase)

-   **Phase 0**: `nix build` all existing package variants byte-compatible; new `mkSiteImage` with a fixture wp-content → grafted files present; `mkStaticAssets` output has no `.php`, no `wp-admin/`; `mkSiteWorker` bundle builds and the full vitest suite passes in this repo's CI (cache + guard + `/__d1`); `nix flake check` (module VM test) for managed+d1 mode: container boots, symlinks correct, db.php generated, no MariaDB.
-   **Phase 1**: `nix flake init -t` a scratch site → `wrangler deploy --dry-run` with the nix-built worker bundle and placeholder-substituted config resolves all bindings; `wrangler dev` serves locally.
-   **Phase 2**: `colmena build` for node1; tunnel handshake (`cloudflared tunnel info`); `demo` serves through the tunnel **with Traefik stopped**; external port scan of the node shows only 22 after the firewall change; Access-gated curl on an admin hostname (302 to login) vs authenticated browser.
-   **Phase 3**: the pilot smoke list above **is** the end-to-end verification — the loop test (plugin install → git → CI → deploy → purge) proves the paradigm.
