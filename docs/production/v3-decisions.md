# wordpress-nix v3 (moonshot v3) design record: a conventional, Composer-built, read-only WordPress fleet on nixos-cluster-orchestration

*Design record, 2026-11-21. Client sites are pseudonymised (`brochure-pilot`, `v2-pilot`, `woo-flagship`, `held-site`); the full plan lives in the private cluster repo. It revises the synthesised plan against three critic passes (build/CI, runtime/security, conversion/rollout). Every blocker and major issue is addressed in the body. §15 lists what was changed, partly accepted or rejected, and why.*

**Facts re-checked against source during this edit:**
- nixpkgs `pkgs/build-support/php/builders/v2`:
  - the vendor hook runs `composer install --no-autoloader`;
  - it copies only `composer.json`, `composer.lock`, `vendor/` and the `extra.installer-paths` prefixes (not `web/wp`);
  - the install hook runs a plain `composer install` against the restored vendor, which writes the autoloader;
  - `buildComposerProject2` accepts a prebuilt `composerVendor`;
  - `checkComposerValidate` uses `--strict --no-check-all --no-check-lock`.
- Composer 2.11 `ValidateCommand` has `--no-check-all`, `--check-lock` and `--no-check-publish`. `AutoloadGenerator` does **not** write `installed.php` or `InstalledVersions.php`; it only references them.
- wordpress-nix `modules/nixos.nix`:
  - every unit sets its own `PrivateTmp = true` (1393, 1444, 1485, 1508, 1549);
  - Caddy's `XDG_DATA_HOME` (1422) and `WP_CLI_CACHE_DIR` (734, 741) are under `stateDir`;
  - `@strayPhp path /wp-content/*.php /wp-includes/*.php` (349);
  - the cron timer is `OnBootSec` plus `OnUnitActiveSec = "1min"` with no flock and no `RandomizedDelaySec` (1561-1568).
- nixos-cluster-orchestration:
  - `lib/cloud.nix` creates every tunnel with `account_id = cf.accountId` (231) and imports adopted tunnels as `${cf.accountId}/${id}` (326);
  - it writes DNS records only for hostnames inside `cloudflare.zones` (`managedHosts`);
  - `tunnel.id` plus a prompted `<name>-cf/cloudflared` credentials secret already lets an app run a tunnel made outside OpenTofu (`modules/app-container.nix:128-215`);
  - backup-core runs on whichever node is running the app's container.
- The cluster template `flake-update.yml` keeps one `flake-auto-update` branch and `concurrency: flake-update` (`cancel-in-progress: false`). Its header says the `cluster` input "is pinned by revision … Bump the library by hand, alone."
- frappe-nix `.github/workflows/app-deps.yml` has these jobs: `disarm`, `relock-plan`, `relock-push`, `auto-merge`, `hold`, `sweep` and `update-branches`. They are gated on `startsWith(github.head_ref, 'dependabot/nix/')`.
- wp-cloud-files `src/MediaHandler.php:154` unlinks after uploading, with no path exclusion.

---

## 1. Executive summary

1. **One stock Bedrock project per site.** Each site is `Avunu/site-<slug>`, an unmodified **Roots Bedrock 1.31.x** tree.
   - Core (`roots/wordpress`, an exact pin) and every wordpress.org plugin and theme (`wp-plugin/*`, `wp-theme/*`) come from Composer through Bedrock's default registry, **WP Packages**.
   - Only premium and custom code is committed, under `web/app/{plugins,themes,mu-plugins}/<slug>/`.
   - Avunu's own plugins come from Packagist as `avunu/*`.
2. **Nixpkgs-standard build.** wordpress-nix (`Avunu/wordpress`) builds the site with `php.mkComposerVendor`: a fixed-output derivation (FOD) whose only inputs are `composer.json` and `composer.lock`, with its hash kept in the one-line file `nix/vendor-hash`.
   - The FOD is then fed to `php.buildComposerProject2`, whose install hook writes the Composer autoloader offline.
   - `nix run .#relock` refreshes the hash.
3. **Dependabot updates.** Dependabot opens grouped Composer PRs. `site-deps.yml`, a Composer-lane port of frappe-nix `app-deps.yml`, then relocks with the org App, verifies the lock with frappe's "URL matches the package" rule, and auto-merges according to a decision table.
4. **CI on self-hosted Nix runners.**
   - Sandboxed checks run on the existing `[self-hosted, nixos]` org runners, and the VM smoke test runs on the `kvm` runners.
   - Networked audits (`composer audit`, wordpress.org checksums, Wordfence) run as plain job steps outside the sandbox.
   - App-key jobs run on hosted `ubuntu-latest`.
   - The cluster's private **Attic** shares the vendor FOD between CI and the nodes.
5. **Fully read-only production.** Each site runs in one nspawn container from a new single-plane `wordpress` kind, a structural copy of `kinds/frappe.nix`.
   - The docroot is an immutable store path, with Bedrock's own `DISALLOW_FILE_MODS`, `DISALLOW_FILE_EDIT` and `AUTOMATIC_UPDATER_DISABLED`.
   - MariaDB runs inside the container on ZFS `localState`, with unix_socket auth.
   - The only web-reachable writable directory is `/var/lib/wordpress/uploads` on JuiceFS. Caddy serves it with `file_server` only, so PHP never runs there.
6. **Media.** Attachments go to a per-site **R2** bucket through **wp-cloud-files 0.6.0**.
   - That release reads config from `getenv`, never offloads private prefixes (Woo downloads, form uploads), and is installable through Composer.
   - Media is backed up by a nightly `rclone copy` to a versioned, retention-locked OVH bucket, using a read-only key the container never sees.
7. **Deploy path.** Each site flake is an input of `<cluster-repo>` that follows `cluster/wordpress-nix`, the Odoo and Frappe pattern.
   - The path is: Worker → `flake-update.yml` (all branch-following inputs, with a `hold` list and a per-input split fallback) → `merge-gate.yml` → pull-deploy.
   - wordpress-nix releases reach production **library first**, then site repos.
8. **Conversion mirrors `frappe-init --migrate`.**
   - `wordpress-init --migrate` is a Python standards-core scaffolder. It runs offline on a *pulled copy*, needs the pulled survey or dump to know what is active, never deletes, stages before locking, and fails hard if the relock fails.
   - Idempotent `wordpress-nix` subcommands handle the rest: `legacy`, `db`, `media`, `edge`, `repo`, `site import`, `verify` and `cutover`.
   - All of it lives in wordpress-nix.
9. **Security posture for presumed-compromised legacy hosts.**
   - Full dumps (triggers, routines, events) are audited.
   - Application passwords and sessions are stripped, and privileged passwords are reset.
   - Salts are regenerated.
   - Two-factor is enforced for administrators.
   - Remote-management plugins (MainWP and similar) are dropped.
   - Database-stored code (WPCodeBox and similar) is moved into git.
10. **Cutover takes minutes.** The real hostnames are in the tunnel ingress from the first staging deploy, and `WP_HOME` switches *before* the final pull. The window between the final pull and the DNS flip is therefore import → verify → flip, with no merge gate inside it.
    - DNS, WAF and cache rules for client zones (spread over about 22 Cloudflare accounts) belong to `wordpress-nix edge` and `cutover`, never to `avc cloud`.
11. **Pilot and retirement.**
    - Pilot: **brochure-pilot**.
    - The v2-pilot live site is remediated and its key safely rotated in M0, then reconverted in wave 2.
    - All v2 code (split-plane, gitium, `wpdb`, Turso, `site-publish`, hosting-cloud `scripts/moonshot`) is deleted in a breaking wordpress-nix v3.0.0 and matching library and hosting-cloud changes.

---

## 2. Decisions needing the user

| # | Decision | Recommended default | Alternatives | Why |
|---|---|---|---|---|
| D1 | Pilot and wave order | brochure-pilot → avunu (if live) and v2-pilot in parallel → long tail → woo-flagship and the other Woo sites | v2-pilot as the pilot | brochure-pilot is the fleet's common premium core with no payments, and DNS alone rolls it back. v2-pilot carries v2 residue and payment add-ons. Confirm the Avunu site is live on WordPress |
| D2 | Media provider | Per-site Cloudflare R2 bucket plus `media.<domain>`, an account-owned bucket-scoped key, and a nightly RO-key `rclone copy` into a versioned, retention-locked OVH bucket | OVH S3 through `avc cloud` | R2 is what wp-cloud-files and the live v2-pilot site already use: free egress and a CDN. R2 has no S3 object versioning, so the OVH mirror is the recovery control |
| D3 | Admin protection | Stock login + enforced `two-factor` (email provider for `manage_options`) + WAF login rate limit on every site. Path-scoped Access opt-in per site (`access.adminPaths`) for brochure sites. Application passwords off in production | Access on by default; jwt-auth SSO | Woo and membership sites need public login and ajax. This is conventional WordPress hardening |
| D4 | Auto-merge appetite | Auto: plugin and theme minor/patch (7-day cooldown, 1 day for patch), core patch (no cooldown), `avunu/*`, PHP libraries, github-actions. Review: core minor (until previews), Woo minors on Woo sites, all majors, new packages | Everything on review | The relock is verified and smoke-tested, and the vulnerability feed is independent of GHSA |
| D5 | Vulnerability feed | Wordfence Intelligence V3 free production feed. Someone registers the key and accepts the commercial-use terms | Patchstack (paid); WPScan (25 requests a day) | It is the only free feed that covers wp.org and many premium slugs |
| D6 | Existing site repos that hold dumps (`site-<pilot>`, `site-<woo-flagship>`, `site-avunu`, `site-<v2-pilot>`) | `wordpress-nix repo adopt` audits history. If it finds any `*.sql`, `wp-config.php` with secrets or `.env`: rename the repo to `site-<slug>-legacy`, archive it, and create a fresh `site-<slug>`. Otherwise land the conversion as a PR | Rewrite history with `git filter-repo` | Customer PII and password hashes are in history. Archive-and-replace is reversible and needs no force-push |
| D7 | Cloudflare account topology and DNS ownership | Each client zone stays in its own account. `wordpress-nix edge provision` creates the site's tunnel **in the zone's account** with the existing multi-account provisioning token. The kind sets `tunnel.id` and `tunnel.accountId`, which tells `avc cloud` to leave it alone. DNS, WAF, Cache Rules and Access for client zones are owned only by `wordpress-nix edge` / `cutover`, and client zones must not appear in `cloudflare.zones` | Widen the cluster's OpenTofu token to all ~22 accounts and add per-account providers; move client zones into Avunu's account | A CNAME to `<id>.cfargotunnel.com` resolves only for a tunnel in the zone's own account (`provision.sh:189-192`). This is the smallest library change and matches the v2 tooling that already works. Two DNS owners would fight |
| D8 | Salts | Regenerate. `--keep-salts` only for flagged salt-dependent sites whose host is not suspected compromised | Keep everywhere | Legacy hosts are presumed compromised (a malicious trigger was found in a real dump) |
| D9 | Delete rather than deprecate v2 | Yes: wordpress-nix v3.0.0 (breaking) plus library and hosting-cloud deletions | Keep v2 behind flags | There are no other consumers, and v2 never went live |
| D10 | Premium plugins with official Composer repositories (ACF Pro, Gravity) | Commit them like all other premium code | `registries:` plus an impure FOD | No fleet site uses them today. Credentials in a FOD need `impure-env` |
| D11 | Manual exceptions to "never write to the legacy server" | Allowed, done by a human through the legacy-host panel and recorded in the cutover record: (a) swapping v2-pilot's R2 key in M0, (b) remediating v2-pilot now, (c) disabling auto-updates and plugin installs at T−7, (d) the admin freeze and form notice on cutover day | None (leave v2-pilot unremediated and drifting until wave 2) | The tooling stays read-only, but a live site needing remediation and code drift during the T−7 week both need a human touch |
| D13 | **Where non-attachment upload writers live** (Cwicly's regenerated CSS in `uploads/cwicly/` on every site, Font Library `uploads/fonts/`, WPCodeBox, Fluent Forms entry files, Woo private downloads) | A small per-site **JuiceFS** volume at `/var/lib/wordpress/uploads` (S3-backed, served by `file_server` only, never PHP). Attachments still go to R2 through wp-cloud-files. The code tree stays fully read-only | (a) Extend wp-cloud-files to offload *any* write under uploads to S3, with a private bucket or prefix for Woo and forms, so production has **no** writable volume at all. (b) Ephemeral uploads (loses Cwicly CSS on restart; not viable) | This is the one place the plan departs from "entirely read-only, uploads in S3". wp-cloud-files is a stage-then-unlink attachment offloader, not a filesystem. Cwicly, fonts and forms write outside the attachment path, and Woo and form files must not be public. Option (a) is the purer end state, but it is a substantial plugin project (stream wrapper or `wp_upload_dir` hooks plus signed private URLs). Recommendation: ship with JuiceFS and revisit (a) after wave 2 |
| D12 | WPCodeBox in production | Snippets live in git as the committed `wpcodebox_functionality_plugin`. `wpcodebox2` is deactivated in production through the new `developmentOnlyPlugins` list. Snippets are edited in devenv | Keep editing snippets in production admin | Production can't write the generated plugin (EROFS), and database-eval'd code makes an admin compromise into RCE |

---

## 3. Target architecture

```
                         ┌──────────────── Avunu/wordpress (wordpress-nix) ─────────────────┐
                         │ lib.mkBedrockSite · flakeModules · nixosModules · wordpress-init   │
                         │ wordpress-nix CLI · reusable site-{ci,deps,audit}.yml · rollout.yml│
                         └───────────────┬───────────────────────────────┬──────────────────┘
                     release v3.x.y      │ (1) library bump               │ (3) rollout PRs to sites
                                         ▼                                │     (after the cluster runs it)
          nixos-cluster-orchestration (kind wordpress, tunnel.accountId,   │
          flake-update hold/split, project-inputs-follow)                  │
                                         │ (2) cluster rev bump (by hand)  │
                                         ▼                                 ▼
Site repo Avunu/site-<slug> (stock Bedrock + thin flake)      <cluster-repo> (private instance)
  composer.json/.lock  ← Dependabot (daily, grouped)            flake.nix: site-<slug>.follows
  nix/vendor-hash      ← relock-push (App)                      apps/<slug>.nix (kind wordpress)
  web/app/*/<premium>/ ← humans (devenv)                        flake-update.yml → merge-gate.yml (KVM)
  .github/workflows → Avunu/wordpress site-*.yml@SHA            → auto-merge → cluster-upgrade
      │ push main: Attic ← vendor FOD (shared by out-path)                │
      └──── cluster-ci Worker ──── dispatch ─────────────────────────────►┘
                                                                          ▼
Container <slug>  (nspawn, Nomad-placed, dormant on all nodes)
  FrankenPHP unix:/run/wordpress/wp.sock   root ${package}/web   (read-only store)
    /app/uploads/*   → handle{ file_server } only  (never PHP)   private prefixes 403
    /wp-content/uploads/* → 301 media.<domain>/* or /app/uploads/*   /wp-admin → /wp/wp-admin
  ${package}/.env → /run/wordpress/bedrock.env (0400)   ${package}/share/wordpress-nix/vendor-src
  /var/lib/wordpress           (stateDir: Caddy data, wp-cli cache; RW, not web-served)
  /var/lib/wordpress/uploads   (JuiceFS: cwicly/, fonts/, wpcodebox/, woocommerce_uploads/, forms …)
  /tmp                         (PrivateTmp per unit; WP_TEMP_DIR=TMPDIR=/tmp)
  MariaDB /var/lib/mysql       (unix_socket; ZFS localState, 60 s replication)
  wordpress-cron.timer (1 min oneshot) · wordpress-migrate (is-installed → snapshot → update-db)
  cloudflared (tunnel in the CLIENT zone's account) → <domain>, www.<domain>, staging.<domain>
      ▼
R2 wp-<slug>-media ── media.<domain> (CORS: PUT from domain/www/staging)
      └─ nightly host-side rclone copy (RO key, placing node) → OVH bucket (versioned + retention)
```

---

## 4. Key design decisions (rationale and rejected alternatives)

### 4.1 Layout: stock Bedrock
- **Decision.**
  - Render from a `roots/bedrock` tag pinned as a wordpress-nix input: `inputs.bedrock = { url = "github:roots/bedrock/1.31.9"; flake = false; }`. Dependabot's `nix` ecosystem bumps it in wordpress-nix only.
  - `config/application.php`, `config/environments/*`, `web/wp-config.php`, `web/index.php`, `CONTENT_DIR=/app`, `wordpress-install-dir: web/wp` and the `installer-paths` are **never edited**.
- **Why.**
  - The user asked for "Bedrock or equivalent", and framework defaults are the binding preference.
  - Bedrock ships the three read-only constants and reads real env variables when no `.env` exists.
  - `development.php` sets `DISALLOW_FILE_MODS=false`, which the premium-update flow uses. Confirmed against 1.31.9 in M1.
- **Consequences.**
  - URLs move to `/app/...` and `/wp/wp-admin`.
  - `wordpress-nix db rewrite-urls` (§8.6) rewrites them in the database.
  - FrankenPHP adds compatibility rules:
    - `/wp-admin*` → 301 `/wp/wp-admin*`;
    - `/wp-login.php` → 308 `/wp/wp-login.php`;
    - an internal rewrite `/wp-content/{plugins,themes}/*` → `/app/...`;
    - **`/wp-content/uploads/*` → 301 to `S3_PUBLIC_URL/*`**, except the local prefixes (`cwicly/`, `fonts/`, `wpcodebox/` and the other non-attachment directories), which go to `/app/uploads/*`. This keeps backlinks, Google Images results and sent emails working.
- **Rejected.**
  - A classic layout with composer/installers: an undocumented hybrid.
  - `johnpbloch/wordpress-core`: not the default.
  - Any edit to `config/application.php`.

### 4.2 Registry: WP Packages with an `only` guard
- **Decision.** `{"name":"wp-packages","type":"composer","url":"https://repo.wp-packages.org","only":["wp-plugin/*","wp-theme/*"]}`.
- **Why.** It has been Bedrock's default since 1.30, builds only from tagged releases, and can be self-hosted. WPackagist has been WP Engine-owned since 2026-03, during litigation.
- **Gap.** Untagged or trunk-only plugins are classified `not-in-registry` and committed instead.
- **Revisit trigger.** An outage longer than 24 hours, or a tamper event. Then self-host `roots/wp-packages`; the registry URL is one managed line.
- **Rejected:** WPackagist; a self-hosted mirror now.

### 4.3 Composer to Nix: one vendor FOD plus the nixpkgs install hook
- **Decision** (`lib/mk-bedrock-site.nix`):
  ```nix
  vendor = php.mkComposerVendor {
    pname = "${name}-vendor"; version = "0";
    src = lib.fileset.toSource { root = siteRoot;
      fileset = lib.fileset.unions [ (siteRoot + "/composer.json") (siteRoot + "/composer.lock") ]; };
    vendorHash = lib.fileContents vendorHashFile;      # ./nix/vendor-hash
    composerNoPlugins = false;   # composer/installers + roots/wordpress-core-installer must run
    composerNoDev = true; composerNoScripts = true;
    # The vendor hook keeps only vendor/ and installer-paths; web/wp comes from
    # wordpress-install-dir and is always dropped, so this copy is mandatory.
    postInstall = ''
      cp -ar web/wp "$out/web/wp"
      test -f "$out/web/wp/wp-includes/version.php"
      # Composer-version-specific files; the install hook rewrites them (M1 verifies).
      rm -f "$out/vendor/composer/installed.php" "$out/vendor/composer/InstalledVersions.php"
    '';
  };
  package = php.buildComposerProject2 {
    pname = name; version = wordpressVersion;   # read from composer.lock
    src = cleanedSiteSrc;                       # no .env*, auth.json, .git, nix/
    composerVendor = vendor;
    composerNoPlugins = false; composerNoDev = true; composerNoScripts = true;
    composerStrictValidation = false;           # validate runs as its own check (§6.2)
    postConfigure = ''cp -ar ${vendor}/web/wp web/wp; chmod -R u+w web/wp'';
    # composerInstallBuildHook: offline `composer install` from the restored vendor
    # → writes vendor/autoload.php, autoload_real.php, installed.php, InstalledVersions.php
    postBuild = ''composer dump-autoload --no-dev --optimize --no-plugins --no-scripts'';
    installPhase = ''<copy tree to $out; inject platform mu-plugins; runtime symlinks (§7.1)>'';
    # Assertions (fail closed):
    #   web/wp/wp-includes/version.php, vendor/autoload.php,
    #   vendor/composer/autoload_real.php, vendor/composer/InstalledVersions.php exist;
    #   no .env*, auth.json, .git; `php -l` over committed web/app code.
    # Runtime reference so Attic retention keeps the FOD (it is in every node closure):
    #   echo ${vendor} > $out/share/wordpress-nix/vendor-src
    passthru.wordpressNix = { inherit php wordpressVersion vendor; };
  };
  ```
- **Why.**
  - It is the nixpkgs-standard path, and the install hook is the one nixpkgs uses to produce the autoloader.
  - Committed premium code never perturbs the hash.
  - The FOD output path depends only on name and hash, so CI and the nodes share it through Attic **even though the two package derivations differ** (§4.7).
- **M1 verification items, with fallbacks.**
  - Does the install hook's offline `composer install` rewrite `installed.php` and `InstalledVersions.php`? If not, keep them inside the FOD. The hash then couples to the Composer version, which the risk register covers.
  - Does `--optimize` work with every fleet plugin? `--classmap-authoritative` is rejected because it breaks packages that load classes outside the classmap.
- **Relock.** `.#relock` (`lib/relock.nix`, the same name and shape as frappe-nix and odoo-nix):
  1. build `vendor` with `lib.fakeHash`;
  2. parse `got: sha256-…`;
  3. write `nix/vendor-hash`;
  4. rebuild to confirm.

  It is plain Nix.
- **Hash drift.**
  - `site-audit.yml` runs `nix build --rebuild .#vendor` weekly.
  - A mismatch on an unchanged lock is a **security event**: an issue labelled `security`, and never an automatic relock.
  - FOD retention in Attic comes from the runtime reference above: nodes push their running closures daily, and the library's 90-day retention keeps anything pushed or fetched.
- **Rejected.**
  - Per-package lock generators (composer2nix, a custom fetchzip generator, composition-c4): bespoke, and wp.org dists carry no hashes.
  - Renovate `postUpgradeTasks`: Dependabot is binding.
  - Committing `vendor/`.
  - Deleting `installed.json` (dump-autoload needs it).

### 4.4 Relock workflow for Dependabot
- **Decision.** wordpress-nix reusable `.github/workflows/site-deps.yml`, called from each site's thin `deps.yml`. It is a **redesign** of frappe `app-deps.yml` for a Composer lane, not a copy:
  - **Trigger condition** on every job:
    - `github.event.pull_request.user.login == 'dependabot[bot]' && startsWith(github.head_ref, 'dependabot/composer/')`;
    - or a rollout PR (`head_ref == 'wordpress-nix/rollout'`) that changed `composer.json`.
  - There is **no `relock-inputs` step**; the site's `flake.lock` moves only through rollout.
  - `disarm` (frappe's): auto-merge is off on every new head until relock and verdict finish.
  - `relock-plan` (`["self-hosted","nixos"]`, `contents: read`, Attic read token):
    1. `nix run .#relock`;
    2. upload the patch.
  - `relock-push` (`ubuntu-latest`, App token minted here only):
    1. re-check the patch touches only `nix/vendor-hash` (mode 100644, `^sha256-[A-Za-z0-9+/]{43}=$`). The allowlist is a **standards-core product hook** (`Product.relock_patch_paths`).
    2. `wordpress-nix ci verify-relock` (below);
    3. push `chore(deps): relock vendor hash [dependabot skip]`.
  - `auto-merge`: standards-core `ci verdict` with the §6.4 table, then `gh pr merge --auto --squash --match-head-commit`.
  - `hold`, `sweep` and `update-branches`: frappe's.
- **`verify-relock` rule** (frappe's "the URL names the same package and version", not a vendor allowlist). For every package added or changed in `composer.lock`:
  - `wp-plugin/<s>` / `wp-theme/<s>`: `dist.url` must be `https://downloads.wordpress.org/{plugin|theme}/<s>.<version>.zip`.
  - `roots/wordpress-no-content`: `https://downloads.w.org/release/wordpress-<version>-no-content.zip`, and the lock's `shasum` must be present.
  - **Packagist packages** (everything else, including `avunu/*`, `roots/*`, `composer/*`, `aws/*`, `symfony/*`, …): `dist.url` must be `https://api.github.com/repos/<o>/<r>/zipball/<ref>` or `https://codeload.github.com/<o>/<r>/legacy.zip/<ref>`. `<o>/<r>` must equal `source.url`'s owner/repo, and `<ref>` must equal `source.reference`.
  - Anything else, such as another host or a mismatch, forces `needs-review`. A **newly added package** always forces `needs-review`.
- **Tests.** A pytest `lockcheck` fixture built from a real Bedrock 1.31.9 + wp-cloud-files 0.6 lock, with a tampered-host case and an owner-mismatch case.
- **Secrets.** Workflows on Dependabot's own head see only *Dependabot* secrets, while workflows on an App-pushed head see *Actions* secrets. `wordpress-nix repo apply` therefore provisions these **both** as org Actions secrets **and** as org Dependabot secrets, scoped to `site-*` repos and named per the frappe contract:
  - `APP_ID`;
  - `APP_PRIVATE_KEY`;
  - `WORDPRESS_NIX_CACHE_READ_TOKEN`;
  - `WORDFENCE_API_KEY`.

  `WORDPRESS_NIX_CACHE_TOKEN` (Attic write) is an Actions secret only. The `main` push that follows an App-made merge then runs with it and uploads the vendor FOD.
- **Rejected.**
  - `pull_request_target` with write tokens (pwn-request).
  - A vendor-name host allowlist: it would refuse every PHP-library bump.
  - Renovate.

### 4.5 Premium and custom plugins under `DISALLOW_FILE_MODS`
- **Decision.** Commit them, each whitelisted in a managed `.gitignore` block (`!web/app/plugins/<slug>`). The plugin header `Version:` is the record.
- **Update flow** (devenv):
  1. `wp restore`;
  2. Bedrock's `development` environment allows file mods, so the vendor's own licence updater works;
  3. `wp plugin update <slug>`;
  4. review the diff, commit `fix(plugins): update <slug> to X.Y.Z`, and open a PR.
- **Checks.**
  - Advisory `ci / premium-diff` flags new `eval`, `base64_decode`, `gzinflate`, `create_function`, `assert(` with variables, outbound URL literals and high-entropy blobs.
  - Weekly `site-audit.yml` runs a dev-mode check (`wp plugin list --update=available`) and keeps one tracking issue per site, `Premium plugin updates`.
- **Avunu packages.** `avunu/wp-cloud-files`, `avunu/cloudflare-email` and `avunu/gutenberg-downgrade` move to Packagist (`type: wordpress-plugin`).
  - Avunu's **jwt-auth fork is not in v3.0.** It is on `data/drop-plugins.json` with the reason "not in v3.0", so the slug collision with wp.org `jwt-auth` resolves to `drop`, never `avunu` and never `composer`.
  - `lc-functions` is committed.
- **Deferred:** a premium-updater instance or fleet job. Revisit after wave 3 if refresh time is the bottleneck.
- **Rejected:** vendor Composer repositories with credentials in the FOD; SatisPress or Satis; `artifact` or `path` repositories; `premium.json` bookkeeping.

### 4.6 Database: per-site in-container MariaDB with unix_socket auth
- **Decision.**
  - `database.createLocally = true` as the module already does it: unix_socket, with `database.user == user` asserted. There is no DB password.
  - Bedrock settings: `DB_USER=wordpress`, `DB_PASSWORD=""`, `DB_HOST=localhost:/run/mysqld/mysqld.sock`.
  - Storage: `localState."/var/lib/mysql"` on ZFS (60 s replication, about 3-minute failover). backup-core detects it automatically.
  - Every operator command that touches the database runs as `wordpress` (`runuser -u wordpress -- wp …`). That is also why `site import` exists (§8.6).
- **Why.** It is the frappe kind pattern: failover, previews, `cluster-restore --db-only`, and a per-site blast radius.
- **Rejected:** the shared `wpdb` (pinned to one node, v2); SQLite/Turso; a generated DB password.

### 4.7 Deploy flow, version skew and release ordering
- **Cluster input stanza:**
  ```nix
  # <cluster-repo>/flake.nix
  site-<slug> = {
    url = "git+https://github.com/Avunu/site-<slug>.git?ref=main";
    inputs.wordpress-nix.follows = "cluster/wordpress-nix";   # no nixpkgs line: the site has no nixpkgs input
  };
  ```
- **What actually deploys.** Because of `follows`, production builds every site with the **cluster's** wordpress-nix and nixpkgs, not the site's own `flake.lock`.
  - The site's lock only pins what site CI and devenv use.
  - The package that site CI builds is **not** the package the nodes run. Only the vendor FOD is shared (same output path, because the path depends on name and hash only).
  - The cluster rebuilds the cheap assembly (an offline `composer install` plus copies).
- **Release order** (documented in `docs/production/releases.md`):
  1. wordpress-nix release.
  2. Library PR bumps `wordpress-nix` in `nixos-cluster-orchestration/flake.lock` (library CI).
  3. Hand-made `<cluster-repo>` PR bumps the `cluster` rev. `merge-gate.yml` builds every node closure, which includes every site package, and the new `checks.wordpress-site-smoke` boots one Bedrock site.
  4. Only after that merges: `gh workflow run rollout.yml -f version=vX.Y.Z` opens site PRs that align `flake.lock` and render `--sync` files.
- **Compatibility rule.** Within a major, the `flakeModules.default` options and `lib.mkBedrockSite` arguments stay backward compatible: new options are optional with defaults. Rollout PRs therefore never render anything the cluster's pinned wordpress-nix does not understand.
- **Skew detection.** The nightly `site-audit.yml` job `ci / cluster-compat` (not required) builds `.#default` with:
  - `--override-input wordpress-nix github:Avunu/wordpress/<rev>`;
  - `--override-input wordpress-nix/nixpkgs github:NixOS/nixpkgs/<rev>`.

  The revs are read from <cluster-repo>'s `flake.lock` through the App token in a hosted step, then passed to the self-hosted build.
- **Library check `checks.project-inputs-follow`.** It generalises <cluster-repo>'s existing `nixpkgs-follows-library` check over `flake.lock`. For every project input whose package exposes `passthru.wordpressNix` (or the odoo/frappe equivalents):
  - its `wordpress-nix` node must resolve to `cluster/wordpress-nix`;
  - no second `nixpkgs` node may exist beneath it.
- **Worker bursts.** GitHub keeps only one pending run per concurrency group, so a Worker dispatch runs `nix flake update` over **all branch-following inputs except those in `.github/flake-update-hold`**. That file lists one input per line, each with a comment.
- **Head-of-line fallback.** If `merge-gate` fails on `flake-auto-update`, the new `flake-update-split` job (same workflow, `workflow_run` on merge-gate failure):
  1. reads the lock diff;
  2. opens `flake-auto-update/<input>` PRs with one input each;
  3. lets merge-gate run on each, so the good ones auto-merge;
  4. opens or updates an issue `flake input <name> fails the merge gate`, and proposes a hold-file PR for the bad input.

  One broken site therefore delays the others by about one gate run, never indefinitely.
- **Rejected.**
  - v2's `site-publish` → `content.rev` → eval-time `fetchGit`.
  - Letting sites not follow `cluster/wordpress-nix`: it multiplies PHP, MariaDB and nixpkgs closures and eval cost per site.

### 4.8 Runtime writers: one rule, one home per path
- **The rule.**
  - The code tree is a read-only store path.
  - **Only attachment-index paths go to R2.**
  - Every other directory under uploads lives on JuiceFS (`/var/lib/wordpress/uploads`). That includes `cwicly/`, `fonts/`, `wpcodebox/`, form uploads, Woo downloads and plugin caches.
  - Caddy serves uploads with `handle /app/uploads/* { … file_server }`, so **no PHP ever runs there**, whatever the extension.

| Writer | v3 handling |
|---|---|
| Media attachments | wp-cloud-files stages, pushes to R2 and unlinks. `S3_DIRECT_UPLOADS=true` with bucket CORS (§7.3) gets past the 100 MB tunnel cap |
| Cwicly CSS and local fonts (`uploads/cwicly/**`) | JuiceFS, served at `/app/uploads/cwicly/…`. Captured into the JuiceFS tarball, never R2 |
| Font Library (`uploads/fonts`) | JuiceFS. URLs stay on `/app/uploads/fonts/…` through the `--exclude-prefix` regex (§4.9). M2 test |
| WPCodeBox2 (`uploads/wpcodebox/`, generated functionality plugin) | Functionality plugin committed as code. `uploads/wpcodebox/` on JuiceFS; its `index.php` stub (exact content `<?php die();`) is exempt from the executable rule. Production deactivates `wpcodebox2` (D12) |
| WooCommerce private downloads (`woocommerce_uploads/`) | JuiceFS only. `S3_EXCLUDE_PREFIXES` stops offload and rewrite. Direct HTTP returns 403. Import forces `woocommerce_file_download_method=force` |
| `wc-logs` | `WC_LOG_HANDLER=WC_Log_Handler_DB` through the bridge |
| Gravity (`gravity_forms/`), `wpo_wcpdf*` | JuiceFS, excluded from offload, and HTTP 403 (they proxy downloads through PHP) |
| Fluent Forms (`fluentform/`) | JuiceFS, excluded from offload. **Served, not denied**: Fluent links entry files directly, with randomised names. `fluentform/temp/` is denied. Verified per installed version in M4; switch to deny only if the plugin's protected-storage mode is on |
| woo-product-feed-pro, Rank Math, CleanTalk, sheet-editor exports | JuiceFS. CleanTalk verified in staging |
| mpdf/dompdf (dk-pdf, give-pdf-receipts, wp-cloud-files thumbnails) | `get_temp_dir()` → `/tmp`. Classifier flag `mpdf-tempdir`. The plugin's temp-dir filter is set in `platform-environment.php` where one exists; otherwise the plugin is dropped |
| `WP_TEMP_DIR`, `TMPDIR` | `/tmp`, which exists in every unit's own PrivateTmp. No `mkdir` and no shared temp directory |
| Translations | None baked in. Fleet is en_US. Flag `non-english-locale` → add `inpsyde/wp-translation-downloader` for that site |
| Page and object caches, drop-ins | Dropped. HTML caching is a Cloudflare Cache Rule with cookie bypass |
| Remote management, backup, debug, host and security plugins: `mainwp-child`, `mainwp`, `core-rollback`, `wp-rollback`, `wp-console`, `download-plugin`, `sql-buddy`, `wpvivid*`, `health-check` mu-plugin, legacy host mu-plugins, `host migration plugins`, `index-wp-*`, `wordfence`, `sucuri`, `patchstack`, Avunu `jwt-auth` fork | Dropped with a reason (`data/drop-plugins.json`). `db audit --strip` deletes `mainwp_child_*` options. The cutover checklist disconnects the site from any MainWP dashboard |
| Database-stored code: `wpcodebox2`, `code-snippets`, `insert-headers-and-footers`, header/footer options, widgets | Classifier flag `db-code` (blocking until decided). `db audit` reports PHP and `<script>` payloads |
| Update checks, Site Health | `platform-environment.php` (production) short-circuits `pre_site_transient_update_*` and filters the `background_updates` and filesystem tests |

- **Constants bridge.** `config/application.php` stays untouched.
  1. wp-cloud-files 0.6.0 reads `getenv('S3_*')` when a constant is undefined (§10.4).
  2. `mu-plugins/platform-environment.php` defines these from env when set and not yet defined: `DISABLE_WP_CRON` (read late, in `wp_cron()`), `WC_LOG_HANDLER` and `S3_DIRECT_UPLOADS`.
  3. `WP_TEMP_DIR` is not bridged. `TMPDIR=/tmp` suffices through `get_temp_dir()` → `sys_get_temp_dir()`.
- **Rejected.**
  - Ephemeral uploads (loses Cwicly and Woo files).
  - A stream wrapper.
  - JuiceFS-mutable `wp-content` (v2).
  - Cwicly or fonts in R2: two homes for one path.
  - A per-object `--only-offloaded` lookup: `wp search-replace` cannot do it.

### 4.9 URL rewrite sequence (one command)
`wordpress-nix db rewrite-urls --from-home https://<domain> --to-home <URL> --media-url https://media.<domain> --exclude-prefix-from <inventory.json>` runs against whichever `wp` is on PATH: devenv, or inside the container through `site import`. Steps, in order:
1. **Attachments → media**, while the dump still holds the legacy domain. Run `wp search-replace --regex --all-tables-with-prefix --precise` with:
   - pattern `(https?:)?//<domain-re>/wp-content/uploads/(?!(?:cwicly|fonts|wpcodebox|woocommerce_uploads|fluentform|gravity_forms|wpo_wcpdf[^/]*|wc-logs|…)/)`;
   - replacement `https://media.<domain>/`.

   The lookahead list comes from `wp-cloud-files inventory` (non-attachment directories) unioned with `S3_EXCLUDE_PREFIXES`. The step runs twice: plain form, and JSON-escaped form (`https:\\/\\/<domain>\\/wp-content\\/uploads\\/`).
2. **Remaining legacy content paths, host-scoped.** `(https?:)?//<domain-re>/wp-content/` → `https://<domain>/app/`, in plain and JSON-escaped forms. URLs of other hosts are never touched.
3. **Host change**, only when `--to-home` differs, i.e. for staging: `https://<domain>` → `--to-home`, plain and escaped.

   `http://` and `//` variants of the domain and `www.` are covered by the regexes. With `--report-changes`, per-table counts go to the cutover record.
4. Then run `wp core update-db` and `wp rewrite flush`, then regenerate the Cwicly CSS (CLI hook if present, else an editor re-save as a checklist item). Check `wp_font_face` URLs.

`wp-cloud-files migrate-urls` (prefix-only) is used only by sites without wordpress-nix. The adopt mode for partially offloaded sites (v2-pilot) is the same sequence: step 1 is idempotent on URLs already on `media.<domain>`.

### 4.10 Cron
- `DISABLE_WP_CRON=true`.
- The module's existing `wordpress-cron` timer runs: `OnBootSec` plus `OnUnitActiveSec=1min` on a oneshot, so it cannot overlap itself, and `wp cron event run --due-now`. Action Scheduler runs inside it.
- No flock. `RandomizedDelaySec` is added only if M6 measurements show cross-site load spikes.
- `/wp-cron.php` is not served. Previews turn the timer off.
- On an empty database (first boot before import), cron and migrate exit 0 through `wp core is-installed`.

### 4.11 Admin login, Access, credentials
- **Default.** Stock login plus `wp-plugin/two-factor`, which the template adds to every site.
  - `platform-environment.php` (production) **forces** the Email provider for users with `manage_options` through the two-factor plugin's provider filters. The filter names (`two_factor_providers`, `two_factor_enabled_providers_for_user`, `two_factor_primary_provider_for_user`) are verified against the pinned version in M1.
  - It also disables application passwords (`add_filter('wp_is_application_passwords_available', '__return_false')`) unless the kind sets `applicationPasswords = true`, which renders `WORDPRESS_NIX_APPLICATION_PASSWORDS=1`.
- **Edge.** WAF rate limit on `/wp/wp-login.php` and `/wp-login.php`, through `wordpress-nix edge provision` in the zone's account. `xmlrpc.php` is denied.
- **Opt-in.** Path-scoped Access (`--access`) on `/wp/wp-admin*` and `/wp/wp-login.php`, with bypasses for `admin-ajax.php`, `admin-post.php`, `load-styles.php` and `load-scripts.php`.
- **Credentials that survive a move.** `db audit --strip`:
  - deletes all `_application_passwords` and `session_tokens` usermeta;
  - removes `mainwp_child_*` options;
  - reports users whose role was raised against the survey.

  `site import` resets the passwords of all administrators and editors:
  - on staging, `wp user reset-password --skip-email` (random);
  - at cutover, with the email sent.
- **Rejected:** Access JWT plus the forked jwt-auth; Access on by default (D3).

### 4.12 Previews
- Implemented in the kind with `preview.slots` defaulting to 0 until the pilot passes:
  - `origin = "unix:/run/wordpress/wp.sock"`;
  - `socketGroups = ["wordpress"]`;
  - `healthPath = "/wp-json/"`.
- **Neutralised:**
  - `WP_ENV=staging` (disallow-indexing);
  - `WP_HOME` set to the preview host;
  - cron timer off;
  - `pre_wp_mail` short-circuited outside production;
  - `productionOnlyPlugins` deactivated;
  - **no S3 credentials**, only bucket, endpoint and public URL: wp-cloud-files' read-only mode, allowed only outside production.

### 4.13 Media bucket and backup
- **Decision.**
  - `wordpress-nix media provision <slug> --domain D [--adopt BUCKET]` creates or adopts the R2 bucket `wp-<slug>-media` in the zone's account, binds `media.<domain>`, mints an account-owned, bucket-scoped RW key, and sets **CORS**: PUT/GET from `https://<domain>`, `https://www.<domain>` and `https://staging.<domain>`.
  - It probes with put/get/delete and a CORS preflight.
  - It is ported from `provision.sh` (`resolve_accounts`, `cf_zone_for`/`cf_account_for`, key minting, `r2-rotate`, the 9d83999 probe).
- **Backup.** Nightly `rclone copy`, **never `sync`**, from R2 to `wordpress-media/<slug>/` in a versioned OVH bucket with object-lock retention (30 days), using a separate **read-only** R2 key (`<slug>-media-ro`).
  - It runs host-side through a new generic `backup.hostPostCommand`. That option runs on the node where backup-core already runs, the one hosting the container, so no extra placement logic is needed.
  - The RO key is mounted only there, never in the container.
- **Dev-restore dumps** also come from `backup.hostPostCommand`. It gzips backup-core's own logical dump from the staging directory and uploads it to `s3://<dev-restore-bucket>/wordpress/<slug>/<ts>.sql.gz` with a host-side key limited to that prefix where OVH IAM allows (M3 verifies). The container holds no backup credentials.
- **Not used:** R2 object versioning. R2 does not implement S3 `PutBucketVersioning`; M0 re-checks the current Cloudflare docs. R2 Bucket Locks are not used either, because wp-cloud-files deletes and thumbnail regeneration must keep working.
- **Rejected:** OVH S3 as the primary; `cloudflare_r2_bucket` in `lib/cloud.nix`.

### 4.14 Vulnerability feed
- **Wordfence Intelligence V3** (D5). `wordpress-nix audit` matches `composer.lock` (`wp-plugin/*`, `wp-theme/*`, `roots/wordpress-no-content`) and committed plugin header versions against the affected ranges.
  - `ci / audit` blocks only *newly introduced* vulnerable versions; pre-existing ones are reported.
  - The daily `site-audit.yml` opens or updates `security`-labelled issues.
  - An operator then runs `composer update wp-plugin/<x>` on a `security/*` branch, which bypasses cooldown in the decision table.
- `composer audit --locked` (Packagist advisories) runs in the same job for PHP libraries.
- **Deferred:** automatic security-lane PRs, after wave 2.

### 4.15 Integrity cross-check
- `verify-wp-checksums` is a stdlib-Python wordpress-nix package. It compares:
  - core against `api.wordpress.org/core/checksums/1.0/`;
  - each `wp-plugin/*` against `downloads.wordpress.org/plugin-checksums/<slug>/<ver>.json`;
  - `wp-theme/*` against the locked zip, downloaded and diffed, since there is no checksum API for themes.
- It fails on any differing or extra `.php` file.
- It runs as a **CI job step, not a sandboxed check** (it needs the network). The classifier and `verify` use it too.

### 4.16 Caches and runners
- **Cache.**
  - Merge `feat/attic-cache`. The cluster Attic is the site cache, with frappe's variable shape: `WORDPRESS_NIX_CACHE=attic:<endpoint>/<cache>`, `WORDPRESS_NIX_CACHE_TOKEN` (`main` pushes) and `WORDPRESS_NIX_CACHE_READ_TOKEN`.
  - Before the cluster is live (M5a), the fixture uses a private Cachix cache through the same variable. The fixture holds no licensed code.
  - `wordpress-nix.cachix.org` keeps serving platform outputs only (PHP, FrankenPHP).
  - Site workflows assert that they hold no Cachix write token once Attic is live.
  - **Rejected:** niks3; harmonia; Cachix for real sites.
- **Runners.**
  - nixos-dev-host `services.githubRepoRunners` org runners: `[self-hosted, nixos]` for sandboxed checks and audits, `[self-hosted, linux, x64, kvm]` for smoke and merge-gate.
  - Never on production nodes. Forks and App-key jobs run on `ubuntu-latest`.
  - Capacity: the current runner plus one KVM runner of 16 GB or more; add another when the queue exceeds about 1 hour.

### 4.17 Secrets (agenix-rekey in <cluster-repo>)
- **Generators from the kind.**
  - `<slug>`: generated, `share = true`. An `env` file with the 8 Bedrock salts only.
  - `<slug>-cloud-files`: prompted, `multiline-hidden`, `persist`. `S3_KEY`/`S3_SECRET`, plus `CLOUDFLARE_EMAIL_*`.
  - `<slug>-cf/cloudflared`: prompted. The tunnel credentials JSON printed by `edge provision`.
  - `<slug>-media-ro`: prompted, host-side only.
- **Ordering rule** (`7952cb8`): the app file is never merged before its prompted secrets exist. `avc secrets provision` is run first.
- **Salts.**
  - Regenerated by default.
  - Flag `salt-dependent`, from `data/salt-dependent-plugins.json`.
  - `--keep-salts` carries them through `restore-core-keys`.
- **Dev.** `lib.secrets.mkModule` with `secrets/backup-access.age`.
- **Tracked legacy secrets** are reported with `git rm --cached` and a "rotate" note, never removed automatically.

---

## 5. Site project template

### 5.1 Tree
```
composer.json composer.lock nix/vendor-hash
config/application.php config/environments/{development,staging}.php   # Bedrock, untouched
web/index.php web/wp-config.php web/app/mu-plugins/bedrock-autoloader…  # Bedrock
web/app/{plugins,themes,mu-plugins}/<committed>/
flake.nix flake.lock .envrc .env.example
.gitignore (Bedrock + managed block)  secrets/README.md secrets/backup-access.age
.github/dependabot.yml .github/wordpress-nix.json .github/workflows/{ci,deps,audit}.yml
committed.toml .editorconfig .pre-commit-config.yaml README.md
```
`.github/wordpress-nix.json` is managed by `--sync`. It holds `{ "woocommerce": bool, "dropPlugins": [basenames], "localUploadPrefixes": [...], "homeUrl": "…" }`, which the decision table, `site import` and `db rewrite-urls` read.

### 5.2 `composer.json` (the Bedrock delta; versions are illustrative, the converter writes real ones)
```json
"repositories": [{ "name": "wp-packages", "type": "composer", "url": "https://repo.wp-packages.org",
                   "only": ["wp-plugin/*", "wp-theme/*"] }],
"require": {
  "php": ">=8.3", "composer/installers": "^2.3", "oscarotero/env": "^2.1",
  "roots/bedrock-autoloader": "^1.0", "roots/bedrock-disallow-indexing": "^2.0",
  "roots/wordpress": "7.1.5", "roots/wp-config": "1.0.0", "vlucas/phpdotenv": "^5.6",
  "avunu/wp-cloud-files": "^0.6", "wp-plugin/two-factor": "^0.x",
  "wp-plugin/fluentform": "^6.0", "wp-plugin/seo-by-rank-math": "^1.0"
},
"config": { "platform": { "php": "8.4" },
  "allow-plugins": { "composer/installers": true, "roots/wordpress-core-installer": true } }
```
`config.platform.php` works around dependabot-core#14379.

### 5.3 `flake.nix`
```nix
{
  inputs.wordpress-nix.url = "github:Avunu/wordpress/v3.0.0";
  outputs = inputs@{ wordpress-nix, ... }:
    wordpress-nix.lib.mkFlake { inherit inputs; } {
      imports = [ wordpress-nix.flakeModules.default ];
      wordpress-nix.site = {
        name = "example-site";
        src = ./.;                         # vendorHashFile defaults to ./nix/vendor-hash
        php = "php84";
        productionOnlyPlugins = [ "fluent-smtp" ];
        developmentOnlyPlugins = [ "wpcodebox2" ];   # D12
        activeTheme = "cwicly";            # smoke test activates it
      };
    };
}
```
There is no `nixpkgs` input. nixpkgs arrives through `wordpress-nix`, and in the cluster through `cluster/wordpress-nix`.

### 5.4 `.github/dependabot.yml` (managed)
```yaml
version: 2
updates:
  - package-ecosystem: composer
    directory: /
    schedule: { interval: daily, time: "@HH:MM@", timezone: America/New_York }   # hashed from slug
    versioning-strategy: increase
    open-pull-requests-limit: 10
    cooldown:
      default-days: 7
      semver-patch-days: 1
      exclude: ["roots/wordpress", "roots/wordpress-no-content"]
    commit-message: { prefix: "fix(deps)" }
    groups:
      wordpress-core: { patterns: ["roots/wordpress*"] }
      wp-plugins:     { patterns: ["wp-plugin/*", "wp-theme/*"], update-types: [minor, patch] }
      avunu:          { patterns: ["avunu/*"] }
      php-libs:       { patterns: ["*"], exclude-patterns: ["roots/wordpress*","wp-plugin/*","wp-theme/*","avunu/*"], update-types: [minor, patch] }
  - package-ecosystem: github-actions
    directory: /
    schedule: { interval: weekly }
    ignore: [{ dependency-name: "Avunu/wordpress*" }]
    commit-message: { prefix: "chore(deps)" }
```
- There is no `nix` ecosystem entry; wordpress-nix moves only through `rollout.yml`.
- M5a resolves whether Dependabot-on-Actions egress needs a credential-less `registries: wp-packages` entry.

### 5.5 Workflows (thin callers, pinned to the SHA of wordpress-nix in `flake.lock`)
- `ci.yml` → `Avunu/wordpress/.github/workflows/site-ci.yml`;
- `deps.yml` → `site-deps.yml`;
- `audit.yml` → `site-audit.yml`, scheduled daily.

All of them pass `runs-on` as JSON, with the fork override.

---

## 6. Build and CI/update pipeline

### 6.1 Flake outputs from `flakeModules.default`
- `packages.{default, vendor}`, `apps.{relock, verify-wp-checksums, wordpress-nix}`, `devShells.default`.
- **`checks`, pure only, through `lib.checks.mk`:**
  - `composer-validate`: `composer validate --strict --no-check-all --no-check-publish --check-lock`, the nixpkgs flags plus `--check-lock`. A pytest or fixture case proves that stock Bedrock with exact pins passes.
  - `vendor-hash`: `nix/vendor-hash` is well-formed and is not `lib.fakeHash`.
  - `build`: the package, with the §4.3 assertions.
  - `lint`: prek.
  - `smoke`: a NixOS VM; requires the `kvm` system feature.

### 6.2 Gates (required: `ci / flake`, `ci / smoke`, `ci / audit`)
| Gate | Runner | Steps |
|---|---|---|
| `ci / flake` | `[self-hosted, nixos]` | `nix build .#checks.x86_64-linux.{composer-validate,vendor-hash,build,lint}`. Plus: no tracked `.env`, `auth.json` or `*.sql` |
| `ci / smoke` | `[self-hosted, linux, x64, kvm]` | `checks.smoke`, a VM with the module, the package and MariaDB. Runs `wp core install` and `wp plugin activate --all` (premium unlicensed), activates the theme, requests `/`, `/wp/wp-login.php` and `/wp-json/`. Fails on any PHP fatal or warning, or any frankenphp/wp-cli permission error in the journal. Asserts that docroot writes fail and that `/app/uploads/2024/01/x.php` returns 403 |
| `ci / audit` | `[self-hosted, nixos]` (networked job steps, outside the sandbox) | `nix develop -c composer audit --locked --no-dev`; `nix run .#verify-wp-checksums -- $(nix build --print-out-paths .#default)`; `nix run .#wordpress-nix -- audit` (Wordfence; blocks newly introduced vulnerabilities) |
| `ci / premium-diff` | `[self-hosted, nixos]` | Advisory |
| `ci / cluster-compat` | nightly in `site-audit.yml` | §4.7; not required |

**Repository policy** comes from `repo apply`:
- auto-merge on, squash only;
- secret scanning and push protection;
- the `main` ruleset;
- the dual Actions and Dependabot secrets (§4.4).

### 6.3 Composer PR lifecycle
1. Dependabot opens `fix(deps): bump the wp-plugins group …` (`composer.json` and `composer.lock`). `disarm` turns auto-merge off.
2. `relock-plan` → `relock-push`: `verify-relock`, then the App pushes `nix/vendor-hash` `[dependabot skip]`.
3. CI on the new head, which now sees Actions secrets: flake, smoke, audit.
4. `auto-merge` (verdict, §6.4) → `gh pr merge --auto --squash --match-head-commit`. `hold` covers heads that got no verdict.
5. The merge is made by the App, so the `main` push runs `site-ci`, which uploads the vendor FOD to Attic with the write token.
6. The Worker dispatches <cluster-repo> `flake-update.yml`: all non-held branch-following inputs in one PR.
7. `merge-gate.yml` builds node closures. The vendor FOD is substituted from Attic by output path, and the assembly is rebuilt locally in minutes. On failure, the split fallback isolates the bad input (§4.7).
8. Auto-merge → `cluster-upgrade` (canary first). Only `wordpress-*` units restart in the container.
9. `wordpress-migrate`:
   1. `wp core is-installed || exit 0`;
   2. snapshot, `wp core update-db`, `wp wc update` if Woo is active, `wp cache flush`;
   3. verify;
   4. roll back on failure, with frappe #296's rollback marker.
10. **Rollback.** `git revert` in the site repo (same path). For urgent fixes, revert the lock bump in <cluster-repo>. If the database was migrated, `cluster-restore <slug> --db-only` to the pre-migrate snapshot.

### 6.4 Decision table (standards-core `DecisionRule`s in `product.py`)
| Change | Verdict |
|---|---|
| `roots/wordpress*` patch | relock → auto |
| `roots/wordpress*` minor | relock → `needs-review`; auto behind a preview smoke check once previews are on |
| `roots/wordpress*` major | review |
| `wp-plugins` group, minor or patch | relock → auto |
| `wp-plugin/woocommerce*` minor, where `.github/wordpress-nix.json` has `woocommerce: true` | relock → `needs-review` |
| `php-libs` group, minor or patch | relock → auto (the verify-relock URL rule passes for GitHub zipballs) |
| `avunu/*` | relock → auto |
| any major; a new package; `verify-relock` failure | review |
| PR labelled `security` | cooldown bypassed; auto when patch or minor; all gates still required |
| github_actions `Avunu/wordpress*` | close (moves via rollout) |
| github_actions, other | auto (no relock; `ci / audit` sees `WORDFENCE_API_KEY` as a Dependabot secret) |

### 6.5 wordpress-nix release pipeline
release-please → library bump PR → hand-made `cluster` rev PR in <cluster-repo> (merge gate plus `checks.wordpress-site-smoke`) → `rollout.yml` site PRs (§4.7).

---

## 7. Production runtime spec, per site

### 7.1 Package layout (built by `mkBedrockSite`)
- `web/app/uploads` → symlink to `/var/lib/wordpress/uploads` (Trellis `shared/uploads`).
- `.env` → `/run/wordpress/bedrock.env`.
- `web/app/{cache,upgrade}` are removed.
- Platform mu-plugins are injected and loaded by `bedrock-autoloader`.
- `share/wordpress-nix/vendor-src` holds the vendor FOD path, so the FOD is retained in the closure. `auto-optimise-store` hard-links the duplicate files.

### 7.2 Container, processes, limits
- **Container:** nspawn under Nomad, `tier = "monolith"`.
- **PHP:** wordpress-nix ZTS, 8.4 by default (8.3 and 8.5 allowed), JIT on as `lib/php.nix` ships it.
  - opcache with `validate_timestamps=0`, 96M.
  - `upload_max_filesize=64M`, `max_execution_time=60`, `realpath_cache_size=4096K`.
- **Memory**, sized so the worst case fits:

| Profile | `num_threads` | `memory_limit` | `WP_MAX_MEMORY_LIMIT` (admin) | InnoDB pool | `memoryMB` = threads × limit + pool + 384 |
|---|---|---|---|---|---|
| Brochure | 3 | 192M | 384M | 256M | 1216 → **1280** |
| WooCommerce | 4 | 384M | 768M | 384M | 2304 → **2560** |

  - `OOMScoreAdjust=-500` on `mysql`; `+300` on `frankenphp` and `wordpress-cron`, so an OOM kills PHP, never the database.
  - The M6 load test runs concurrent admin exports and PDF generation.
- **Units:**
  - `wordpress-init`: assembles `/run/wordpress/bedrock.env` (0400, `wordpress`) from `environmentFiles` and `settings`; creates `uploads`. It does not create a temp directory.
  - `mysql`.
  - `wordpress-migrate`: after mysql, before frankenphp; `is-installed` guard.
  - `frankenphp`.
  - `wordpress-cron.timer`: as the module has it (§4.10).
  - `cloudflared` inside the container (library pattern).
- **Hardening.**
  - `lib.nixos.hardening` and `ProtectSystem=strict`.
  - `ReadWritePaths = [ stateDir ]`, where `stateDir=/var/lib/wordpress` holds Caddy `XDG_DATA_HOME`/`XDG_CONFIG_HOME`, `HOME`, `WP_CLI_CACHE_DIR` and `uploads/`. It is not under the web root and not served.
  - `PrivateTmp=true` per unit.
  - No `MemoryDenyWriteExecute` (JIT).
  - The VM test asserts that the journal has no permission errors from frankenphp or wp-cli.
- **Settings** (RFC 42 `settings`, non-secret): `WP_HOME`, `WP_SITEURL=${WP_HOME}/wp`, `WP_ENV`, `DB_*`, `DISABLE_WP_CRON=true`, `WP_TEMP_DIR=/tmp`, `TMPDIR=/tmp`, `WC_LOG_HANDLER`, `S3_BUCKET`, `S3_ENDPOINT`, `S3_PUBLIC_URL`, `S3_DIRECT_UPLOADS`, `S3_EXCLUDE_PREFIXES`, `WP_MAX_MEMORY_LIMIT`.

### 7.3 Routes (Caddyfile in `modules/nixos.nix`)
```
root * ${package}/web
@legacyLocal path_regexp ll ^/wp-content/uploads/((?:cwicly|fonts|wpcodebox|fluentform)/.*)$
redir @legacyLocal /app/uploads/{re.ll.1} 301
@legacyMedia path_regexp lm ^/wp-content/uploads/(.*)$
redir @legacyMedia {$S3_PUBLIC_URL}/{re.lm.1} 301
redir /wp-admin* /wp{uri} 301 ; redir /wp-login.php /wp/wp-login.php 308
rewrite /wp-content/plugins/* /app/plugins/{path.3...} (and themes)
handle /app/uploads/* {
  @private path_regexp ^/app/uploads/(woocommerce_uploads|gravity_forms|wpo_wcpdf[^/]*|wc-logs|fluentform/temp)/
  respond @private 403
  @exec path_regexp (?i)\.(ph(p\d?|tml|ar|ps)|cgi|pl|py|sh|htaccess|user\.ini)$
  respond @exec 403
  file_server                       # never php_server
}
@incPhp path_regexp (?i)^/wp/wp-includes/.*\.ph(p\d?|tml|ar)$
@tinymce path /wp/wp-includes/js/tinymce/wp-tinymce.php
respond @incPhp !@tinymce 404     (expressed as a not-matcher)
respond /xmlrpc.php /.env* /composer.* /config/* /wp-cron.php 404/403 (existing @notServed set)
php_server
```
- The local-prefix lists are rendered from `denyUploadPaths` and `localUploadPrefixes`.
- The client IP comes from `CF-Connecting-IP` (existing).
- The module VM test requests `/app/uploads/2024/01/x.php`, `/app/uploads/x.phtml` and `/app/uploads/woocommerce_uploads/a.zip` (expecting 403), and `/wp-content/uploads/2024/01/a.jpg` (expecting 301 to media).

### 7.4 Edge, backups, observability
- **Edge** (`wordpress-nix edge provision`, zone's account):
  - one tunnel per app;
  - a Cache Rule with long TTLs on `/app/themes/*`, `/app/plugins/*`, `/wp/wp-includes/*`;
  - anonymous HTML cached, with bypass on `wordpress_logged_in_*`, `wp-postpass_*`, `woocommerce_*` and `comment_author_*`;
  - a WAF login rate limit;
  - optional Access.
- **Backups:**
  - backup-core: nightly logical MariaDB dump plus JuiceFS uploads, hourly state, and a weekly rehearsal (plus `wp core is-installed` and a row count);
  - `hostPostCommand` runs the R2 → OVH media copy and the dev-restore dump upload (§4.13).
- **Observability.** Existing journald `logging.*` fields, plus log-alert rules:
  - PHP Fatal at a rate above 0 per 5 minutes;
  - `wordpress-migrate` failed or rolled back;
  - `wordpress-cron` failed 3 times in a row;
  - wp-cloud-files `offload failed`.

  Also:
  - a watchdog `/wp-json/` probe;
  - a daily `wp wp-cloud-files status --format=json` log line, alerting when local-only attachments stay above 0 for 24 hours.

### 7.5 Fleet sizing (working estimates; measured in M3 and M6)
- 35 brochure × 1.25 GB + 5 Woo × 2.5 GB ≈ **56 GB of Nomad reservations**. It must fit on 2 of 3 nodes during a failover.
- If node RAM does not allow that, the M3 options are:
  - the library adds Nomad memory oversubscription (`resources.memoryMaxMB`) and reserves the measured p95 RSS;
  - add a fourth node.
- Disk: about 5-10 GB of docroots per node after dedup.
- Eval-cost budget: under 2× today's cluster eval at 40 inputs, measured in M3.
- One tunnel per app; a shared tunnel is deferred until about 50 sites.

---

## 8. Conversion tool ("moonshot" in wordpress-nix)

### 8.1 Commands (one umbrella; no orchestrator; nothing writes to the legacy host)
| Command | Mirrors | Touches |
|---|---|---|
| `wordpress-nix legacy survey <ssh-host> [--path P \| --root /var/www --fleet] [--json\|--csv] [--compare <survey.json>]` | moonshot `survey_source`, fleet survey | read-only SSH and read-only SQL |
| `wordpress-nix legacy pull <ssh-host> --path P --into DIR [--db-only]` | `pull_content` + `dump_source_database` | local DIR |
| `wordpress-nix db audit <dump> [--survey F] [--strip --out F] [--accept ID]…` | `audit-mysql-dump` | local files |
| `wordpress-nix db rewrite-urls …` (§4.9) | `rewrite_media_urls` | the database behind `wp` on PATH |
| `nix run github:Avunu/wordpress -- --migrate [DIR] [--survey F] [--dump F]` (`wordpress-init`) | `frappe-init --migrate` | local tree |
| `wordpress-nix repo adopt <slug>` / `repo apply` / `repo doctor` | standards-core | GitHub |
| `wordpress-nix media provision <slug> --domain D [--adopt BUCKET]` | `provision.sh` R2 | Cloudflare R2 |
| `wordpress-nix media offload <slug> --from <ssh-host> --path P --dump F [--adopt]` | `migrate_media` | R2 + a local JuiceFS tarball |
| `wordpress-nix edge provision <slug> --domain D [--access]` | `provision_plane_tunnel`, Access | Cloudflare (zone's account): tunnel, `staging.` CNAME, WAF, Cache Rule, Access |
| `wordpress-nix site import <slug> --dump F [--files T] --home URL [--replace]` | `restore_database` | the cluster container (over SSH) |
| `wordpress-nix verify <slug> --against <ssh-host> [--via-socket] [--delta-since TS]` | `verify_migration` | read-only |
| `wordpress-nix cutover <slug> [--rollback]` | moonshot `cutover` | Cloudflare DNS and cache in the zone's account |
| `wordpress-nix site status [<slug>] --json` | new | read-only probes |
| `wordpress-nix audit` / `ci <step>` | §4.14 / standards-core | read-only / CI |

- **Secrets for the tool:**
  - `WORDPRESS_NIX_CLOUDFLARE_TOKEN`: the existing multi-account provisioning token, re-homed from the clan var to an age file in <cluster-repo> and decrypted by the operator;
  - `WORDPRESS_NIX_WORDFENCE_KEY`.
- There is no state file. Every external step probes remote reality (`703742d`). The only local records are `.wordpress-nix-backup/{survey.json, provenance.json, inventory.json, cutover-<slug>.json}`.

### 8.2 `legacy survey`
- **Gathers:**
  - WordPress, PHP and MariaDB versions; multisite; table prefix; locale;
  - active and inactive plugins and themes with versions;
  - `wp core|plugin verify-checksums` when wp-cli exists;
  - administrators with registration dates;
  - uploads size and non-attachment subdirectories, and PHP files in uploads;
  - Woo yes/no; DB size;
  - **`information_schema.TRIGGERS/ROUTINES/EVENTS/VIEWS` counts and definitions (read-only)**;
  - legacy-host specifics;
  - **the zone's Cloudflare status:** whether the zone is on Cloudflare in a full setup, its account, whether the provisioning token reaches it, existing Page/Cache/Transform rules, and whether APO is on.
- **Fleet mode** emits one CSV/JSON row per site with a complexity score: plugins × weights + Woo + premium count + writers + PHP gap + locale + `db-code` + "not on Cloudflare".
- **`--compare <T-7 survey.json>`** exits 1 on any change in core, plugin or theme versions or in the active set (§13.2).

### 8.3 `legacy pull`
- It runs code rsync and dump in **one SSH session** and records both timestamps in `provenance.json`.
- The code rsync excludes uploads, caches, `wflogs` and drop-ins.
- Dump: `mysqldump --single-transaction --triggers --routines --events --hex-blob`. The **full** raw dump is kept as evidence, encrypted with age to the operator's identity.
- It writes `survey.json` (§8.2) next to it, so `wordpress-init` can read the active set.

### 8.4 `db audit`
- **Input:** the full dump, cross-checked against the survey's executable-object counts.
- **Fails on**, and `--strip` removes, the following:
  - triggers, procedures, functions, events, `DEFINER` views, `CREATE USER`/`GRANT`;
  - `_application_passwords` and `session_tokens` usermeta;
  - `mainwp_child_*` options.
- **Reports** (each needs `--accept <id>`):
  - autoloaded options with obfuscation patterns;
  - PHP or `<script>` payloads in snippet tables (wpcodebox, code-snippets), widgets and header/footer options;
  - administrators not in the survey's confirmed list, and users whose role was raised;
  - a `siteurl`/`home` mismatch.
- **Exit codes:** 0 clean (or all accepted), 1 unaccepted findings, 2 bad argument. The existing `tools/audit-mysql-dump.py` already returns 1.

### 8.5 `wordpress-init` (scaffolder)
**Flags.**
- The shared set: `--version`, `--dry-run`, `--skip-lock`, `-y/--yes`, `--commit`, `--force`, `--name <slug>`, `--migrate`, `--init`, `--sync` (and `--check`), `--strict` (warnings become **exit 1**, as in frappe).
- `--home-url <url>` is **the `--default-site` equivalent**, so `lib.checks.commandFlags` passes.
- WordPress-specific:
  - `--php php83|php84|php85`;
  - `--survey <file>` / `--dump <file>`;
  - `--keep-plugin`/`--drop-plugin <slug>` (repeatable);
  - `--keep-inactive`;
  - `--keep-salts`;
  - `--accept-modified <slug>:{replace|vendor}`;
  - `--no-loosen`.
- Environment: only `WORDPRESS_NIX_*`.

**Mode detection.**

| Directory contains | Result |
|---|---|
| empty | `--init` (new Bedrock) |
| Bedrock plus a wordpress-nix flake | reconcile (no-op if nothing drifted) |
| classic WordPress | migrate |
| v2 wordpress-nix site | migrate; old flake → backup |
| foreign `flake.nix` | exit 4 (`--force` writes `flake.nix.wordpress-nix`) |
| multisite | exit 6 |

**Migrate steps.**
1. **Guards.**
   - Nested repo: exit 8 unless `--force`.
   - WordPress below 6.6: exit 5.
   - **No `--survey`/`--dump`** (auto-discovered in `DIR/.wordpress-nix-backup/`) **and no `--keep-inactive`: exit 6**, because the active set cannot be known.
   - Read `$table_prefix` → `DB_PREFIX`.
2. **Classify** every directory in `wp-content/{plugins,themes,mu-plugins}` and every drop-in.

   **Dispositions:**
   - **composer.** The wp.org API knows the slug, `Update URI` is empty or w.org, the author matches, `verify-wp-checksums` passes for the installed version, and the version is in WP Packages. **Themes:** the locked zip is downloaded and diffed against the installed copy. Any difference raises `modified`.
   - **avunu.** Directory name *and* header match an Avunu Packagist package.
   - **commit.** Premium or custom, or in `premium-slugs.json`.
   - **drop.**
     - Anything on the drop list, which includes jwt-auth (fork), mainwp*, core-rollback, wp-console, download-plugin and the rest of §4.8.
     - Drop-ins, `*.bak` and empty directories.
     - Inactive plugins and themes unless `--keep-inactive`. The parent of an active child theme is kept.
     - Dropped items go to backup and are recorded in `.github/wordpress-nix.json` `dropPlugins`.
   - **platform.** `platform-*.php` is ignored.

   **Flags:**
   - `modified`: blocks.
   - `missing-code`: active in the database but absent on disk; blocks unless the plugin is on the drop list.
   - `collision`, `not-in-registry`, `delisted`.
   - `writes-to-disk`, `mpdf-tempdir`, `non-english-locale`, `php-floor`.
   - `suspicious-code` in committed code.
   - `db-code` (WPCodeBox and similar): blocks until `--keep-plugin`/`--drop-plugin` is decided. For WPCodeBox the default plan commits `wpcodebox_functionality_plugin` and lists `wpcodebox2` in `developmentOnlyPlugins`.
   - `salt-dependent`.
3. **Print the plan.**
   - The disposition table and flags.
   - Runtime-writer dispositions (§4.8).
   - The tracked-secret audit.
   - The **existing-repo note**, which runs `repo adopt --check` when a remote exists.

   `--dry-run` → exit 0. Declining → exit 6.
4. **Pipeline.** Check-then-act; the git index plus the tree are the state.
   1. Create `.wordpress-nix-backup/` (gitignored, README, provenance). Run `git init` if needed.
   2. Move legacy core, `wp-config.php`, `wp-content/{uploads,cache,upgrade}`, dropped items, drop-ins and `*.sql` into backup. **Nothing is deleted.**
   3. Render Bedrock from the pinned input (`keep=True`), then the template files: `nix/vendor-hash` seeded with `lib.fakeHash`, and `.github/wordpress-nix.json`.
   4. Move committed code to `web/app/...` (`git mv` semantics when tracked).
   5. Reconcile `composer.json` surgically: exact installed versions, `avunu/*`, `wp-plugin/two-factor`, `config.platform.php`, and the `only` guard.
   6. Write the managed `.gitignore` block. Run `verify_not_ignored` and `verify_secrets_visible`.
   7. **`git add -A`** (capped at 20000 files), **before any Nix command**, because Nix flakes in a git repo see only tracked files.
   8. Unless `--skip-lock`:
      1. `composer update --no-install --no-scripts`;
      2. `git add composer.lock`;
      3. `nix run .#relock`;
      4. `nix flake lock`;
      5. `git add nix/vendor-hash flake.lock`.

      **A failure here exits 1** with the failing command and the "rerun" hint. A `fakeHash` would fail CI with certainty.
   9. `--sync`: render the standards-core managed files, then `git add`.
   10. Assert that `flake.nix`, `composer.{json,lock}`, `nix/vendor-hash`, `config/application.php`, `web/wp-config.php`, `.gitignore` and `.github/wordpress-nix.json` are indexed, and that **`nix/vendor-hash` is not `lib.fakeHash` unless `--skip-lock`**. With `--commit`, commit `feat: convert to Bedrock (wordpress-nix)`.
   11. Second commit, with `--commit` and unless `--no-loosen`:
       - loosen `wp-plugin/*` and `wp-theme/*` to `^x.y`;
       - `composer update --lock`;
       - relock;
       - `chore(deps): loosen plugin constraints`.
   12. Report warnings, runtime writers and dropped items, then "Next steps" in the shared wording: `direnv allow`, `devenv up`, `wp restore` or `wp db import`, then `wordpress-nix repo adopt <slug>`.

**Exit codes** (the shared table; `--sync`/`--check` codes copied verbatim from frappe-nix `docs/reference/scaffolder.md`):

| Code | Meaning |
|---|---|
| 0 | done, or a dry run |
| 1 | generic failure, `--strict` warnings, relock failure |
| 2 | `--init` into a non-empty directory |
| 4 | foreign flake |
| 5 | version unsupported |
| 6 | wrong shape (multisite), declined, or no survey/dump for the active set |
| 7 | nothing to migrate |
| 8 | nested repo |

`--sync`: 0 clean, 1 drift, 2 config, 3 environment. `wordpress-nix` subcommands use the standards-core codes: 0 ok; 1 refusal or gate failure; 2 bad argument; 3 GitHub or git error.

### 8.6 Data commands
- **`media offload`.**
  - Inputs: the attachment index (`_wp_attached_file` plus `_wp_attachment_metadata` sizes) from the audited dump, **filtered by the `S3_EXCLUDE_PREFIXES` list**.
  - `rclone copy --files-from` those paths from legacy sftp to R2 (resumable), then `rclone check`.
  - Everything else (`cwicly/`, `fonts/`, `wpcodebox/`, private directories, unknowns) is packed into a local tarball for JuiceFS. Executables are never copied, except the exact WPCodeBox stub, and are reported.
  - It writes `inventory.json`, the directory classes that feed `db rewrite-urls`.
  - **`--adopt`** is auto-enabled when the legacy config defines `S3_BUCKET`:
    - attachments whose objects already exist (HEAD or `rclone check`) are skipped;
    - source missing but present in the bucket is OK;
    - an attachment in neither place fails with a list.
- **`edge provision`** (zone's account):
  - finds or creates the tunnel `<cluster>-<slug>` and prints its id and credentials JSON for `avc secrets provision` (`<slug>-cf/cloudflared`);
  - creates the proxied `staging.<domain>` CNAME;
  - creates or updates the WAF rate limit and the HTML Cache Rule;
  - optionally creates the Access app.

  It refuses if the zone is not in a full Cloudflare setup, or if the token cannot reach the account.
- **`site import`**:
  1. Finds the node running the allocation (`avc` / Nomad status).
  2. Refuses if `wp core is-installed` succeeds and `--replace` was not given. With `--replace`, it first runs `cluster-backup <slug>`.
  3. Streams the dump over SSH into `nixos-container run <slug> -- runuser -u wordpress -- wp db import -`.
  4. Streams the tarball into `runuser -u wordpress -- tar -xz -C /var/lib/wordpress/uploads`.
  5. Then, as `wordpress`:
     - `db rewrite-urls --to-home <--home>`;
     - deactivates `dropPlugins` with `wp eval 'deactivate_plugins([...], true)'` (works for files that no longer exist);
     - `wp option update woocommerce_file_download_method force` (Woo only);
     - resets privileged passwords (§4.11);
     - `wp core update-db`, `wp rewrite flush`.

  No temporary files are left on the node. It is idempotent with `--replace`.
- **`verify`:**
  - per-table row counts and an administrator diff against the survey;
  - `wp wp-cloud-files status` showing 0 local-only, and `verify --sample 200`;
  - `verify-wp-checksums`;
  - HTTP posture (xmlrpc 403, `/wp-cron.php` not served, `/.env` 404, uploads PHP 403, `woocommerce_uploads` 403, the `/wp-admin` redirect, the `/wp-content/uploads` 301, media on `media.<domain>`, Cwicly CSS 200);
  - sitemap-sampled content parity;
  - the asset 200-check.

  Options:
  - `--via-socket`: runs the HTTP checks on the node against the unix socket with `Host: <domain>` (pre-flip).
  - `--delta-since TS`: compares row maxima for `wp_posts`, `wp_comments`, `fluentform_submissions` and `give_*` on legacy against v3 and lists late entries.
- **`cutover`.**
  - Snapshots the previous DNS records exactly (proxied flag, TTL), plus the zone's Page, Cache and Transform rules and APO state, into `cutover-<slug>.json`, which is also pasted into a <cluster-repo> issue.
  - Disables legacy-host-era HTML cache rules and APO.
  - Sets proxied CNAMEs for the apex and `www` to the tunnel.
  - Purges the zone.
  - `--rollback` restores the records and rules bit-exact and purges again.

### 8.7 Tests
- **`tests/make-classic-wp.sh`** builds a synthetic classic install covering:
  - pristine and modified wp.org plugins, and a **modified wp.org theme**;
  - a premium plugin with `Update URI: false`;
  - the jwt-auth fork collision (expect `drop`);
  - a version missing from the registry;
  - an inactive plugin, and a child plus parent theme;
  - an **active-but-missing plugin**, both dropped (`mainwp-child`) and not dropped (`missing-code`);
  - WPCodeBox2 with its functionality plugin and `uploads/wpcodebox/index.php` stub;
  - legacy-host mu-plugins, `object-cache.php` and `advanced-cache.php`;
  - a tracked `wp-config.php` and `*.sql`;
  - uploads with `cwicly/`, `fonts/`, `woocommerce_uploads/`, `fluentform/` and a planted `x.php`;
  - prefix `wpx_`;
  - a survey JSON;
  - variants: a v2 flake, multisite, too-old core, a nested repo, and a missing survey.
- **`tests/migrate-classic.{nix,sh}`** runs offline with `-y --skip-lock` and `WORDPRESS_NIX_CATALOG`. It asserts:
  - dispositions and flags, `composer.json` and `.gitignore`;
  - the backup is complete;
  - a second run is a no-op;
  - exit codes 4, 5, 6 (multisite and no survey) and 8;
  - rendered files are indexed before the lock step.
- **`selftest-scaffold.yml`** (networked, weekly) runs without `--skip-lock` and asserts that `nix/vendor-hash` is not `fakeHash` and that `nix build` succeeds.
- **pytest:**
  - classify, gitignore, composer_json;
  - lockcheck (real Bedrock + wp-cloud-files lock);
  - the audit range matcher;
  - the media list builder, including exclusions and adopt;
  - the rewrite-urls regex builder (plain, JSON-escaped, other-host untouched);
  - exactness of the cutover record;
  - the `composer validate` flags against stock Bedrock.
- **`tests/audit-mysql-dump`:**
  - an admin-minting trigger, an event, a procedure, a DEFINER view;
  - an obfuscated autoloaded option;
  - application passwords, a MainWP key, a PHP snippet row.

---

## 9. Cluster integration

### 9.1 Kind `wordpress` (rewrite of `modules/kinds/wordpress.nix` in `kinds/frappe.nix` structure)
```nix
wordpress = { package; domain; extraDomains ? []; stagingDomain ? null; settings ? {};
              profile ? "brochure";            # brochure | woocommerce → threads, limits, pool, memoryMB
              access.adminPaths ? false; applicationPasswords ? false;
              media = { bucket; endpoint; publicUrl; backup ? true; }; };
```
- `module` imports `inputs.wordpress-nix.nixosModules.default` and sets `services.wordpress-nix`:
  - `enable`, `package`, `database.createLocally = true`;
  - `environmentFiles = [ "/run/secrets/env" ] ++ optional (!isPreview) "/run/secrets/cloud-files"`;
  - `settings`;
  - `cron.enable = !isPreview`, `migrate.enable = true`;
  - `socketPath = "/run/wordpress/wp.sock"`.
- Storage: `juicefsPaths."/var/lib/wordpress/uploads"` and `localState."/var/lib/mysql"`.
- Secrets: the generators from §4.17.
- Previews: `preview.*` per §4.12.
- **Tunnel:**
  - `tunnel.id`, `tunnel.accountId` (the zone's account, from `edge provision`) and `tunnel.credentials = { generator = "<slug>-cf"; file = "cloudflared"; }`;
  - `tunnel.ingress` for **`domain`, `extraDomains` and `stagingDomain` from the first deploy**. DNS decides which one receives traffic.
- `resources.memoryMB` from `profile` (§7.2).
- `backup.hostPostCommand`: the media copy and the dev-restore dump (§4.13).
- It lifts the `wpOwned` secret-ownership fix from the v2 `mk-wordpress-site.nix`.

### 9.2 Library changes beyond the kind
- **`tunnel.accountId`** (new, default `null` = `cf.accountId`). When it is set and differs from `cf.accountId`, `lib/cloud.nix` emits no tunnel resource, no import and no DNS for that app, because the tunnel is managed outside OpenTofu.
  - Assertion: no ingress hostname of such an app may fall inside `cloudflare.zones`, so DNS has a single owner.
  - Tests: an eval test for both branches.
- **`backup.hostPostCommand`** (new, generic): runs host-side on the backing-up node after a successful snapshot, with `$BACKUP_STAGING` and host secrets available. Test: `tests/backup-roundtrip.nix` extension.
- **`checks.project-inputs-follow`** (§4.7) and **`checks.wordpress-site-smoke`** (boots `tests/fixtures/bedrock-site` in a container through the socket).
- **Template `flake-update.yml`:** all branch-following inputs minus `.github/flake-update-hold`, plus the `flake-update-split` job (§4.7). Test: an `act`- or VM-level fixture where one injected input fails eval.
- **Deletions:**
  - `modules/kinds/wpdb.nix`, `lib/mk-wordpress-site.nix`;
  - `wordpressPlanes` in `modules/apps.nix:38-77`;
  - the v2 `wordpress-<slug>-git` generator;
  - `tests/fixtures/apps/demo-site.json`, replaced by `demo-site.nix`;
  - the `container-isolation` and `mk-cluster` assertions are updated.
- **`lib/log-alert-rules.nix`:** the §7.4 rules. **cluster-watchdog:** a `/wp-json/` probe.
- Merge **`feat/attic-cache`** as is. The "retention pinning" wording is dropped; retention comes from the runtime reference (§4.3).
- README: the single-plane WordPress section, the sizing table, and no `wpdb`.

### 9.3 <cluster-repo>
- **Prerequisite gate G1:**
  - nodes 2 and 3 in `hosts/`;
  - the cluster-ci Worker deployed (`topology.nix:95`);
  - Attic live;
  - a real-provider `avc cloud apply`;
  - a `cluster-restore` rehearsal;
  - a second YubiKey master identity.
- **Per site:**
  - the input (§4.7) and `apps/<slug>.nix`;
  - `avc secrets provision` before the app PR;
  - client zones are **not** added to `cloudflare.zones`.
- Pull the updated `flake-update.yml` and create `.github/flake-update-hold` (empty).
- Re-home the Cloudflare provisioning token as `secrets/cloudflare-provision.age`, for operators only, not for nodes.

---

## 10. Repo-by-repo work breakdown

### 10.1 wordpress-nix (`Avunu/wordpress`): all automation lives here
- **`flake.nix`:**
  - add the `bedrock` input (`flake = false`, tag-pinned) and `python-devops-nix`;
  - remove the `gitium` and `wordpress-sqlite-anywhere` inputs;
  - outputs:
    - `lib.{mkFlake, mkBedrockSite, mkPhp, mkFrankenphp}`;
    - `nixosModules.default`, `flakeModules.default`;
    - `templates.default` (`templates/bedrock-site`);
    - packages `wordpress-init` (default app), `wordpress-nix`, `audit-mysql-dump`, `verify-wp-checksums`, `restore-core-keys`.
- **New files:**
  - `lib/mk-bedrock-site.nix` (§4.3);
  - `lib/relock.nix`.
- **`modules/flake-module.nix`:**
  - options `wordpress-nix.site = { name; php; src; vendorHashFile; productionOnlyPlugins; developmentOnlyPlugins; activeTheme; }`;
  - pure checks only (§6.1).
- **`modules/nixos.nix`, v3 contract** (`Release-As: 3.0.0`):
  - New options:
    - `package`, `environmentFiles`, `settings`, `uploadsDir`;
    - `denyUploadPaths` (default `woocommerce_uploads gravity_forms wpo_wcpdf* wc-logs fluentform/temp`);
    - `localUploadPrefixes` (default `cwicly fonts wpcodebox fluentform`);
    - `profile` → `num_threads`, `memory_limit`, `WP_MAX_MEMORY_LIMIT`, `innodb_buffer_pool_size`;
    - `OOMScoreAdjust` values.
  - Keep `database.createLocally`.
  - `migrate.*` from `lib.nixos.migrate` with `verifiers.mysqldump` and an `is-installed` check phase.
  - Caddyfile per §7.3. `ReadWritePaths=[ stateDir ]`. Each unit keeps its own PrivateTmp; `WP_TEMP_DIR=TMPDIR=/tmp`.
  - Delete `source.*`, `saltsFile`, `database.type`/`turso.*`, `gitium.*`, `plane.*`, `tablePrefix`, and the generated `wp-config.php`.
- **`modules/devenv.nix`:**
  - Bedrock-only: `composer install` in the tree, serving `web/`, with a dev `.env`.
  - Scripts:
    - `wp`;
    - `wp restore [--at] [--no-neutralize]` (`lib.backups.fetch` with a WordPress layout parser);
    - `wp-admin-user`, `wp-reset`, `relock`, `verify-checksums`;
    - `wpcodebox-export` (regenerates and copies the functionality plugin into `web/app/plugins/`).
  - Delete SQLite and `wp-core-reset`.
- **`mu-plugins/platform-environment.php`** gains:
  - the constants bridge;
  - update-check and Site Health suppression;
  - `pre_wp_mail` outside production;
  - `productionOnlyPlugins` and `developmentOnlyPlugins` filtering of `option_active_plugins`;
  - two-factor enforcement;
  - the application-password switch;
  - the mpdf temp filters;
  - `wp_handle_upload_prefilter` denying executables.

  Delete `platform-public-plane`, `platform-admin-plane`, `platform-gitium` and `platform-performance-keys`.
- **Delete:**
  - `lib/site.nix`, `lib/mk-site-docroot.nix`, `lib/wordpress-core.nix`;
  - the SQLite and Turso converters and `mkTursoPublisher`;
  - `modules/containers.nix`, `docker-entrypoint.sh`, `conf/wp-config.php`, `oci.yml`;
  - `lib/sh/*` and the bash `lib/init.nix`;
  - `templates/site/`, `tests/plane.nix`, `site-publish.yml`.
- **`tools/audit-mysql-dump.py`:** the §8.4 extensions.
- **Python `py/wordpress_nix_tools/`.** A standards-core `Product` (`product.py`: decision rules, `relock_patch_paths = ["nix/vendor-hash"]`, the verify-relock hook):
  - `wordpress_init/` (args, detect, classify, wporg, plan, pipeline, template, gitignore, composer_json, secret_files, presets, state, ui, proc);
  - `legacy/`, `db/` (audit, rewrite_urls), `media/`, `edge/`, `site/` (import, status, verify, cutover), `repo_adopt.py`, `audit.py`, `lockcheck.py`.
- **Data:** `data/{wordpress-presets,drop-plugins,host-mu-plugins,runtime-writers,salt-dependent-plugins,premium-slugs,db-code-plugins}.json`.
- **Standards profile `wordpress-site`.** Managed files: `dependabot.yml`, the workflows, the `.gitignore` block, `.github/wordpress-nix.json`, `.editorconfig`, `.pre-commit-config.yaml`, `committed.toml`, and `repo-policy/site/`, which requires `ci / flake`, `ci / smoke` and `ci / audit`.
- **Reusable workflows** (SHA-pinned): `site-ci.yml`, `site-deps.yml`, `site-audit.yml` (daily vulnerabilities, weekly premium staleness, weekly `--rebuild .#vendor`, nightly cluster-compat), `rollout.yml`, `selftest-scaffold.yml`.
- **Tests:**
  - §8.7;
  - `tests/bedrock-build.nix`, an offline fixture from a local Composer `artifact` repository. It asserts the autoloader files, `web/wp`, a stable hash over two builds, and `vendor-src`;
  - `tests/module.nix` (VM):
    - install, activate all, curl;
    - docroot write and `wp plugin install` both fail;
    - the §7.3 route assertions;
    - first boot with an empty DB: migrate exits 0;
    - a migrate-rollback scenario;
    - S3 offload to MinIO with env-only `S3_*`, using a wp-cloud-files 0.6 branch build through a Composer `path` repository until Packagist has 0.6.0;
    - a Woo-downloadable attachment stays local;
    - no permission errors in the journal.
- **Docs:**
  - `docs/getting-started.md`;
  - `docs/scaffolding/migrate.md` ("never write to the legacy server", plus the D11 exceptions);
  - `docs/development/{restore,premium-plugins,wpcodebox}.md`;
  - `docs/production/{cluster,cutover,releases,v3-decisions}.md`;
  - `docs/reference/{scaffolder,wordpress-nix-cli}.md`, with the `--sync` exit codes copied verbatim.

### 10.2 nixos-cluster-orchestration
§9.1 and §9.2.

### 10.3 <cluster-repo>
§9.3.

### 10.4 wp-cloud-files 0.6.0 and the other Avunu plugins
1. **`composer.json`:** `type: wordpress-plugin`, no `version`, `minimum-stability: stable`, `prefer-stable`, `composer/installers` in `require`. Publish on Packagist as `avunu/wp-cloud-files`.
2. **`index.php`:**
   - require its own `vendor/autoload.php` only if present, else require `class_exists(\Aws\S3\S3Client::class)`;
   - load plugin-update-checker only when its own `vendor/` exists and `!DISALLOW_FILE_MODS`. This defuses #89.
3. **Config:** `Config::get()` reads the constant, else `getenv()`. Replace every `defined('S3_*')`.
4. **Exclusions:**
   - `S3_EXCLUDE_PREFIXES`, defaulting to `woocommerce_uploads/,fluentform/,gravity_forms/,wpo_wcpdf/,wc-logs/`;
   - a filter `wpcf_should_offload(bool, string $relPath, int $attachmentId)`.

   Applied in `MediaHandler::processMedia`, `ThumbnailHandler`, `DirectUploadProcessor`, CLI `migrate` and `UrlRewriter`, so excluded files are neither uploaded, unlinked nor rewritten.
5. **`DocumentThumbnailer`:** DomPDF via `InstalledVersions::getInstallPath`; all temp files under `get_temp_dir()`.
6. **Security:** refuse executable extensions and MIME types; never overwrite an existing key without `--force`.
7. **CLI:**
   - `status --format=json`;
   - `verify [--sample N|--all]`;
   - `inventory --json` (attachment / public-generated / private / unknown);
   - `migrate-urls --exclude-prefix` (regex lookahead, plain and JSON-escaped variants). The per-object `--only-offloaded` is dropped.
8. **Tests:**
   - Bedrock-layout integration;
   - env-only configuration;
   - exclusion (a Woo downloadable attachment produces no R2 object and the local file remains);
   - fonts not rewritten;
   - executable refusal;
   - direct upload with CORS.
9. **Hygiene:** fix #81; merge #96 and #104; close #89.
10. **Same Composer and Packagist treatment** for `wordpress-cloudflare-email` → `avunu/cloudflare-email` (with a getenv fallback) and `gutenberg-downgrade`. jwt-auth is not published.

### 10.5 python-devops-nix / gitops
- No forks. Add wordpress-nix to `docs/conventions.md` (gate: `flake` + `smoke`).
- New product hooks in standards-core, each with a filed issue first:
  - `Product.relock_patch_paths`;
  - the `verify-relock` callback;
  - a "deployment project" profile (no release-please).
- `lib.backups.fetch`: a WordPress layout parser (`wordpress/<slug>/<ts>.sql.gz`).
- `avunu/gitops` (wordpress#86) stays the home of the generic Dependabot auto-merge that wordpress-nix itself uses. Site repos use `site-deps.yml`.
- The exit-code table is unchanged.

### 10.6 nixos-hosting-cloud retirement
- **Now:** a README banner on `scripts/moonshot/` saying it is superseded.
- **Port first:** survey, dump, audit, rclone, verify, the Cloudflare account helpers, tunnel creation without login, Access, the `push_repo` verify-on-remote rule, and the cutover record.
- **After v2-pilot cuts over:** one commit `refactor!: retire moonshot v2`, deleting:
  - `scripts/moonshot/`, `lib/moonshot.nix`, `lib/mk-wordpress-site.nix`;
  - the wordpress and wpdb kinds, `apps/<v2-pilot>.json`, `apps/pilot.json.example`, `apps/wpdb.nix`;
  - `vars/shared/wp-<v2-pilot>{,-git}`;
  - `site-content-rev.yml`.
- **Other repos:** archive `wordpress-moonshot`; delete the `wordpress-nix-oci` duplicate clone.

---

## 11. Milestones with exit criteria

**M0, decisions and groundwork (about 1 week)**
- §2 signed off as `docs/production/v3-decisions.md`.
- **v2-pilot, in this order (D11):**
  1. mint an account-owned, bucket-scoped key for `<existing-bucket>`;
  2. a human puts it into the live legacy wp-config through the legacy-host panel;
  3. a put/get/delete probe through the live site (upload and delete a test image);
  4. only then revoke the user-owned key.
- **Remediate the live site now** (human, documented):
  - drop the malicious trigger and any routines or events;
  - delete minted administrators;
  - rotate salts and all administrator passwords;
  - delete application passwords;
  - scan files against `wp core|plugin verify-checksums`.
- **Tear down v2 residue:** the 2 v2 tunnels, the Access app, the `staging.` CNAME, the legacy node's `wpdb` container (after confirming it is empty), and the `wp-v2-pilot-git` deploy key. Tag `site-<v2-pilot>` as `archive/v2-moonshot`.
- A shell prototype of `legacy survey` runs on every legacy-host server and produces the **fleet inventory CSV**, including the Cloudflare zone, account and token-reach columns.
- Re-check against current Cloudflare docs: R2 versioning is absent, CORS rule limits, cross-account CNAME behaviour.
- Land the hygiene drafts wordpress#86, orchestration #14 and wp-cloud-files #104.
- *Exit:*
  - decision record merged;
  - inventory exists;
  - v2-pilot on the new key with uploads working, and the old key revoked;
  - v2-pilot remediation recorded.

**M1, Bedrock build and module (wordpress-nix)**
- `mkBedrockSite`, `.#relock`, flake module, fixture, v3 module, `platform-environment.php`, `verify-wp-checksums`.
- *Exit:*
  - pure `nix flake check` is green on the self-hosted runners;
  - **`vendor/autoload.php`, `vendor/composer/autoload_real.php`, `InstalledVersions.php` and `web/wp/wp-includes/version.php` are present in the package**;
  - the install hook's rewriting of `installed.php` and `InstalledVersions.php` is confirmed (or the fallback recorded);
  - vendor hash stable across two clean builds **and across two nixpkgs revisions** (composer version);
  - plugin bump + `.#relock` builds;
  - `composer-validate` passes on stock Bedrock with exact pins;
  - module VM test passes, including env-only S3 to MinIO through the 0.6 branch `path` repository, the uploads-PHP 403s, the empty-DB first boot, and no journal permission errors;
  - a tampered fixture plugin fails `verify-wp-checksums`;
  - `development.php` `DISALLOW_FILE_MODS=false` confirmed;
  - two-factor filter names confirmed.

**M2, wp-cloud-files 0.6.0 and the Avunu packages on Packagist** (parallel with M1; **a hard prerequisite of M4's exit**)
- *Exit:*
  - integration suite green under the Bedrock fixture;
  - env-only configuration;
  - exclusions (Woo downloadable stays local; no R2 object);
  - `migrate-urls --exclude-prefix` leaves `wp_font_face`, Cwicly and private URLs on `/app/uploads`;
  - executables refused;
  - `avunu/wp-cloud-files`, `avunu/cloudflare-email` and `avunu/gutenberg-downgrade` resolvable from Packagist.

**M3, cluster library** (parallel with cluster bring-up)
- The kind, `tunnel.accountId`, `backup.hostPostCommand`, `project-inputs-follow`, `wordpress-site-smoke`, the flake-update hold/split, alerts, deletions, the Attic merge.
- *Exit:*
  - `mk-cluster` VM test boots the demo site through the socket;
  - a preview slot (forced to 1) serves with mail and cron off and S3 read-only;
  - `cluster-restore --db-only` works;
  - the flake-update test with one injected failing input merges the others and opens the issue;
  - `tunnel.accountId` eval tests pass;
  - eval time and memory measured at 1 site and at 40 synthetic inputs;
  - node RAM against §7.5 reservations decided.

**G1, cluster live gate** (<cluster-repo>, §9.3). Required before M5b and M6.

**M4, scaffolder and data tools** (needs M2 for real-site locks)
- *Exit:*
  - offline test green and idempotent;
  - `legacy survey` + `pull` + `--migrate --dry-run` on brochure-pilot, avunu, woo-flagship and v2-pilot give plans with zero unclassified items, and every flag reviewed by hand (including brochure-pilot's `mainwp-child` and `core-rollback` → drop, and WPCodeBox `db-code`);
  - a **networked full conversion of brochure-pilot without `--skip-lock`** produces a non-fake `nix/vendor-hash` that builds;
  - the `media offload --adopt` dry run on v2-pilot reports zero "in neither place";
  - Fluent Forms serving behaviour verified per installed version.

**M5a, site CI and update pipeline on `Avunu/site-fixture`** (before G1; cache = private Cachix through `WORDPRESS_NIX_CACHE`)
- *Exit:*
  - a real Dependabot Composer PR is relocked, auto-merged, and the merge commit's `main` push uploads to the cache, **all with zero human action**;
  - a **github-actions Dependabot PR (no relock) also auto-merges** with `ci / audit` green;
  - a **`php-libs` group PR** passes `verify-relock` and auto-merges;
  - a foreign dist host and an owner mismatch are each refused;
  - an injected vulnerable version is flagged by `ci / audit`, and `site-audit` opens an issue;
  - the `registries:` question is resolved.

**M5b, end-to-end deploy** (after G1)
- *Exit:*
  - a fixture-site Composer bump → Worker → flake-update → merge-gate → deployed with zero human action;
  - a wordpress-nix release goes through the §6.5 ordering;
  - **a cluster build with the Attic substituter disabled and a mutated fixture zip fails loudly**, and `site-audit --rebuild` opens the security issue;
  - Attic still holds the vendor FOD of a site untouched for longer than the retention window (simulated by a shortened retention in a test cache).

**M6, pilot (brochure-pilot)**
- *Staging gate:*
  - row counts match; zero new administrators; application passwords gone;
  - `wp-cloud-files verify --all` clean;
  - 100% status-code parity on sampled URLs; asset check clean; `/wp-content/uploads/*` 301s resolve;
  - a Fluent Forms upload lands on JuiceFS, **and an admin can open it from the entry and from the notification email**;
  - a Cwicly editor save regenerates CSS, and the CSS survives a container move;
  - **a WPCodeBox snippet edited in devenv, exported, committed and merged reaches production**;
  - no unexplained EROFS/EACCES over a 7-day staging soak;
  - a load test with concurrent admin exports produces no OOM of mysqld;
  - a forced migrate-rollback drill passes;
  - a DNS rollback restores records and rules bit-exact in a test zone.
- *Exit:*
  - 14 days in production without incident;
  - one Dependabot plugin bump and one wordpress-nix rollout deployed automatically;
  - §7.5 updated from measurements.

**M7, waves** (§12). **M8, retirement** (§10.6). *M8 exit:* `rg -i 'gitium|plane\.role|wpdb|content\.rev|turso'` is empty across wordpress-nix, orchestration and hosting-cloud, history and docs excepted.

Dependency summary:
- M0 → M1 ∥ M2 ∥ M3.
- M2 → M4.
- M1 → M5a.
- M3 + G1 → M5b.
- M4 + M5a + M5b → M6 → M7 → M8.

---

## 12. Pilot and fleet waves

**Pilot: brochure-pilot.**
- **Why:**
  - pure legacy LiteSpeed brochure site, no flake history;
  - the fleet's common premium core (Cwicly, HappyFiles Pro, WPCodeBox2, Fluent Forms Pro, FluentCampaign Pro, Relevanssi Premium);
  - exercises Cwicly CSS, Fluent uploads and the WPCodeBox flow;
  - no payments;
  - DNS-only rollback.
- **Pilot-specific items:**
  - `mainwp-child` and `core-rollback` are active in the database but missing on disk; both are dropped, and the site is disconnected from any MainWP dashboard;
  - the existing `Avunu/site-<pilot>` holds SQL dumps, so `repo adopt` renames it to `-legacy`, archives it, and creates a fresh repo (D6);
  - `the legacy host's edge-cache mu-plugin` means the zone's cache rules and APO are snapshotted and disabled at cutover.

| Wave | Sites | Notes |
|---|---|---|
| Pilot | brochure-pilot | Full runbook; rollback rehearsed on staging |
| 1 | avunu (if live on WordPress) | Starts after the pilot's 14-day soak. Internal. Drop SQLite and wrangler leftovers; classify wpcd-* (likely drop); `stripe-direct-debit-woocommerce` makes it a light Woo rehearsal. Repo has no remote, so a new repo |
| 2 | v2-pilot | **Runs in parallel with wave 1**; does not wait for wave 1's soak. Already remediated (M0). **Recaptured live.** `media provision --adopt <existing-bucket>` (keeps `media.<v2-pilot-domain>`, adds CORS). `media offload --adopt`. Give ×9 add-ons committed. Drop `captivate-sync-sqlite-compat` and `zzz-repro-fixups`. Stripe webhook check. `repo adopt` (D6). Then §10.6 retirement |
| 3 | legacy-host long tail, by complexity score | 3-5 per week, `--dry-run` plans reviewed up front. Zones not on Cloudflare are onboarded first (a separate task) or deferred. Advance only with zero rollbacks in the previous batch and tool time under 2 hours per site |
| 4 | woo-flagship, then other Woo sites | PHP 8.2 → 8.4 in devenv first. cf-images → wp-cloud-files (or a wp.org package; per site). Redis dropped. WooCommerce.com extensions committed (`woo-update-manager` dropped). Private downloads on JuiceFS with `force`. Gateway sandbox test. Maintenance-window cutover. Woo minors on review |
| Hold | held-site | Excluded while `jx-rebuild/` is in progress |

Out of scope: sites already leaving WordPress (static or Next.js rebuilds).

---

## 13. Cutover and rollback runbook (per site)

### 13.1 Preparation (T−7 days)
1. Review the survey row. Require the zone to be on Cloudflare in a full setup and reachable by the token. Lower the apex and `www` TTL to 60 s.
2. **Human, in the legacy-host panel (D11):** disable WordPress auto-updates (core, plugins, themes), legacy-host auto-updates and plugin installs. Record it.
3. `wordpress-nix legacy pull <host> --path P --into ~/sites/<slug>`: code, full dump and `survey.json` in one session.
4. `wordpress-nix db audit dump.sql --survey survey.json --strip --out import.sql`. Every finding is accepted with `--accept` or the run stops.
5. In `~/sites/<slug>`: `nix run github:Avunu/wordpress -- --migrate --dry-run`, review, then rerun with `-y --commit`.
6. `wordpress-nix repo adopt <slug>`: new repo or PR per D6. Then `repo apply`. CI goes green.
7. `wordpress-nix media provision <slug> --domain <domain> [--adopt BUCKET]`, then `wordpress-nix edge provision <slug> --domain <domain> [--access]`. Together they print the `<slug>-cloud-files` and `<slug>-cf` secret fragments.
8. In <cluster-repo>:
   - `avc secrets provision`;
   - a PR adding the input and `apps/<slug>.nix` with `domain = "<domain>"; extraDomains = [ "www.<domain>" ]; stagingDomain = "staging.<domain>"; settings.WP_HOME = "https://staging.<domain>";`;
   - merge after the gate. The empty DB boots cleanly through the `is-installed` guard.
9. `wordpress-nix media offload <slug> --from <host> --path P --dump import.sql [--adopt]`.
10. `wordpress-nix site import <slug> --dump import.sql --files private.tar.gz --home https://staging.<domain>`. This runs the §4.9 sequence. Then regenerate the Cwicly CSS.
11. `wordpress-nix verify <slug> --against <host>`.
12. Human pass: forms (including opening uploaded files), donation or checkout in test mode, the WPCodeBox flow, client UAT.

### 13.2 Cutover day
Window: 1 hour for brochure sites. Woo sites get a checkout maintenance window at the lowest-traffic hour, with client sign-off.
1. **T−1 h:** cluster PR with `settings.WP_HOME = "https://<domain>"`. Merge after the gate and wait for the deploy. Staging now redirects to the real domain, as expected. **This is the only merge gate of the day, and it runs before any freeze.**
2. Freeze: editors stop. Woo sites go into maintenance mode through WordPress's own UI. Brochure sites with forms get a "submissions paused for 15 minutes" notice, or the form disabled, set by a human.
3. `wordpress-nix legacy survey <host> --path P --compare survey.json`. **Exit 1 stops the cutover:** re-convert (a PR) and reschedule.
4. `legacy pull --db-only`, then `db audit --strip`, then `media offload` (incremental). Note the pull timestamp `TS`.
5. `wordpress-nix site import <slug> --dump import.sql --files private.tar.gz --home https://<domain> --replace`. This includes resetting privileged passwords with email.
6. `wordpress-nix verify <slug> --against <host> --via-socket`.
7. `wordpress-nix cutover <slug>`: snapshot, disable legacy cache rules and APO, flip DNS, purge. **Pull-to-flip is about 10-20 minutes.**
8. Public `verify`, then `verify --delta-since TS`. Re-enter any late legacy entries listed (forms, comments, donations) by hand.
9. Smoke:
   - homepage;
   - administrator login with two-factor enrolment;
   - a form submission (email delivered; upload on JuiceFS; file opens from the entry);
   - a media upload larger than 100 MB lands in R2 (direct upload and CORS);
   - `wp cron event list`;
   - Woo: a test order, refund and download (`force`), and webhooks reaching the new origin;
   - re-enter `salt-dependent` credentials.

### 13.3 Rollback
- **Within the decision window** (2 hours for Woo; end of day for brochure):
  1. `wordpress-nix cutover <slug> --rollback` restores DNS, rules and APO, then purges.
  2. Run `verify --delta-since <flip-ts>` **against v3** to list entries made on v3. Re-enter them on legacy by hand.
  3. Unfreeze legacy.
- **After the window:** fix forward through §6.3 rollback steps, or `cluster-restore <slug> --db-only`.
- **Afterwards:**
  - keep legacy read-only for 14 days, then cancel at 30;
  - remove the `staging.<domain>` CNAME and `stagingDomain`;
  - disconnect MainWP dashboards;
  - rotate any legacy credential ever in git;
  - flag API keys found in `wp_options` (Stripe and similar) for rotation with the client.

---

## 14. Risks register

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| Cluster not live (no apps; Worker undeployed) | High / blocks M5b and M6 | Explicit G1 gate. M1-M5a proceed without it |
| Cross-account Cloudflare (about 22 accounts) breaks tunnels, DNS or WAF | High / blocker | D7: tunnel in the zone's account via `edge provision`; `tunnel.accountId`; single DNS owner, enforced by an assertion; survey checks token reach |
| Zone not on Cloudflare | Medium / site deferred | Survey column. `edge provision` and `cutover` refuse; onboard the zone first |
| Missing Composer autoloader or `web/wp` | Low after M1 / every request fatal | Install hook plus explicit `dump-autoload`, fail-closed assertions, M1 exit criteria |
| Vendor FOD hash couples to the Composer/nixpkgs version | Medium / mass mismatch on cache miss | Strip version-specific files (M1-verified); FOD shared by out-path through Attic; runtime reference keeps it retained; M1 two-nixpkgs test |
| FOD evicted from Attic; wp.org zip mutated or deleted | Low / deploy failure | Runtime reference (FOD in node closures, pushed daily); weekly `--rebuild` → security issue; M5b disabled-substituter test |
| Version skew: production builds sites with the cluster's wordpress-nix | Medium / surprise breakage | Library-first release order; within-major API compatibility; nightly `cluster-compat`; merge-gate builds every site and smokes one |
| One broken site input blocks fleet deploys | Medium / fleet deploy stall | `flake-update-split` per-input PRs + issue; `.github/flake-update-hold`; M3 test |
| Lost flake-update bumps in bursts | Medium / stale deploys | Dispatch updates all non-held branch-following inputs |
| Dependabot secret split stalls auto-merge or skips the Attic upload | High if unaddressed | Dual Actions and Dependabot secrets; App-made merges; M5a github-actions-lane and main-push checks |
| `verify-relock` too strict (PHP libraries) or too loose | Medium | frappe's URL-matches-package rule; lockcheck fixture from a real lock |
| Private files exposed in the public bucket | Medium / data leak | `S3_EXCLUDE_PREFIXES` in the plugin and in offload; private 403s; M2 and M6 tests |
| Executable planted in uploads | Medium / RCE | Uploads served only by `file_server`; executables never captured; upload prefilter; VM test |
| Database-stored code (WPCodeBox and similar) | High on most sites / RCE via admin | `db-code` flag; code committed; `wpcodebox2` deactivated in production; `db audit` scans; two-factor |
| Credentials surviving the move (application passwords, hashes, MainWP keys) | Medium / takeover | `--strip`; privileged password reset; application passwords off; MainWP dropped and disconnected |
| Malicious triggers missed | Low now | Full dump with `--triggers --routines --events`; survey `information_schema` cross-check |
| v2-pilot needs remediation before wave 2 | Medium | M0 remediation and key swap; wave 2 runs in parallel with wave 1 |
| Code drift between T−7 and cutover | Medium / older code on a newer DB | Legacy auto-updates disabled at T−7; `survey --compare` gate |
| Late public writes lost during the window | Medium / data loss | Pre-staged ingress and `WP_HOME`; pull-to-flip in minutes; form notice; `--delta-since` reconciliation both ways |
| Stale edge HTML after the flip or the rollback | Medium | Snapshot, disable and purge in `cutover`/`--rollback` |
| Broken legacy upload URLs | Medium / SEO and email links | `/wp-content/uploads/*` 301 rules; host-scoped rewrites |
| OOM kills mysqld | Medium | Sized `memoryMB`, lower `memory_limit`, `OOMScoreAdjust`, M6 load test |
| Fleet RAM reservations exceed 2-node failover capacity | Medium | M3 measurement; Nomad oversubscription or a fourth node |
| A compromised admin deletes media | Low | RO-key `rclone copy` to a versioned, retention-locked OVH bucket (R2 has no versioning) |
| Salt regeneration breaks encrypted plugin data | Medium | `salt-dependent` flag, `--keep-salts`, re-entry in smoke |
| Registry tampering or WP Packages outage | Low | `only` guard; verify-relock; independent wp.org checksums; self-host fallback |
| Premium plugins go stale or vulnerable | Medium | Weekly staleness issue, Wordfence, devenv flow, `premium-diff` |
| Unlisted runtime writer | Medium | `writes-to-disk` flag, smoke probes, staging soak journal scan |
| Dependabot quirks (#14379, #14231) | Medium | `config.platform.php`; delisted plugins fail CI, then are committed or replaced |
| Single YubiKey for agenix | Low / lockout | Second key in G1 |
| Two-factor friction | Medium | Email provider; enrolment at cutover |

---

## 15. Review notes

**Accepted in full** (with where each now lives):
- **build-ci**:
  - autoloader → install hook plus `dump-autoload` and assertions (§4.3, M1);
  - networked checks moved out of the sandbox (§6.1, §6.2);
  - `composer validate` flags (§6.1);
  - verify-relock rule ported from frappe (§4.4);
  - FOD retention through the runtime reference (§4.3, M5b);
  - Dependabot/Actions secret split (§4.4, M5a);
  - converter staging before lock (§8.5 step 7);
  - head-of-line blocking (§4.7, M3);
  - `web/wp` copy is mandatory (§4.3);
  - site-deps is a redesign, not a copy (§4.4);
  - dead `nixpkgs.follows` dropped and the check redefined over `flake.lock` (§4.7).
- **runtime-security**:
  - plugin-side exclusions (§10.4 item 4);
  - URL rewrite order and escaping (§4.9);
  - one home per path for Cwicly and fonts (§4.8);
  - `WP_TEMP_DIR=/tmp` (§4.8, §7.2);
  - `stateDir` writable (§7.2);
  - Caddy uploads routing (§7.3);
  - container commands as `wordpress` via `site import` (§8.6);
  - surviving credentials (§4.11, §8.4);
  - WPCodeBox (§4.8, D12);
  - full dumps (§8.3);
  - memory budget (§7.2);
  - R2 versioning claim removed (§4.13);
  - CORS (§4.13);
  - Fluent Forms serving (§4.8, M6);
  - Woo `force` (§8.6);
  - cron description corrected (§4.10);
  - mirror placement via `backup.hostPostCommand` (§4.13);
  - M1/M2 dependency resolved with a `path` repository (§11).
- **conversion-rollout**:
  - cross-account tunnels and DNS ownership (D7, §9.2);
  - v2-pilot key order and adopt mode (M0, §8.6);
  - staging before lock (dup of build-ci);
  - `--survey`/`--dump` required (§8.5);
  - T−7 drift gate (§13.1, §13.2);
  - pre-staged ingress and `WP_HOME` (§13.2);
  - scoped rewrite, `/wp-content/uploads` 301 and edge purge (§4.9, §7.3, §8.6);
  - milestone reordering into M2/M3/G1/M5a/M5b (§11);
  - cluster-centric rollout (§4.7);
  - WPCodeBox on the pilot (§12, M6);
  - MainWP and core-rollback dropped (§4.8);
  - existing repos (D6, `repo adopt`);
  - import tooling and empty-DB guard (§8.6, §4.10);
  - survey `information_schema` plus early v2-pilot remediation (§8.2, M0, §12);
  - exit codes (§8.5);
  - theme diff, jwt-auth drop and survey signature (§8.5, §8.1).

**Partly accepted:**
- **`--classmap-authoritative`** (build-ci autoloader fix). We use `--optimize` instead. It is Bedrock and Trellis's documented production flag, and authoritative mode breaks plugins that load classes outside the classmap.
- **Deleting `installed.php`/`InstalledVersions.php` from the FOD** (build-ci, skew item 3). `dump-autoload` does **not** regenerate them (verified in Composer 2.11 `AutoloadGenerator`), so the critic's mechanism would break `InstalledVersions`, which wp-cloud-files 0.6 uses. We delete them only because the nixpkgs install hook's offline `composer install` is expected to rewrite them, and we make that an M1 verification with a recorded fallback.
- **`ci / cluster-compat` as a required check.** It is nightly and advisory. Making it required on every site PR would need the private cluster lock in every site run. The real gate is the cluster merge-gate, which builds every site, and the release ordering guarantees the site lock is never ahead of the cluster.
- **Per-account OpenTofu providers** (conversion blocker, first alternative). Rejected in favour of the critic's second alternative, a tunnel created in the site account by the tool. It needs only `tunnel.accountId` in the library and reuses v2 code that already works across 22 accounts (D7).
- **Blanket 404 for PHP under `/app/plugins/*` and `/app/themes/*`** (Caddy item). Not adopted. Some plugins legitimately expose direct PHP endpoints. Uploads never reach PHP, and `wp-includes` PHP is blocked except `wp-tinymce.php`. Direct plugin endpoints are reported by the classifier instead.
- **"Run each replace twice, plain and JSON-escaped".** Adopted, but as `--regex` patterns built by one tested function (§4.9) rather than duplicated command lines.
- **Host-side mirror via `ConditionPathExists` or a Nomad periodic job.** We use backup-core's existing "runs where the app runs" rule through a generic `backup.hostPostCommand`, which is less new machinery.

**Rejected:** none outright. Where the critics offered two fixes, the more conventional one was chosen (noted above).

**Residual items for verification, not decisions:**
- the install-hook rewrite of `installed.php`;
- the two-factor filter names;
- Fluent Forms serving per version;
- whether Dependabot needs `registries:` for egress;
- OVH IAM prefix-scoped write keys;
- R2 CORS limits;
- whether the WPCodeBox functionality plugin runs with `wpcodebox2` inactive (fallback: keep it active and remove its editing capability in production).

Each has an owning milestone above.

Key source paths:
- `wordpress-nix/modules/nixos.nix`
- `wordpress-nix/lib/php.nix`
- `nixos-cluster-orchestration/lib/cloud.nix`
- `nixos-cluster-orchestration/modules/app-container.nix`
- `nixos-cluster-orchestration/modules/backup-core.nix`
- `nixos-cluster-orchestration/templates/cluster/.github/workflows/flake-update.yml`
- `<cluster-repo>/flake.nix`
- `frappe-nix/.github/workflows/app-deps.yml`
- `wp-cloud-files: src/{Plugin,MediaHandler,UrlRewriter,CLI}.php`
- `nixos-hosting-cloud/scripts/moonshot/{provision,migrate,lib}.sh`
