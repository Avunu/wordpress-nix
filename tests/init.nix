# The bootstrapper (lib/init.nix) against throwaway directories: a fresh
# scaffold, an existing install adopted in place, a re-run, and the two guards.
# No network: the flake lock is skipped, so this asserts on what is written and
# what git is told to track — which is where a bootstrapper goes wrong quietly.
{ pkgs }:

let
  init = import ../lib/init.nix { inherit pkgs; };
in
pkgs.runCommand "wordpress-init-check"
  {
    nativeBuildInputs = [
      init
      pkgs.git
    ];
  }
  ''
    export HOME="$TMPDIR" GIT_CONFIG_GLOBAL=/dev/null
    fail() { echo "FAIL: $*" >&2; exit 1; }
    tracked() { git -C "$1" ls-files --error-unmatch -- "$2" >/dev/null 2>&1; }

    # ── fresh scaffold ──────────────────────────────────────────────────
    wordpress-init -y --skip-lock --name Fresh_Site --database turso fresh >/dev/null
    grep -q 'siteName = "fresh-site"' fresh/flake.nix || fail "siteName not substituted"
    grep -q 'database.type = "turso"' fresh/flake.nix || fail "--database ignored"
    grep -q 'github:Avunu/wordpress-nix' fresh/flake.nix || fail "wrong platform input"
    ! grep -rq 'changeme-site-slug\|CHANGEME-site-slug' fresh --exclude-dir=.git || fail "slug placeholder left behind"
    for f in flake.nix .envrc .gitignore wp-content/plugins/.gitkeep; do
      tracked fresh "$f" || fail "$f is not staged"
    done

    # ── adopting an existing install ────────────────────────────────────
    mkdir -p site/wp-admin site/wp-includes site/wp-content/{plugins/foo,uploads/2024,mu-plugins}
    cat > site/wp-config.php <<'PHP'
    <?php
    define('DB_PASSWORD', 'hunter2');
    $table_prefix = 'avu_';
    PHP
    touch site/wp-includes/version.php site/wp-admin/index.php site/wp-load.php site/index.php \
      site/.htaccess site/.bash_history site/wp-content/db.php \
      site/wp-content/plugins/foo/foo.php site/wp-content/uploads/2024/a.jpg
    echo 'INSERT 1;' > site/export.sql
    echo '*.swp' > site/.gitignore

    wordpress-init -y --skip-lock site > adopt.log
    for f in wp-content/plugins/foo/foo.php flake.nix .envrc; do
      tracked site "$f" || fail "$f should be tracked"
    done
    for f in wp-config.php wp-includes/version.php wp-admin/index.php wp-load.php index.php \
             .htaccess .bash_history wp-content/db.php wp-content/uploads/2024/a.jpg export.sql; do
      ! tracked site "$f" || fail "$f must not be tracked"
    done
    # An existing plugins directory gets no placeholder; empty ones do.
    ! tracked site wp-content/plugins/.gitkeep || fail "placeholder added to a populated directory"
    tracked site wp-content/themes/.gitkeep || fail "placeholder missing from an empty directory"
    grep -q "table_prefix = 'avu_'" site/flake.nix || fail "table prefix not carried over"
    grep -q 'wp-import export.sql' adopt.log || fail "dump not offered to wp-import"
    [ -f site/wp-config.php ] && [ -f site/export.sql ] || fail "adoption moved or removed a file"
    head -n1 site/.gitignore | grep -qx '\*.swp' || fail "the site's own .gitignore lines were lost"

    # ── re-run: reconcile, change nothing ───────────────────────────────
    cp site/.gitignore gitignore.before; cp site/flake.nix flake.before
    git -C site status --porcelain > status.before
    wordpress-init -y --skip-lock site > rerun.log
    grep -q 'reconciling' rerun.log || fail "second run did not reconcile"
    cmp site/.gitignore gitignore.before || fail ".gitignore block not idempotent"
    cmp site/flake.nix flake.before || fail "flake.nix rewritten"
    [ "$(grep -c '>>> wordpress-nix' site/.gitignore)" = 1 ] || fail "managed block duplicated"
    git -C site status --porcelain | cmp - status.before || fail "re-run changed the index"

    # ── guards ──────────────────────────────────────────────────────────
    mkdir -p foreign/wp-content other
    echo '{ outputs = _: {}; }' > foreign/flake.nix
    echo hi > other/a.txt
    ! wordpress-init -y --skip-lock foreign 2>/dev/null || fail "overwrote a foreign flake guard"
    ! wordpress-init -y --skip-lock other 2>/dev/null || fail "scaffolded into a non-WordPress directory"
    wordpress-init -y --skip-lock --force foreign >/dev/null
    grep -q '{ outputs = _: {}; }' foreign/flake.nix || fail "foreign flake.nix was modified"
    [ -f foreign/flake.nix.wordpress-nix ] || fail "wrapper not written beside the foreign flake"

    # ── dry run writes nothing ──────────────────────────────────────────
    wordpress-init -y --dry-run dry >/dev/null
    [ ! -e dry ] || fail "--dry-run created the target"

    echo ok > $out
  ''
