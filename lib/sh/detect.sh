# wordpress-init — read-only probes: what is this directory, and what does the
# WordPress install in it already say about itself.
#
# Nothing here writes, so `--dry-run` is exact rather than a best guess.

# A bare .git does not make a directory non-empty: `git init` followed by
# nothing is still somewhere we can scaffold.
dir_is_empty() {
  local entry
  [ -e "$1" ] || return 0
  [ -d "$1" ] || return 1
  for entry in "$1"/* "$1"/.[!.]* "$1"/..?*; do
    if [ -e "$entry" ] || [ -L "$entry" ]; then
      if [ "$(basename "$entry")" != ".git" ]; then
        return 1
      fi
    fi
  done
  return 0
}

# Any one of these is enough: a full install, a bare wp-content checkout (a
# site repo cloned without core), or a wp-config.php sitting alone.
looks_like_wordpress() {
  [ -d wp-content ] || [ -f wp-includes/version.php ] ||
    [ -f wp-settings.php ] || [ -f wp-config.php ]
}

is_wordpress_nix_site() { [ -f flake.nix ] && grep -q 'wordpress-nix' flake.nix; }

has_foreign_flake() { [ -f flake.nix ] && ! is_wordpress_nix_site; }

# `siteName = "…";` as an existing wordpress-nix flake spells it.
existing_site_name() {
  sed -nE 's/^[[:space:]]*siteName[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/p' flake.nix | head -n1
}

# The literal $table_prefix of an existing wp-config.php. A prefix computed
# from the environment (`getenv(...) ?: 'wp_'`) does not match, and yields
# nothing: it is not ours to guess.
detect_table_prefix() {
  local line prefix
  [ -f wp-config.php ] || return 0
  line="$(grep -E "^[[:space:]]*\\\$table_prefix[[:space:]]*=[[:space:]]*['\"]" wp-config.php | head -n1 || true)"
  [ -n "$line" ] || return 0
  prefix="$(printf '%s' "$line" | awk -F"['\"]" '{ print $2 }')"
  case "$prefix" in
    '' | *[!A-Za-z0-9_]*) return 0 ;;
  esac
  printf '%s' "$prefix"
}

# Database files wp-import can load, newest first: dumps in the root, and the
# file an SQLite Database Integration site keeps under wp-content.
find_database_files() {
  find . -maxdepth 1 -type f \( -name '*.sql' -o -name '*.sql.gz' -o -name '*.sqlite' \) \
    -printf '%T@ %f\n' | sort -rn | cut -d' ' -f2-
  [ ! -f wp-content/database/.ht.sqlite ] || echo wp-content/database/.ht.sqlite
}
