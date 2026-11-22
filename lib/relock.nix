# `nix run .#relock` in a site repository: rewrites nix/vendor-hash, the
# vendor FOD's hash (lib/mk-bedrock-site.nix), after composer.lock changed.
# The same name as frappe-nix's and odoo-nix's relock; here it is plain Nix:
#
#   1. write lib.fakeHash to the file and build the FOD (`.#vendor`), which
#      fails with the hash it got;
#   2. write that hash;
#   3. build again, to confirm it.
#
# On any failure the file gets its old contents back. In a git work tree the
# file is staged (`git add`), as a flake only sees what git tracks, and as
# frappe-nix's relock stages what it writes.
#
# A flake app, outside every other output's dependency graph: it has to run
# when the package cannot evaluate, which is exactly when it is needed.
#
#   nix run .#relock                 # from the site's root
#   nix run .#relock -- .#vendor     # another installable
{
  pkgs,
  lib ? pkgs.lib,
  # Relative to the site's root, where it runs.
  vendorHashFile ? "nix/vendor-hash",
}:
pkgs.writeShellApplication {
  name = "wordpress-nix-relock";
  # nix is the caller's own, deliberately: it talks to the caller's daemon.
  runtimeInputs = [
    pkgs.gnugrep
    pkgs.coreutils
  ];
  text = ''
    file=${lib.escapeShellArg vendorHashFile}
    installable=''${1:-.#vendor}
    fake=${lib.escapeShellArg lib.fakeHash}

    if [[ ! -f composer.json || ! -f composer.lock ]]; then
      echo "relock: run this from the site's root (composer.json and composer.lock)" >&2
      exit 1
    fi
    if ! command -v nix >/dev/null; then
      echo "relock: needs nix on PATH" >&2
      exit 1
    fi

    mkdir -p "$(dirname "$file")"
    old=""
    [[ -f "$file" ]] && old=$(cat "$file")
    restore() {
      if [[ -n "$old" ]]; then printf '%s\n' "$old" > "$file"; else rm -f "$file"; fi
    }
    stage() {
      if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git add -A -- "$file" 2>/dev/null || true
      fi
    }

    printf '%s\n' "$fake" > "$file"
    stage
    log=$(mktemp)
    trap 'rm -f "$log"' EXIT
    echo "relock: building $installable to learn its hash"
    if nix build --no-link "$installable" > "$log" 2>&1; then
      restore
      stage
      echo "relock: $installable built with lib.fakeHash; is it the vendor FOD?" >&2
      exit 1
    fi
    got=$(grep -oE 'got: +sha256-[A-Za-z0-9+/]{43}=' "$log" | tail -n1 | grep -oE 'sha256-.*' || true)
    if [[ -z "$got" ]]; then
      cat "$log" >&2
      restore
      stage
      echo "relock: the build failed before it reached a hash (above)" >&2
      exit 1
    fi

    printf '%s\n' "$got" > "$file"
    stage
    echo "relock: confirming $got"
    if ! nix build --no-link "$installable"; then
      restore
      stage
      echo "relock: $installable does not build with the hash it reported" >&2
      exit 1
    fi
    if [[ "$got" == "$old" ]]; then
      echo "relock: $file is unchanged ($got)"
    else
      echo "relock: wrote $got to $file"
    fi
  '';
  meta = {
    description = "Rewrite a Bedrock site's nix/vendor-hash after a composer.lock change";
    mainProgram = "wordpress-nix-relock";
  };
}
