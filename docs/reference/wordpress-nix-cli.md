---
title: The wordpress-nix CLI
description: The conversion commands (legacy, db, media, edge, repo adopt, site, verify, cutover, audit) and the standards-core commands of wordpress-nix, with their flags, records and exit codes.
order: 5
tags: [reference, cli, conversion, cloudflare, ci]
updated: 2026-11-21
---

`wordpress-nix` is one umbrella (`nix run github:Avunu/wordpress#wordpress-nix -- …`). It carries standards-core's commands with wordpress-nix's product (`ci`, `repo apply|doctor|audit|rollout|targets`, `sync`, `policy`, `profile`, `config`) and the conversion commands below. Nothing writes to a legacy host; nothing keeps a state file (every step probes Cloudflare, GitHub or the cluster). The only local records are `.wordpress-nix-backup/{survey.json, provenance.json, inventory.json, cutover-<slug>.json}`.

Exit codes are standards-core's: **0** ok; **1** a refusal or a failed gate; **2** a bad argument; **3** a GitHub, git, SSH or API error.

Environment: `WORDPRESS_NIX_CLOUDFLARE_TOKEN` (the multi-account provisioning token, decrypted from cluster-ovh-01), `WORDPRESS_NIX_WORDFENCE_KEY`, `WORDPRESS_NIX_AGE_RECIPIENT`, `WORDPRESS_NIX_CLUSTER` (the tunnel name prefix, default `ovh-01`), `WORDPRESS_NIX_NODE`, and `WORDPRESS_NIX_S3_{KEY,SECRET,ENDPOINT}` for `media offload`.

## legacy

| Command | Does |
|---|---|
| `legacy survey <host> --path P [--json\|--csv] [--compare <survey.json>]` | Versions (WordPress, PHP, MariaDB), multisite, prefix, locale, plugins and themes with versions and the active set, `wp core\|plugin verify-checksums` when wp-cli exists, administrators, uploads (size, directories, PHP files), Woo, DB size, `information_schema` triggers, routines, events and views with their definitions, xCloud, and the zone's Cloudflare status (full setup, account, token reach, rules, APO). `--compare` exits 1 on any change in core, plugin or theme versions or the active set. |
| `legacy survey <host> --root /var/www --fleet [--csv]` | One row per site, with the complexity score. |
| `legacy pull <host> --path P --into DIR [--db-only] [--age-recipient R]` | Code rsync (no uploads, caches, wflogs, drop-ins, logs) and `mysqldump --single-transaction --triggers --routines --events --hex-blob` in one SSH session (a ControlMaster); `dump.sql` (0600), the age-encrypted evidence copy, `survey.json` and `provenance.json` (both timestamps) in `DIR/.wordpress-nix-backup/`. |

## db

| Command | Does |
|---|---|
| `db audit <dump> [--survey F] [--strip --out F] [--accept ID]… [--json]` | `audit-mysql-dump`: fails on, and `--strip` removes, triggers, routines, events, views, `CREATE USER`/`GRANT`, application passwords, session tokens and `mainwp_child_*` options (tuple by tuple, the rest byte for byte). Reports, until `--accept`ed: obfuscated autoloaded options, snippet-table rows, widget and header/footer scripts, SQL-written administrators, administrators the survey does not list, a `siteurl`/`home` mismatch, survey count mismatches. Exit 0 clean or all accepted, 1 open findings, 2 a bad argument. |
| `db rewrite-urls --from-home URL [--to-home URL] [--media-url URL] [--exclude-prefix-from inventory.json] [--dry-run] [--report-changes] [--print]` | The §4.9 sequence through `wp` on PATH: attachments to `media.<domain>` (plain and JSON-escaped, minus the local prefixes), the remaining `/wp-content/` to `/app/`, the host to `--to-home` when it differs, then `wp core update-db` and `wp rewrite flush`. Other hosts are never touched. |

## media

| Command | Does |
|---|---|
| `media provision <slug> --domain D [--adopt BUCKET] [--rotate] [--no-probe]` | In the zone's account: the bucket `wp-<slug>-media` (or the adopted one), `media.<domain>`, CORS for the domain, `www.` and `staging.`, and account-owned bucket-scoped keys (read-write for the site, read-only for the OVH mirror), probed with put/get/delete and a CORS preflight. Prints the `<slug>-cloud-files` and `<slug>-media-ro` fragments once. An existing key is never re-minted; `--rotate` mints, then revokes. |
| `media offload <slug> --from HOST --path P --dump F [--adopt] [--credentials F] [--dry-run]` | The dump's attachment index (files, sizes, originals; minus the excluded prefixes) from the host to R2 (`rclone copy --files-from`, then `rclone check`); everything else into `.wordpress-nix-backup/private.tar.gz`; executables refused (but the WPCodeBox stub); `inventory.json`. `--adopt` (automatic when the legacy config names `S3_BUCKET`) skips what the bucket has and fails on attachments in neither place. |

## edge

| Command | Does |
|---|---|
| `edge provision <slug> --domain D [--cluster C] [--access --access-emails E]` | In the zone's account: the tunnel `<cluster>-<slug>` (found or created; its credentials printed as `<slug>-cf/cloudflared`), the proxied `staging.<domain>` CNAME, the login rate limit and an `xmlrpc.php` block, the static-asset Cache Rule and the anonymous-HTML Cache Rule (disabled until cutover), and path-scoped Access with its bypasses. Refuses a zone not in a full setup. |

## repo adopt

`repo adopt <slug> [--org Avunu] [--check] [--branch main]`: no `site-<slug>` → create it; one whose history holds a dump, `wp-config.php` or `.env` → rename it `site-<slug>-legacy`, archive it, create a fresh one (D6); otherwise push `wordpress-nix/v3` and open a pull request. Every push is verified on the remote. `--check` changes nothing. Then `repo apply`.

## site, verify, cutover

| Command | Does |
|---|---|
| `site import <slug> --dump F [--files T] --home URL [--replace] [--node N]` | On the node running `app-<slug>`: refuses an installed site without `--replace` (which runs `cluster-backup` first); streams the dump into `wp db import -` and the tarball into the uploads volume; confirms no executable object; rewrites URLs to `--home`; deactivates `dropPlugins`; forces Woo's download method; resets privileged passwords (no email on staging); `wp core update-db`, `wp rewrite flush`. |
| `site status [<slug>] [--domain D] [--json]` | HTTP on the apex, `staging.` and `media.`; DNS; the tunnel's health; the cutover record. |
| `verify <slug> --against HOST --path P [--via-socket] [--delta-since TS]` | Row counts per table, new administrators, `wp-cloud-files status` and `verify --sample=200`, `verify-wp-checksums`, the HTTP posture (§7.3); `--via-socket` before the flip. `--delta-since` lists the rows legacy gained after the pull. |
| `cutover <slug> --domain D [--rollback]` | Snapshot (DNS, Page/Cache/Transform rules, APO) to `cutover-<slug>.json`, disable the legacy HTML caching and APO, enable `wordpress-nix-html-cache`, CNAME the apex and `www` to the tunnel, purge. `--rollback` restores the snapshot exactly and purges. |

## audit

`audit [--root .] [--lock F] [--base-lock F] [--feed F] [--format text|json|github]`: composer.lock's `wp-plugin/*`, `wp-theme/*` and core, and committed plugin and theme headers, against Wordfence Intelligence V3. With `--base-lock`, only vulnerabilities the change introduced exit 1.

## The site CI steps (standards-core)

- `ci apply-relock-patch --patch P`: relock-push's check; the only path a relock may push is `nix/vendor-hash`.
- `ci verify-relock --base SHA --head SHA`: every changed `composer.lock` entry's dist URL names its package and version (wordpress.org zips for `wp-plugin/*`, `wp-theme/*` and core, the GitHub zipball at the source reference for Packagist), a new package is `needs-review`, and `nix/vendor-hash` is a real hash.
- `ci verdict` / `ci automerge`: the §6.4 decision table (`wordpress_nix_tools/verdict.py`).
