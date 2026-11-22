"""Laying down templates/bedrock-site without ever clobbering.

The template is stock Bedrock plus the site flake, so it carries literal values
(``changeme``, ``php84``) rather than tokens: ``nix flake init -t`` uses it as it is.
It is rendered into a staging directory, where the site's values replace those
lines, and only then installed (``keep=True``: an existing file is left alone).
``composer.json`` and ``.gitignore`` are reconciled by their own steps.
"""

import atexit
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from wordpress_nix_tools.common import ui
from wordpress_nix_tools.wordpress_init.state import Run

# lib.fakeHash: the vendor FOD's hash before the first relock.
FAKE_HASH = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
SITE_JSON = ".github/wordpress-nix.json"
# Files the pipeline reconciles itself rather than installing from the template.
NOT_INSTALLED = (".gitignore", "composer.json", "composer.lock")


def nix_list(values: list[str]) -> str:
    return "[ " + " ".join(json.dumps(v) for v in values) + " ]" if values else "[ ]"


def dependabot_time(name: str) -> str:
    """A time between 00:00 and 05:55 hashed from the slug, so the fleet's daily
    Composer runs spread out (templates/bedrock-site/.github/dependabot.yml)."""
    minutes = int(hashlib.sha256(name.encode()).hexdigest()[:6], 16) % 72 * 5
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def render_flake(text: str, st: Run) -> str:
    theme = json.dumps(st.active_theme) if st.active_theme else "null"
    subs = (
        (r'name = "changeme";', f"name = {json.dumps(st.name)};"),
        (r'php = "php84";', f"php = {json.dumps(st.php)};"),
        (r"productionOnlyPlugins = \[ \];", f"productionOnlyPlugins = {nix_list(st.production_only)};"),
        (r"developmentOnlyPlugins = \[ \];", f"developmentOnlyPlugins = {nix_list(st.development_only)};"),
        (r'activeTheme = "twentytwentyfive";', f"activeTheme = {theme};"),
    )
    for pattern, value in subs:
        text = re.sub(pattern, lambda _m, v=value: v, text, count=1)
    return text


def render_template(st: Run) -> Path:
    """config.template rendered into a staging directory (once per run)."""
    if st.staging is not None:
        return st.staging
    staging = Path(tempfile.mkdtemp(prefix="wordpress-init."))
    atexit.register(shutil.rmtree, staging, True)
    shutil.copytree(st.config.template, staging, dirs_exist_ok=True, symlinks=True)
    for root, _dirs, files in os.walk(staging):
        os.chmod(root, os.stat(root).st_mode | stat.S_IWUSR)
        for f in files:
            p = Path(root, f)
            p.chmod(p.stat().st_mode | stat.S_IWUSR)
    edits = {
        "flake.nix": lambda t: render_flake(t, st),
        "README.md": lambda t: t.replace("# changeme", f"# {st.name}", 1),
        ".github/dependabot.yml": lambda t: re.sub(
            r'time: "\d\d:\d\d"', f'time: "{dependabot_time(st.name)}"', t, count=1
        ),
        ".env.example": lambda t: (
            t if st.db_prefix == "wp_" else t.replace("# DB_PREFIX='wp_'", f"DB_PREFIX='{st.db_prefix}'", 1)
        ),
    }
    for rel, edit in edits.items():
        p = staging / rel
        if p.is_file():
            p.write_text(edit(p.read_text()))
    (staging / "nix").mkdir(exist_ok=True)
    (staging / "nix" / "vendor-hash").write_text(FAKE_HASH + "\n")
    st.staging = staging
    return staging


def staged_files(staging: Path) -> list[str]:
    return sorted(str(p.relative_to(staging)) for p in staging.rglob("*") if p.is_file())


def install_template(st: Run, *, keep: bool = True) -> None:
    staging = render_template(st)
    for rel in staged_files(staging):
        if rel in NOT_INSTALLED:
            continue
        dest = st.root / rel
        if keep and (dest.exists() or dest.is_symlink()):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(staging / rel, dest)
        ui.info(f"+ {rel}")


def site_json(st: Run) -> dict:
    """``.github/wordpress-nix.json``: what the site's CI, the cluster app and the
    import read about this site."""
    return {
        "name": st.name,
        "home": st.home,
        "php": st.php,
        "dbPrefix": st.db_prefix,
        "locale": st.locale or "en_US",
        "woocommerce": st.woocommerce,
        "activeTheme": st.active_theme or None,
        "productionOnlyPlugins": st.production_only,
        "developmentOnlyPlugins": st.development_only,
        # What `site import` deactivates: dropped plugins the database has active.
        "dropPlugins": sorted({i.name for i in st.dropped if i.kind == "plugin" and i.active}),
    }


def write_site_json(st: Run) -> None:
    """Written on a conversion; on a reconcile only seeded when missing."""
    path = st.root / SITE_JSON
    if path.is_file() and st.mode == "reconcile":
        return
    text = json.dumps(site_json(st), indent=2) + "\n"
    if path.is_file() and path.read_text() == text:
        ui.info(f". {SITE_JSON} already current")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    ui.info(f"+ {SITE_JSON}")
