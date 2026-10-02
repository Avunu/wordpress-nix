# wordpress-init — shared helpers.
#
# This file is concatenated with the other lib/sh/*.sh sources into a single
# script by lib/init.nix, so shellcheck sees the whole program at build time.
# Definitions only; `main` runs from main.sh, which must be concatenated last.

# Baked at build time by lib/init.nix.
TEMPLATE="@TEMPLATE@"

# ── globals ───────────────────────────────────────────────────────────────
# Declared up front because the script runs under `set -o nounset`.
FORCE=false
DRY_RUN=false
ASSUME_YES=false
SKIP_LOCK=false
DO_COMMIT=false
COMMIT_MSG="chore: bootstrap wordpress-nix"

name=""
database="sqlite"
target=""
target_abs=""

# scaffold | adopt | repair — see decide_kind.
KIND=""
# A flake.nix that is not wordpress-nix's: the wrapper is written beside it.
FOREIGN_FLAKE=false
# Where install_template writes. "." once we are inside the target; the target
# itself for a dry run that has not created it.
DEST="."
# The existing install's $table_prefix, when it is not the dev shell's default.
table_prefix=""

declare -a WARNINGS=()

# ── output ────────────────────────────────────────────────────────────────
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() {
  WARNINGS+=("$*")
  printf '  \033[33m⚠\033[0m  %s\n' "$*" >&2
}
die() {
  printf '\033[31mERROR:\033[0m %s\n' "$1" >&2
  exit "${2:-1}"
}

has_tty() { [ -t 0 ] && [ -t 1 ]; }

# Prompt only when we can; never emit a gum call with stdin closed. `gum
# confirm` exits 1 on "no", which would trip `set -e` outside an if.
confirm() {
  has_tty || return 1
  gum confirm "$1"
}

# ── names ─────────────────────────────────────────────────────────────────
# The site name is a Nix string, a port-hash seed and an image name, and the
# flake option insists on ^[a-z0-9][a-z0-9-]*$.
normalize_name() {
  local n
  n="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed -E 's/-+/-/g; s/^-//; s/-$//')"
  printf '%s' "${n:-wordpress-site}"
}
