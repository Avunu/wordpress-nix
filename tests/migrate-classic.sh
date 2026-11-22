#!/usr/bin/env bash
# End-to-end check for `wordpress-init --migrate` over a synthetic classic install.
#
# Usage: migrate-classic.sh <path-to-wordpress-init> <path-to-make-classic-wp.sh>
#
# Offline: the catalog is a file (WORDPRESS_NIX_CATALOG), the conversion runs with
# --skip-lock, and the lock step runs against stand-in composer and nix.
set -euo pipefail

wordpress_init="$1"
MAKE_WP="$2"

ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
export HOME="$ROOT/home"
mkdir -p "$HOME"
export GIT_CONFIG_GLOBAL="$ROOT/gitconfig"
export GIT_CONFIG_SYSTEM=/dev/null
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@example.com
export GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@example.com
git config --global init.defaultBranch main

fails=0
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
no() {
  printf '  \033[31m✗\033[0m %s\n' "$1"
  fails=$((fails + 1))
}
check() { # <description> <command...>
  local desc=$1
  shift
  if "$@" > /dev/null 2>&1; then ok "$desc"; else no "$desc"; fi
}
check_eq() { # <description> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else no "$1 (expected '$2', got '$3')"; fi
}
exits() { # <expected> <dir> <args...>: run wordpress-init in <dir>, compare the exit code
  local want=$1 dir=$2 rc=0
  shift 2
  (cd "$dir" && "$wordpress_init" "$@") > "$ROOT/last.log" 2>&1 || rc=$?
  check_eq "exit $want: $(basename "$dir") $*" "$want" "$rc"
}
FAKE=sha256-0000000000000000000000000000000000000000000=
FAKE_HASH=sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=

bash "$MAKE_WP" "$ROOT/fx" > /dev/null
export WORDPRESS_NIX_CATALOG="$ROOT/fx/catalog.json"
DECIDE=(--accept-modified modified-plugin:replace --accept-modified fixture-parent:vendor --drop-plugin ghost-plugin)
for copy in dry blocked locked lockfail resume; do cp -a "$ROOT/fx/site" "$ROOT/$copy"; done

echo "── help ─────────────────────────────────────────────────────"
"$wordpress_init" --help > "$ROOT/help.txt"
for flag in --version --dry-run --skip-lock -y --yes --commit --force --name --migrate --init --sync --check \
  --strict --home-url --php --survey --dump --keep-plugin --drop-plugin --keep-inactive --keep-salts \
  --accept-modified --no-loosen; do
  check "--help lists $flag" grep -q -- "$flag" "$ROOT/help.txt"
done

echo "── dry run and the blocked plan ─────────────────────────────"
before="$(cd "$ROOT/dry" && git status --porcelain && find . -path ./.git -prune -o -type f -print | sort | md5sum)"
exits 0 "$ROOT/dry" --migrate --dry-run --name fixture
check "the plan shows the jwt-auth fork dropped" grep -q 'plugin/jwt-auth .*DROP' "$ROOT/last.log"
check "the plan flags modified-plugin" grep -q 'modified-plugin.*\[modified\]' "$ROOT/last.log"
check "the plan flags ghost-plugin missing-code" grep -q 'ghost-plugin.*missing-code' "$ROOT/last.log"
check "the plan drops mainwp-child (active, absent)" grep -q 'plugin/mainwp-child .*DROP' "$ROOT/last.log"
check "the plan reports wpcodebox2 db-code, decided" grep -q 'wpcodebox2.*db-code' "$ROOT/last.log"
check "the plan reports the tracked wp-config.php" grep -q 'wp-config.php is tracked by git' "$ROOT/last.log"
check "the plan reports the planted uploads PHP" grep -q 'uploads holds 1 PHP file(s) (x.php)' "$ROOT/last.log"
after="$(cd "$ROOT/dry" && git status --porcelain && find . -path ./.git -prune -o -type f -print | sort | md5sum)"
check_eq "--dry-run changed nothing" "$before" "$after"
exits 1 "$ROOT/blocked" --migrate -y --skip-lock --name fixture
check "a blocked plan names its decisions" grep -q 'accept-modified modified-plugin:replace|vendor' "$ROOT/last.log"
check "a blocked plan changed nothing" test -f "$ROOT/blocked/wp-config.php"
exits 1 "$ROOT/blocked" --migrate --dry-run --strict --name fixture "${DECIDE[@]}"

echo "── migrating ────────────────────────────────────────────────"
cd "$ROOT/fx/site"
if ! "$wordpress_init" --migrate -y --skip-lock --name fixture "${DECIDE[@]}" > "$ROOT/run1.log" 2>&1; then
  cat "$ROOT/run1.log"
  echo "migration failed"
  exit 1
fi

echo "── dispositions ─────────────────────────────────────────────"
req() { jq -r --arg n "$1" '.require[$n] // "absent"' composer.json; }
check_eq "roots/wordpress pinned to the installed core" 6.8.3 "$(req roots/wordpress)"
check_eq "pristine plugin → wp-plugin/ at its exact version" 1.0.0 "$(req wp-plugin/pristine-plugin)"
check_eq "modified plugin, replace → wp-plugin/" 2.0.0 "$(req wp-plugin/modified-plugin)"
check_eq "two-factor added" "^0.10" "$(req wp-plugin/two-factor)"
check_eq "inactive plugin not required" absent "$(req wp-plugin/inactive-plugin)"
check_eq "the template's theme dropped" absent "$(req wp-theme/twentytwentyfive)"
check_eq "config.platform.php" 8.4 "$(jq -r .config.platform.php composer.json)"
check_eq "the only guard" '["wp-plugin/*","wp-theme/*"]' "$(jq -c '.repositories[] | select(.name == "wp-packages") | .only' composer.json)"
check_eq "the name" avunu/site-fixture "$(jq -r .name composer.json)"
for d in plugins/premium-plugin plugins/unregistered-plugin plugins/wpcodebox2 plugins/wpcodebox_functionality_plugin \
  themes/fixture-child themes/fixture-parent mu-plugins/custom-mu.php; do
  check "committed: web/app/$d" test -e "web/app/$d"
done
check "the modified parent theme kept its change (vendor)" test -f web/app/themes/fixture-parent/hack.php
for d in plugins/jwt-auth plugins/pristine-plugin plugins/inactive-plugin themes/spare-theme; do
  check "not committed: web/app/$d" test ! -e "web/app/$d"
done
check_eq "developmentOnlyPlugins" '[ "wpcodebox2" ]' "$(sed -n 's/.*developmentOnlyPlugins = \(.*\);/\1/p' flake.nix)"
check "flake.nix names the site" grep -q 'name = "fixture";' flake.nix
check "flake.nix sets the active theme" grep -q 'activeTheme = "fixture-child";' flake.nix
check_eq "dbPrefix" wpx_ "$(jq -r .dbPrefix .github/wordpress-nix.json)"
check_eq "dropPlugins" '["ghost-plugin","jwt-auth","mainwp-child"]' "$(jq -c .dropPlugins .github/wordpress-nix.json)"
check_eq "home" https://fixture.example "$(jq -r .home .github/wordpress-nix.json)"
check ".env.example carries DB_PREFIX" grep -q "^DB_PREFIX='wpx_'" .env.example
check_eq "nix/vendor-hash seeded with lib.fakeHash (--skip-lock)" "$FAKE_HASH" "$(cat nix/vendor-hash)"

echo "── .gitignore ───────────────────────────────────────────────"
check "the managed block whitelists premium-plugin" grep -qx '!web/app/plugins/premium-plugin/' .gitignore
check "the managed block ignores the backup" grep -qx '/.wordpress-nix-backup/' .gitignore
check "Bedrock's own rules are there" grep -qx 'web/wp' .gitignore
check "the backup is ignored" git check-ignore -q .wordpress-nix-backup/plan.json

echo "── the backup is complete ───────────────────────────────────"
B=.wordpress-nix-backup/legacy
for f in wp-config.php backup.sql robots.txt wp-includes/version.php wp-admin/index.php \
  wp-content/object-cache.php wp-content/advanced-cache.php wp-content/plugins/jwt-auth/jwt-auth.php \
  wp-content/plugins/inactive-plugin wp-content/plugins/cart-cache.bak wp-content/themes/spare-theme \
  wp-content/mu-plugins/xcloud-auto-login.php wp-content/mu-plugins/platform-environment.php \
  wp-content/uploads/x.php wp-content/uploads/cwicly/css/cc-post-1.css wp-content/uploads/wpcodebox/index.php \
  wp-content-rest/plugins/pristine-plugin/pristine-plugin.php; do
  check "backup holds $f" test -e "$B/$f"
done
check "no wp-content left" test ! -e wp-content
check "no legacy core left" test ! -e wp-includes
check "plan.json recorded" test -f .wordpress-nix-backup/plan.json
check "the survey stayed" test -f .wordpress-nix-backup/survey.json

echo "── the index ────────────────────────────────────────────────"
for f in flake.nix composer.json nix/vendor-hash config/application.php web/wp-config.php .github/wordpress-nix.json \
  .gitignore web/app/plugins/premium-plugin/premium-plugin.php web/app/themes/fixture-parent/hack.php; do
  check "indexed: $f" git ls-files --error-unmatch -- "$f"
done
check "wp-config.php's deletion is staged" sh -c '! git ls-files --error-unmatch wp-config.php'
check "nothing committed (no --commit)" test "$(git rev-list --count HEAD)" = 1
check "no untracked file outside the backup" test -z "$(git status --porcelain --untracked-files=all | grep '^??')"

echo "── a second run is a no-op ──────────────────────────────────"
tree1="$(git write-tree)"
status1="$(git status --porcelain)"
"$wordpress_init" -y --skip-lock > "$ROOT/run2.log" 2>&1 || { cat "$ROOT/run2.log"; no "the second run failed"; }
check "the second run reconciled" grep -q 'reconciling' "$ROOT/run2.log"
check_eq "the index is unchanged" "$tree1" "$(git write-tree)"
check_eq "the tree is unchanged" "$status1" "$(git status --porcelain)"

echo "── an interrupted conversion resumes ────────────────────────"
cd "$ROOT/resume"
"$wordpress_init" --migrate -y --skip-lock --name fixture "${DECIDE[@]}" > "$ROOT/resume1.log" 2>&1 \
  || { cat "$ROOT/resume1.log"; no "the conversion to resume failed"; }
# Back to where an interrupted run stops after the template lands: wp-content still
# holds a committed plugin, and a legacy file is at the root.
mkdir -p wp-content/plugins
mv web/app/plugins/unregistered-plugin wp-content/plugins/
git rm -rq --cached web/app/plugins/unregistered-plugin
cp .wordpress-nix-backup/legacy/wp-config.php wp-config.php
exits 0 "$ROOT/resume" -y --skip-lock
check "the rerun resumed from plan.json" grep -q 'resuming the conversion' "$ROOT/last.log"
for f in composer.json config/application.php web/wp-config.php nix/vendor-hash wp-cli.yml flake.nix \
  web/app/plugins/premium-plugin/premium-plugin.php; do
  check "resume kept $f" test -e "$f"
done
for d in composer.json config web nix wp-cli.yml flake.nix; do
  check "resume moved no template file: $d" test ! -e ".wordpress-nix-backup/legacy/$d"
done
check "resume moved the remaining plugin" git ls-files --error-unmatch -- web/app/plugins/unregistered-plugin
check "resume emptied wp-content" test ! -e wp-content
check "resume swept wp-config.php again" test ! -e wp-config.php -a -e .wordpress-nix-backup/legacy/wp-config.php.1
cd "$ROOT/fx/site"

echo "── guard rails ──────────────────────────────────────────────"
exits 6 "$ROOT/fx/multisite" --migrate -y --skip-lock
exits 5 "$ROOT/fx/old" --migrate -y --skip-lock
exits 6 "$ROOT/fx/nosurvey" --migrate -y --skip-lock
exits 8 "$ROOT/fx/nested/parent/site" --migrate -y --skip-lock
exits 4 "$ROOT/fx/foreign" --migrate -y --skip-lock
mkdir -p "$ROOT/nonempty" && touch "$ROOT/nonempty/x"
exits 2 "$ROOT/nonempty" --init
mkdir -p "$ROOT/notwp" && touch "$ROOT/notwp/x"
exits 6 "$ROOT/notwp" --migrate
exits 0 "$ROOT/fx/v2" --migrate -y --skip-lock
check "v2: the old flake moved to the backup" test -f "$ROOT/fx/v2/.wordpress-nix-backup/legacy/flake.nix"
check "v2: the new flake is wordpress-nix's" grep -q 'wordpress-nix.flakeModules.default' "$ROOT/fx/v2/flake.nix"
exits 0 "$ROOT/fx/foreign" --migrate -y --skip-lock --force
check "foreign --force: flake.nix.wordpress-nix written" test -f "$ROOT/fx/foreign/flake.nix.wordpress-nix"
check "foreign --force: the foreign flake.nix kept" grep -q 'outputs = _: { };' "$ROOT/fx/foreign/flake.nix"

echo "── the lock step, with stand-in composer and nix ────────────"
mkdir -p "$ROOT/bin"
# The build sandbox has no /usr/bin/env: the stand-ins name this bash.
BASH_BIN="$(command -v bash)"
{ echo "#!$BASH_BIN"; cat <<'EOF'
echo "composer $*" >> "$STANDIN_LOG"
[ -n "${STANDIN_FAIL:-}" ] && { echo "composer: resolution failed" >&2; exit 2; }
git ls-files --error-unmatch flake.nix composer.json nix/vendor-hash .github/wordpress-nix.json > /dev/null \
  || { echo "composer: the rendered files are not indexed" >&2; exit 3; }
printf '{"content-hash":"%s","packages":[]}\n' "$(sha256sum composer.json | cut -c1-32)" > composer.lock
EOF
} > "$ROOT/bin/composer"
{ echo "#!$BASH_BIN"; cat <<EOF
echo "nix \$*" >> "\$STANDIN_LOG"
case "\$1 \$2" in
  "run .#relock")
    git ls-files --error-unmatch composer.lock > /dev/null || { echo "nix: composer.lock is not indexed" >&2; exit 3; }
    echo "$FAKE" > nix/vendor-hash; git add nix/vendor-hash ;;
  "flake lock") echo '{"nodes":{},"root":"root","version":7}' > flake.lock ;;
  *) echo "nix: unexpected \$*" >&2; exit 4 ;;
esac
EOF
} > "$ROOT/bin/nix"
chmod +x "$ROOT/bin/composer" "$ROOT/bin/nix"
export STANDIN_LOG="$ROOT/standin.log"
: > "$STANDIN_LOG"
(cd "$ROOT/locked" && PATH="$ROOT/bin:$PATH" "$wordpress_init" --migrate -y --commit --name fixture "${DECIDE[@]}") \
  > "$ROOT/lock.log" 2>&1 || { cat "$ROOT/lock.log"; no "the conversion with the lock step failed"; }
cd "$ROOT/locked"
check_eq "the stand-ins ran in order" \
  "composer update --no-install --no-scripts --no-interaction|nix run .#relock|nix flake lock|composer update --lock --no-install --no-scripts --no-interaction|nix run .#relock" \
  "$(paste -sd'|' "$STANDIN_LOG")"
check_eq "nix/vendor-hash is the relocked hash, not lib.fakeHash" "$FAKE" "$(cat nix/vendor-hash)"
check_eq "two commits on top of the legacy one" \
  "chore(deps): loosen plugin constraints|feat: convert to Bedrock (wordpress-nix)|legacy site, as the host had it" \
  "$(git log --format=%s | paste -sd'|')"
check_eq "the first commit pins exactly" 1.0.0 "$(git show HEAD~1:composer.json | jq -r '.require["wp-plugin/pristine-plugin"]')"
check_eq "the second loosens" "^1.0" "$(jq -r '.require["wp-plugin/pristine-plugin"]' composer.json)"
check_eq "core stays exact" 6.8.3 "$(jq -r '.require["roots/wordpress"]' composer.json)"
check "composer.lock and flake.lock are committed" git ls-files --error-unmatch composer.lock flake.lock
check "the tree is clean" test -z "$(git status --porcelain)"

: > "$STANDIN_LOG"
rc=0
(cd "$ROOT/lockfail" && PATH="$ROOT/bin:$PATH" STANDIN_FAIL=1 "$wordpress_init" --migrate -y --name fixture "${DECIDE[@]}") \
  > "$ROOT/lockfail.log" 2>&1 || rc=$?
check_eq "a failed lock step exits 1" 1 "$rc"
check "it names the failing command and the rerun" grep -q 'composer update --no-install --no-scripts --no-interaction. failed.*rerun' "$ROOT/lockfail.log"
check "the rendered files were indexed before it" git -C "$ROOT/lockfail" ls-files --error-unmatch flake.nix composer.json
check_eq "nix/vendor-hash is still lib.fakeHash" "$FAKE_HASH" "$(cat "$ROOT/lockfail/nix/vendor-hash")"

echo
if [ "$fails" -gt 0 ]; then
  echo "$fails check(s) failed"
  exit 1
fi
echo "all checks passed"
