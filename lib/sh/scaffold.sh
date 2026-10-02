# wordpress-init — laying down the site template without ever clobbering.
#
# The template is rendered into a staging directory and only then installed, so
# token substitution can never touch a file the site already owns.

STAGING=""

cleanup_staging() {
  [ -z "$STAGING" ] || rm -rf "$STAGING"
  return 0
}

# templates/site is also what `nix flake init -t` hands out, so it stays a
# valid site on its own: its placeholders are real words (changeme-site-slug,
# CHANGEME) rather than @TOKENS@. The slug is the only one we know the answer
# to; the rest (Cloudflare IDs, URLs, the repo) are the user's, and are
# reported at the end.
render_template() {
  STAGING="$(mktemp -d)"
  trap cleanup_staging EXIT
  cp -R "$TEMPLATE"/. "$STAGING"/
  chmod -R u+w "$STAGING"
  (
    cd "$STAGING"
    sed -i -e "s|changeme-site-slug|$name|g" -e "s|CHANGEME-site-slug|$name|g" \
      flake.nix wrangler.jsonc
    if [ "$database" != sqlite ]; then
      sed -i "s|database.type = \"sqlite\";|database.type = \"$database\";|" flake.nix
    fi
    # The dev wp-config hard-codes 'wp_' ahead of configExtra, and the NixOS
    # module sets its own tablePrefix ahead of it too, so one line in the
    # shared siteConfig is right for both.
    if [ -n "$table_prefix" ]; then
      sed -i "/define('WP_MEMORY_LIMIT'/a\\          \$table_prefix = '$table_prefix';" flake.nix
    fi
  )
}

# Install everything except .gitignore, which is spliced as a managed block
# (install_gitignore_block) so a later run can upgrade it in place. Existing
# files are never replaced.
install_template() {
  local rel dest dir
  while IFS= read -r -d '' rel; do
    rel="${rel#./}"
    case "$rel" in
      .gitignore) continue ;;
      */.gitkeep)
        # Placeholders for empty directories only: a site's own plugins
        # directory does not need a stray file in it.
        dir="$DEST/$(dirname "$rel")"
        if [ -d "$dir" ] && ! dir_is_empty "$dir"; then
          continue
        fi
        ;;
    esac
    dest="$rel"
    if [ "$rel" = flake.nix ] && $FOREIGN_FLAKE; then
      dest=flake.nix.wordpress-nix
    fi
    if [ -e "$DEST/$dest" ]; then
      info ". $dest exists — kept"
      continue
    fi
    if $DRY_RUN; then
      info "+ $dest (dry run)"
      continue
    fi
    mkdir -p "$(dirname "$DEST/$dest")"
    cp "$STAGING/$rel" "$DEST/$dest"
    info "+ $dest"
  done < <(cd "$STAGING" && find . -type f -print0 | sort -z)
}

# ── .gitignore ────────────────────────────────────────────────────────────

GITIGNORE_BEGIN='# >>> wordpress-nix >>> (managed block — edits here are overwritten)'
GITIGNORE_END='# <<< wordpress-nix <<<'

install_gitignore_block() {
  local tmp
  tmp="$(mktemp)"
  if [ -f .gitignore ] && grep -qxF "$GITIGNORE_BEGIN" .gitignore; then
    grep -qxF "$GITIGNORE_END" .gitignore ||
      die ".gitignore has the wordpress-nix block's opening line but not its closing one ($GITIGNORE_END) — fix it and re-run"
    awk -v b="$GITIGNORE_BEGIN" -v e="$GITIGNORE_END" -v f="$STAGING/.gitignore" '
      $0 == b { print b; while ((getline line < f) > 0) print line; close(f); skip = 1; next }
      $0 == e { print e; skip = 0; next }
      !skip   { print }
    ' .gitignore > "$tmp"
  else
    {
      if [ -f .gitignore ]; then
        cat .gitignore
        [ -z "$(tail -c1 .gitignore)" ] || echo
      fi
      printf '%s\n' "$GITIGNORE_BEGIN"
      cat "$STAGING/.gitignore"
      printf '%s\n' "$GITIGNORE_END"
    } > "$tmp"
  fi
  if [ -f .gitignore ] && cmp -s "$tmp" .gitignore; then
    rm -f "$tmp"
    info ". .gitignore already current"
  else
    mv "$tmp" .gitignore
    chmod 0644 .gitignore
    info "+ .gitignore (wordpress-nix managed block)"
  fi
}

# ── git ───────────────────────────────────────────────────────────────────

# A flake's source tree is exactly the set of git-tracked files, so anything
# the build needs that .gitignore excludes is a silent, much-later failure.
# Turn it into an error here instead.
verify_not_ignored() {
  local bad=0 p
  for p in flake.nix .envrc wp-content wrangler.jsonc; do
    [ -e "$p" ] || continue
    if git check-ignore -q -- "$p"; then
      printf '  \033[31m✗\033[0m  %s is excluded by .gitignore\n' "$p" >&2
      bad=1
    fi
  done
  [ "$bad" = 0 ] ||
    die "an earlier .gitignore rule excludes files the Nix build needs (a flake's source tree is only its git-tracked files)"
}

# Initialise a repository unless the directory is already inside one (a site
# kept in a monorepo is that repo's business).
ensure_repo() {
  local top
  top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  if [ "$top" = "$(pwd -P)" ]; then
    return 0
  elif [ -n "$top" ]; then
    info "inside the git repository at $top — using it"
  else
    git init -q -b main
    info "+ git repository (main)"
  fi
}

# Files the index already holds that the .gitignore block now excludes: from an
# earlier `git add`, or a repo that committed its whole install. Ignore rules do
# not untrack, and wp-config.php among them holds the database credentials. Left
# alone — dropping what a repository tracks is its owner's call — but named.
warn_tracked_but_ignored() {
  local -a files=()
  mapfile -t files < <(git ls-files -ci --exclude-standard -- . | head -n 20)
  if [ "${#files[@]}" -gt 0 ]; then
    warn "git already tracks files the platform ignores (${files[*]}); untrack them with 'git rm -r --cached -- <path>'. wp-config.php in particular holds credentials."
  fi
}

stage_all() {
  local n
  n="$(git add -A -n -- . | wc -l)"
  if [ "$n" -gt 20000 ]; then
    warn "$n files would be staged — that usually means .gitignore is wrong"
    $ASSUME_YES || confirm "Stage $n files anyway?" ||
      die "aborted; nothing was staged (the working tree changes above are already applied)"
  fi
  git add -A -- .
  git ls-files --error-unmatch -- flake.nix >/dev/null ||
    die "flake.nix did not reach the git index"
}

lock_flake() {
  local out
  if $SKIP_LOCK; then
    info "skipping the flake lock (--skip-lock)"
  elif $FOREIGN_FLAKE; then
    info "not locking: the wrapper is flake.nix.wordpress-nix, to be merged by hand"
  elif ! command -v nix >/dev/null 2>&1; then
    warn "nix is not on PATH; run 'nix flake lock' yourself"
  elif [ -f flake.lock ] && ! $FORCE && [ "$KIND" = repair ]; then
    info ". flake.lock exists — kept (nix flake update wordpress-nix to move the pin)"
  else
    step "Pinning the platform (nix flake lock)"
    # Quiet on success: a first lock prints one line per transitive input.
    if out="$(nix flake lock 2>&1)"; then
      git add -- flake.lock
      info "+ flake.lock"
    else
      printf '%s\n' "$out" >&2
      warn "nix flake lock failed (output above). Everything else is done; fix the cause and run 'nix flake lock'."
    fi
  fi
}
