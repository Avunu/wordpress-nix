# templates/bedrock-site carries what `wordpress-nix sync` renders for it: the
# template, in a throwaway repository named site-changeme with a flake.lock that
# locks wordpress-nix, syncs offline, and the only change is the three caller
# workflows (ci.yml, deps.yml, audit.yml). Those pin the locked wordpress-nix
# revision, which a template cannot know: the first `wordpress-init --sync` after
# `nix flake lock` writes them. Then a second sync changes nothing, and
# `sync --check` is clean.
{
  pkgs,
  checks,
  cli,
}:
checks.script "site-template-sync"
  {
    src = ../templates/bedrock-site;
    tools = [
      pkgs.git
      pkgs.jq
      cli
    ];
    env = {
      GIT_CONFIG_GLOBAL = "/dev/null";
      GIT_CONFIG_NOSYSTEM = "1";
      GIT_AUTHOR_NAME = "check";
      GIT_AUTHOR_EMAIL = "check@example.org";
      GIT_COMMITTER_NAME = "check";
      GIT_COMMITTER_EMAIL = "check@example.org";
    };
  }
  ''
    rev=0123456789abcdef0123456789abcdef01234567
    jq -n --arg rev "$rev" '{
      nodes: {
        root: { inputs: { "wordpress-nix": "wordpress-nix" } },
        "wordpress-nix": { locked: { owner: "Avunu", repo: "wordpress", rev: $rev, type: "github" } }
      },
      root: "root", version: 7
    }' > flake.lock
    git init -q -b main
    git remote add origin https://github.com/Avunu/site-changeme.git
    git add -A
    git commit -qm template

    wordpress-nix sync --write --offline
    changed="$(git status --porcelain | sort)"
    want="$(printf '%s\n' 'A  .github/workflows/audit.yml' 'A  .github/workflows/ci.yml' 'A  .github/workflows/deps.yml')"
    if [ "$changed" != "$want" ]; then
      echo "templates/bedrock-site differs from what wordpress-nix sync renders:" >&2
      echo "$changed" >&2
      git diff --cached -- . ':!.github/workflows' >&2
      exit 1
    fi
    grep -q "site-ci.yml@$rev # v" .github/workflows/ci.yml

    git commit -qm sync
    wordpress-nix sync --write --offline
    test -z "$(git status --porcelain)"
    wordpress-nix sync --check --offline | tee "$out"
  ''
