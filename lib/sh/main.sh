# wordpress-init — usage, argument parsing and the bootstrap sequence.
#
# Concatenated last by lib/init.nix: everything above is definitions, and this
# file is what actually runs.

usage() {
  cat <<'USAGE'
Usage: wordpress-init [options] [target-dir]

Bootstrap a wordpress-nix site: a thin repo (flake.nix, .envrc, .gitignore,
wp-content/, the site identity and CI callers) that the platform's dev
shell, image and NixOS module build from. Run it in:

  an empty directory        scaffolds a new site
  an existing WordPress     adopts it in place: wp-content becomes the payload,
                            core and wp-config.php are gitignored (the platform
                            pins core and generates its own wp-config.php), and
                            nothing on disk is moved or deleted
  a wordpress-nix site      reconciles it: adds what is missing, refreshes the
                            .gitignore block, never rewrites your files

The target defaults to the current directory.

Options:
  --name <slug>            Site name: ports and the image name derive from it
                           (default: the directory name)
  --database <type>        sqlite | turso | mysql — the dev shell's database
                           (default: sqlite)
  --force                  Scaffold into a non-WordPress directory; or, beside
                           a flake.nix that is not wordpress-nix's, write the
                           wrapper to flake.nix.wordpress-nix
  --dry-run                Print the plan and exit without changing anything
  -y, --yes                Assume yes; no prompts
  --skip-lock              Do not run `nix flake lock`
  --commit[=<msg>]         Commit the result instead of only staging it
  -h, --help               Show this help

Everything is staged, nothing is committed (unless --commit), and no existing
file is overwritten. With a TTY you are prompted (via gum) for the site name.
USAGE
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --name) name="${2:?--name needs a value}"; shift 2 ;;
      --name=*) name="${1#*=}"; shift ;;
      --database) database="${2:?--database needs a value}"; shift 2 ;;
      --database=*) database="${1#*=}"; shift ;;
      --commit) DO_COMMIT=true; shift ;;
      --commit=*) DO_COMMIT=true; COMMIT_MSG="${1#*=}"; shift ;;
      --force) FORCE=true; shift ;;
      --dry-run) DRY_RUN=true; shift ;;
      -y | --yes) ASSUME_YES=true; shift ;;
      --skip-lock) SKIP_LOCK=true; shift ;;
      -h | --help) usage; exit 0 ;;
      -*) usage >&2; die "unknown flag: $1" ;;
      *) target="$1"; shift ;;
    esac
  done
  case "$database" in
    sqlite | turso | mysql) ;;
    *) die "unknown --database '$database' (expected: sqlite, turso or mysql)" 5 ;;
  esac
}

# Runs with the working directory already inside a non-empty target. Sets KIND
# rather than echoing it, so `die` here exits the process instead of a subshell.
decide_kind() {
  if is_wordpress_nix_site; then
    info "already a wordpress-nix site — reconciling"
    KIND=repair
  elif has_foreign_flake; then
    $FORCE ||
      die "'$(pwd -P)' has a flake.nix that is not a wordpress-nix site's. Re-run with --force: the wrapper will be written to flake.nix.wordpress-nix for you to merge." 4
    FOREIGN_FLAKE=true
    KIND=adopt
  elif looks_like_wordpress; then
    KIND=adopt
  else
    $FORCE ||
      die "'$(pwd -P)' is not empty and does not look like WordPress (no wp-content/, wp-includes/ or wp-config.php). Use --force to scaffold a site into it anyway." 6
    KIND=scaffold
  fi
}

resolve_name() {
  local default
  if [ -z "$name" ] && [ "$KIND" = repair ]; then
    name="$(existing_site_name)"
    if [ "$name" = changeme-site-slug ]; then
      name=""
    fi
  fi
  if [ -z "$name" ]; then
    default="$(normalize_name "$(basename "$target_abs")")"
    if has_tty && ! $ASSUME_YES && ! $DRY_RUN; then
      name="$(gum input --header "Site name (lowercase, digits, dashes):" --value "$default")"
    fi
    name="${name:-$default}"
  fi
  name="$(normalize_name "$name")"
}

print_plan() {
  local f
  step "Plan"
  case "$KIND" in
    scaffold) info "scaffold a new site in $target_abs" ;;
    adopt) info "adopt the WordPress install in $target_abs" ;;
    repair) info "reconcile the wordpress-nix site in $target_abs" ;;
  esac
  info "site name      : $name"
  info "dev database   : $database"
  if [ -n "$table_prefix" ]; then
    info "table prefix   : $table_prefix (kept, in the flake's siteConfig)"
  fi
  if [ -d "$DEST" ]; then
    while IFS= read -r f; do
      info "database file  : $f"
    done < <(cd "$DEST" && find_database_files)
  fi
}

# What is left for the user to fill in. Only the files the template owns are
# searched: wp-content is not ours to grep.
report_placeholders() {
  local f
  local -a hits=()
  for f in flake.nix .github/workflows/publish.yml; do
    if [ -f "$f" ] && grep -qE 'CHANGE_?ME' "$f"; then
      hits+=("$f")
    fi
  done
  if [ "${#hits[@]}" -gt 0 ]; then
    printf '\nStill to fill in (search for CHANGEME / CHANGE_ME): %s\n' "${hits[*]}"
    printf 'Local development works without them; they matter once you deploy.\n'
  fi
  return 0
}

print_report() {
  local w db
  step "Done — $(pwd -P)"
  info "site name      : $name"
  info "dev database   : $database"

  if [ "${#WARNINGS[@]}" -gt 0 ]; then
    printf '\n\033[33m%s warning(s):\033[0m\n' "${#WARNINGS[@]}"
    for w in "${WARNINGS[@]}"; do
      printf '  ⚠  %s\n' "$w"
    done
  fi

  db="$(find_database_files | head -n1)"
  cat <<'NEXT'

Next steps:
  git diff --cached --stat     # review; nothing has been committed
  direnv allow                 # or: nix develop --no-pure-eval
  devenv up                    # FrankenPHP (+ Mailpit) — the URL is printed on entry
NEXT
  if [ -n "$db" ]; then
    printf '  %-28s # (another shell) load it into the dev database\n' "wp-import $db"
  else
    printf '  wp-import <dump.sql>         # (another shell) load a MySQL dump or SQLite file, or open the URL to run the installer\n'
  fi
  printf '  wp-admin-user                # a local administrator (prints the password)\n'
  report_placeholders
}

bootstrap() {
  if $DRY_RUN; then
    print_plan
    step "Files"
    render_template
    install_template
    printf '\n--dry-run: nothing was changed.\n'
    return 0
  fi

  step "Bootstrapping '$name' ($KIND) in $target_abs"
  mkdir -p "$DEST"
  cd "$DEST" || die "cannot enter $DEST"
  DEST="."

  if [ -f wp-content/db.php ]; then
    info "wp-content/db.php is an existing drop-in; the platform replaces it with its own (gitignored) one"
  fi

  print_plan
  render_template
  step "Files"
  install_template
  install_gitignore_block
  ensure_repo
  verify_not_ignored

  step "Staging"
  stage_all
  lock_flake

  if $DO_COMMIT; then
    git commit -q -m "$COMMIT_MSG"
    info "committed: $COMMIT_MSG"
  fi
  warn_tracked_but_ignored

  print_report
}

main() {
  # A stray GIT_DIR (from a hook, or from `git submodule foreach`) would send
  # every git command in this script at the wrong repository.
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY

  parse_args "$@"
  : "${target:=.}"
  target_abs="$(realpath -m -- "$target")"

  if [ -d "$target" ] && ! dir_is_empty "$target"; then
    cd "$target" || die "cannot enter $target"
    decide_kind
    DEST="."
    table_prefix="$(detect_table_prefix)"
    if [ "$table_prefix" = wp_ ]; then
      table_prefix=""
    fi
  else
    KIND=scaffold
    DEST="$target_abs"
  fi

  resolve_name
  bootstrap
}

main "$@"
