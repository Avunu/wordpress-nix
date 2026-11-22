"""Read-only probes of the target directory: what it is, and what WordPress says.

Shapes (§8.5): ``empty``; ``bedrock`` (Bedrock plus a wordpress-nix flake: reconcile);
``classic`` (a WordPress install: migrate); ``v2`` (a wordpress-nix v2 site, whose
flake moves to the backup: migrate); anything else is not WordPress.
"""

import os
import re
from pathlib import Path

from wordpress_nix_tools.common.proc import git, probe

WP_VERSION = re.compile(r"""\$wp_version\s*=\s*['"]([^'"]+)['"]""")
TABLE_PREFIX = re.compile(r"""^\s*\$table_prefix\s*=\s*['"]([^'"]*)['"]""", re.MULTILINE)
MULTISITE = re.compile(r"""define\s*\(\s*['"](MULTISITE|WP_ALLOW_MULTISITE)['"]\s*,\s*(true|1|'1')""", re.I)
CONFIG_KEY = re.compile(
    r"""define\s*\(\s*['"]([A-Z_]+)['"]\s*,\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|(\w+))\s*\)"""
)
SALT_KEYS = (
    "AUTH_KEY",
    "SECURE_AUTH_KEY",
    "LOGGED_IN_KEY",
    "NONCE_KEY",
    "AUTH_SALT",
    "SECURE_AUTH_SALT",
    "LOGGED_IN_SALT",
    "NONCE_SALT",
)

# Legacy core at a classic install's root: what migrate moves to the backup.
CORE_PATHS = (
    "wp-admin",
    "wp-includes",
    "index.php",
    "license.txt",
    "readme.html",
    "xmlrpc.php",
    "wp-activate.php",
    "wp-blog-header.php",
    "wp-comments-post.php",
    "wp-config-sample.php",
    "wp-cron.php",
    "wp-links-opml.php",
    "wp-load.php",
    "wp-login.php",
    "wp-mail.php",
    "wp-settings.php",
    "wp-signup.php",
    "wp-trackback.php",
    ".htaccess",
    ".user.ini",
    "php.ini",
)


def read(path: str | Path) -> str:
    try:
        return Path(path).read_text(errors="replace")
    except OSError:
        return ""


def dir_is_empty(path: str = ".") -> bool:
    try:
        return not any(n for n in os.listdir(path) if n != ".git")
    except OSError:
        return True


def is_wordpress_nix_flake(text: str) -> bool:
    return "wordpress-nix.flakeModules.default" in text or "wordpress-nix.site" in text


def is_v2_flake(text: str) -> bool:
    return ("Avunu/wordpress" in text or "wordpress-nix" in text) and not is_wordpress_nix_flake(text)


def is_bedrock(root: Path) -> bool:
    return (root / "composer.json").is_file() and (root / "config" / "application.php").is_file()


def _has_code(root: Path) -> bool:
    wpc = root / "wp-content"
    for sub in ("plugins", "themes", "mu-plugins"):
        d = wpc / sub
        if d.is_dir() and any(p.name not in ("index.php",) and not p.name.startswith(".") for p in d.iterdir()):
            return True
    return False


def is_classic(root: Path) -> bool:
    return (root / "wp-includes" / "version.php").is_file() or _has_code(root)


def shape(root: Path) -> str:
    flake = read(root / "flake.nix")
    if dir_is_empty(str(root)):
        return "empty"
    if flake and is_wordpress_nix_flake(flake) and is_bedrock(root):
        return "bedrock"
    if flake and is_v2_flake(flake) and _has_code(root):
        return "v2"
    if is_classic(root):
        return "classic"
    return ""


def has_foreign_flake(root: Path) -> bool:
    flake = read(root / "flake.nix")
    return bool(flake) and not is_wordpress_nix_flake(flake) and not is_v2_flake(flake)


def wp_version(root: Path) -> str:
    m = WP_VERSION.search(read(root / "wp-includes" / "version.php"))
    return m.group(1) if m else ""


def config_values(root: Path) -> dict[str, str]:
    """``define('KEY', 'value')`` pairs of wp-config.php (never evaluated)."""
    out = {}
    for m in CONFIG_KEY.finditer(read(root / "wp-config.php")):
        out[m.group(1)] = next(g for g in m.groups()[1:] if g is not None)
    return out


def table_prefix(root: Path) -> str:
    m = TABLE_PREFIX.search(read(root / "wp-config.php"))
    return m.group(1) if m else ""


def is_multisite(root: Path) -> bool:
    return bool(MULTISITE.search(read(root / "wp-config.php")))


def repo_state(root: Path) -> str:
    """``own`` (root is its repository's top), ``nested`` (inside another), ``none``."""
    top = probe(git("rev-parse", "--show-toplevel", cwd=str(root)))
    if top is None:
        return "none"
    return "own" if Path(top).resolve() == root.resolve() else "nested"


def has_remote(root: Path) -> str:
    return probe(git("remote", "get-url", "origin", cwd=str(root))) or ""


def history_secrets(root: Path) -> list[str]:
    """Paths in the repository's history that D6 treats as disqualifying: any
    ``*.sql``, ``wp-config.php`` or ``.env`` ever committed. Offline: local history."""
    out = probe(git("log", "--all", "--pretty=format:", "--name-only", "--diff-filter=A", cwd=str(root)))
    if not out:
        return []
    found = set()
    for line in out.splitlines():
        name = line.strip()
        base = name.rsplit("/", 1)[-1]
        if base.endswith((".sql", ".sql.gz")) or base in ("wp-config.php", ".env", "auth.json"):
            found.add(name)
    return sorted(found)
