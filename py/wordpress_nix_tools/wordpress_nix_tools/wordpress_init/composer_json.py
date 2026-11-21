"""composer.json, reconciled surgically (§8.5 step 4.5): the site's name, core and
every Composer disposition at its exact installed version, ``avunu/*``,
``wp-plugin/two-factor``, ``config.platform.php``, and WP Packages' ``only`` guard.
Keys the site added itself are left alone. ``loosen`` is step 11's second commit."""

import json
from pathlib import Path

from wordpress_nix_tools.common import ui
from wordpress_nix_tools.common.versions import caret
from wordpress_nix_tools.wordpress_init.state import Run

WP_PACKAGES = "https://repo.wp-packages.org"
ONLY = ["wp-plugin/*", "wp-theme/*"]
TEMPLATE_THEME = "wp-theme/twentytwentyfive"


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def dump(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def ensure_only_guard(doc: dict) -> None:
    repos = doc.setdefault("repositories", [])
    if isinstance(repos, dict):
        repos = [dict(v, name=k) if isinstance(v, dict) else v for k, v in repos.items()]
        doc["repositories"] = repos
    for repo in repos:
        if isinstance(repo, dict) and str(repo.get("url", "")).rstrip("/") == WP_PACKAGES:
            repo["only"] = sorted(set(repo.get("only") or []) | set(ONLY))
            return
    repos.insert(0, {"name": "wp-packages", "type": "composer", "url": WP_PACKAGES, "only": list(ONLY)})


def reconcile(st: Run) -> None:
    path = st.root / "composer.json"
    staged = (st.staging / "composer.json") if st.staging else None
    template = load(staged) if staged and staged.is_file() else {}
    if path.is_file():
        doc = load(path)
    else:
        doc = template
    old = path.read_text() if path.is_file() else None

    if doc.get("name", "").endswith("/site-changeme") or not doc.get("name"):
        doc["name"] = f"avunu/site-{st.name}"
    req = doc.setdefault("require", {})
    if st.mode in ("migrate", "init"):
        if st.wp_version:
            req["roots/wordpress"] = st.wp_version
        site_themes = {i.name for i in st.items if i.kind == "theme" and i.disp != "drop"}
        if st.mode == "migrate" and TEMPLATE_THEME in req and "twentytwentyfive" not in site_themes:
            del req[TEMPLATE_THEME]
        for item in st.items:
            if item.disp in ("composer", "avunu") and item.package:
                req[item.package] = item.constraint
    two_factor = (template.get("require") or {}).get("wp-plugin/two-factor")
    if two_factor:
        req.setdefault("wp-plugin/two-factor", two_factor)
    doc["require"] = dict(sorted(req.items(), key=lambda kv: (kv[0] != "php", kv[0])))
    doc.setdefault("config", {}).setdefault("platform", {})["php"] = st.preset.platform
    ensure_only_guard(doc)

    text = dump(doc)
    if old == text:
        ui.info(". composer.json already current")
        return
    path.write_text(text)
    ui.info("+ composer.json")


def loosen(root: Path) -> bool:
    """wp-plugin/* and wp-theme/* from exact pins to ``^x.y``; True when it changed."""
    path = root / "composer.json"
    doc = load(path)
    req = doc.get("require") or {}
    changed = False
    for name, constraint in list(req.items()):
        if name.startswith(("wp-plugin/", "wp-theme/")) and constraint[:1].isdigit():
            req[name] = caret(constraint)
            changed = True
    if changed:
        path.write_text(dump(doc))
    return changed
