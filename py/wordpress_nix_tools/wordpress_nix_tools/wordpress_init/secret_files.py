"""Secrets, as far as the scaffolder goes: tracked credentials reported (never
fixed: untracking is a staged deletion, and rotation is a human decision), age
files the build cannot see refused, and secrets/README.md seeded in a new site."""

import os

from wordpress_nix_tools.common import ui
from wordpress_nix_tools.common.proc import git, probe, probe_ok
from wordpress_nix_tools.wordpress_init.state import Run

TRACKED = ("wp-config.php", ".env", "auth.json", "*.sql", "*.sql.gz", "wp-content/*.sql", "**/*.sql")

SECRETS_README = """\
# secrets/

The site's own secrets are the cluster's (cluster-ovh-01, agenix-rekey): the
salts and the database password are generated there, and `wordpress-nix media
provision` and `edge provision` print the fragments `avc secrets provision`
takes. Nothing here is read by the build.

A secret a plugin needs in development goes in `.env`, which git ignores.
"""


def audit_tracked_secrets(st: Run) -> None:
    tracked = probe(git("ls-files", "--", *TRACKED, cwd=str(st.root))) or ""
    for f in sorted(set(tracked.splitlines())):
        if not f:
            continue
        st.warn(
            f"""{f} is tracked by git.

     It holds (or held) the legacy site's credentials: the database password, the
     salts, and any S3 or API keys. The conversion moves it to .wordpress-nix-backup/,
     which stages its deletion, but the values stay in the repository's history.
     Treat every credential it has ever held as disclosed and rotate it, and see
     `wordpress-nix repo adopt` for a fresh repository (D6)."""
        )


def verify_secrets_visible(st: Run) -> None:
    """An .age file is ciphertext and is committed; an ignored one is invisible to the
    build."""
    found = []
    for root, _, files in os.walk(st.root / "secrets"):
        found += [os.path.join(root, f) for f in files if f.endswith(".age")]
    bad = False
    for p in sorted(found):
        rel = os.path.relpath(p, st.root)
        if probe_ok(git("check-ignore", "-q", "--", rel, cwd=str(st.root))):
            ui.err(f"{rel} is excluded by .gitignore")
            bad = True
    if bad:
        ui.die("the site's .gitignore excludes age secrets (they are ciphertext and belong in the repo)")
