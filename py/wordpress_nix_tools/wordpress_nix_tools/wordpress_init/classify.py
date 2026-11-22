"""Every directory in wp-content/{plugins,themes,mu-plugins} and every drop-in, with
a disposition and flags (§8.5 step 2). Read-only: nothing moves until the pipeline.

Dispositions:
- ``composer``: wordpress.org knows the slug, ``Update URI`` is empty or theirs, the
  author matches, the installed copy matches what wordpress.org published for that
  version, and WP Packages has that version: ``wp-plugin/<slug>`` or
  ``wp-theme/<slug>`` at the installed version;
- ``avunu``: the directory and its header are an Avunu Packagist package;
- ``commit``: premium or custom code (``premium-slugs.json``, an ``Update URI`` that
  is not wordpress.org's, a slug wordpress.org does not know), moved to web/app/;
- ``drop``: the drop list, drop-ins, ``*.bak`` and empty directories, inactive code
  (unless ``--keep-inactive``; an active child theme keeps its parent), host
  mu-plugins; moved to the backup and recorded in ``dropPlugins``;
- ``platform``: ``mu-plugins/platform-*.php`` (the build injects its own).

Flags: ``modified``, ``missing-code`` and ``db-code`` block; ``collision``,
``not-in-registry``, ``delisted``, ``writes-to-disk``, ``mpdf-tempdir``, ``php-floor``,
``suspicious-code`` and ``salt-dependent`` are warnings.
"""

import re
from pathlib import Path

from wordpress_nix_tools.common import data
from wordpress_nix_tools.common.versions import compare, normalize
from wordpress_nix_tools.wordpress_init import wporg
from wordpress_nix_tools.wordpress_init.state import Item, Run

HEADER = re.compile(r"^[ \t/*#@]*([A-Za-z][A-Za-z ]*?):[ \t]*(.*?)[ \t]*(?:\*/)?$", re.MULTILINE)
PLUGIN_KEYS = ("Plugin Name", "Version", "Author", "Update URI", "Requires PHP", "Plugin URI", "Text Domain")
THEME_KEYS = ("Theme Name", "Version", "Author", "Template", "Update URI", "Requires PHP", "Theme URI")

SUSPICIOUS = re.compile(
    r"eval\s*\(\s*(?:base64_decode|gzinflate|gzuncompress|str_rot13|\$_(?:POST|GET|REQUEST|COOKIE))"
    r"|assert\s*\(\s*\$_(?:POST|GET|REQUEST|COOKIE)"
    r"|preg_replace\s*\(\s*['\"][^'\"]*/e[a-z]*['\"]"
    r"|create_function\s*\("
    r"|\$\{?['\"]?_(?:POST|GET|REQUEST|COOKIE)['\"]?\}?\s*\[[^\]]+\]\s*\("
    r"|(?:\\x[0-9a-fA-F]{2}){12,}",
)
SCAN_LIMIT = 2_000_000


def headers(text: str, keys: tuple[str, ...]) -> dict[str, str]:
    """WordPress's get_file_data(): the first 8 KiB, ``Key: value`` lines."""
    head = text[:8192]
    out = {}
    for m in HEADER.finditer(head):
        key = m.group(1).strip()
        if key in keys and key not in out:
            out[key] = m.group(2).strip()
    return out


def _read(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(8192)
    except OSError:
        return ""


def plugin_header(path: Path) -> dict[str, str]:
    """The header of a plugin directory's main file, or of a single-file plugin."""
    if path.is_file():
        h = headers(_read(path), PLUGIN_KEYS)
        return h if "Plugin Name" in h else {}
    candidates = [path / f"{path.name}.php"] + sorted(path.glob("*.php"))
    for f in candidates:
        if f.is_file():
            h = headers(_read(f), PLUGIN_KEYS)
            if "Plugin Name" in h:
                return h
    return {}


def theme_header(path: Path) -> dict[str, str]:
    h = headers(_read(path / "style.css"), THEME_KEYS)
    return h if "Theme Name" in h else {}


def _is_empty(path: Path) -> bool:
    return path.is_dir() and not any(p.is_file() for p in path.rglob("*"))


def inventory(root: Path) -> list[Item]:
    """What is on disk, unclassified."""
    wpc = root / "wp-content"
    dropins = set(data.load("drop-plugins").get("dropins", []))
    items: list[Item] = []
    for kind, sub in (("plugin", "plugins"), ("theme", "themes"), ("mu-plugin", "mu-plugins")):
        d = wpc / sub
        if not d.is_dir():
            continue
        for entry in sorted(d.iterdir(), key=lambda p: p.name):
            name = entry.name
            if name.startswith(".") or name == "index.php":
                continue
            is_dir = entry.is_dir()
            if not is_dir and kind == "theme":
                continue
            if not is_dir and kind == "plugin" and not name.endswith((".php", ".bak")):
                continue
            if not is_dir and kind == "mu-plugin" and not name.endswith((".php", ".bak")):
                continue
            header = theme_header(entry) if kind == "theme" else plugin_header(entry)
            items.append(
                Item(
                    kind=kind,
                    name=name,
                    is_dir=is_dir,
                    header=header,
                    version=normalize(header.get("Version", "")),
                    parent=header.get("Template", "") if kind == "theme" else "",
                )
            )
    if wpc.is_dir():
        for entry in sorted(wpc.iterdir(), key=lambda p: p.name):
            if entry.is_file() and entry.name in dropins:
                items.append(Item(kind="dropin", name=entry.name, is_dir=False))
    return items


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def author_matches(header_author: str, catalog_author: str) -> bool:
    a, b = _norm(re.sub(r"<[^>]+>", "", header_author)), _norm(catalog_author)
    if not a or not b:
        return True
    return a in b or b in a


def _wporg_update_uri(uri: str) -> bool:
    uri = uri.strip().lower()
    return not uri or "wordpress.org" in uri or "w.org/" in uri


def _avunu_package(item: Item, catalog: wporg.Catalog) -> tuple[str, str] | None:
    """(package, constraint) when the directory and its header are Avunu's."""
    table = data.load("premium-slugs").get("avunu", {})
    entry = table.get(item.name)
    package = entry.get("package") if isinstance(entry, dict) else entry
    constraint = entry.get("constraint", "") if isinstance(entry, dict) else ""
    if not package:
        package = catalog.avunu(item.name)
    if not package:
        return None
    blob = " ".join(item.header.get(k, "") for k in ("Author", "Plugin URI", "Update URI", "Theme URI"))
    if "avunu" not in blob.lower():
        return None
    if not constraint:
        parts = item.version.split(".")
        constraint = "^" + ".".join(parts[:2]) if item.version else "*"
    return package, constraint


def classify_item(st: Run, item: Item, catalog: wporg.Catalog) -> None:
    a = st.args
    accepted = a.accepted()
    premium = set(data.load("premium-slugs").get("premium", []))
    slug = item.slug

    if item.kind == "dropin":
        item.disp, item.reason = "drop", "a drop-in: the platform runs none"
        return
    if item.kind == "mu-plugin":
        if not item.is_dir and item.name.startswith("platform-") and item.name.endswith(".php"):
            item.disp, item.reason = "platform", "the build injects its own platform-*.php"
            return
        why = data.host_mu_reason(item.name)
        if why:
            item.disp, item.reason = "drop", f"host mu-plugin: {why}"
            return
    if item.name.endswith(".bak") or ".bak" in item.name:
        item.disp, item.reason = "drop", "a backup copy (*.bak)"
        return
    if item.is_dir and _is_empty(st.root / item.source):
        item.disp, item.reason = "drop", "an empty directory"
        return
    if slug in a.drop_plugins or item.name in a.drop_plugins:
        item.disp, item.reason = "drop", "--drop-plugin"
        return
    if item.kind == "mu-plugin":
        item.disp, item.reason = "commit", "a must-use plugin: committed code"
        return

    kept = slug in a.keep_plugins or item.name in a.keep_plugins
    why = data.drop_reason(slug)
    if why and not kept:
        item.disp, item.reason = "drop", why
        return
    if item.active is False and not a.keep_inactive and not kept:
        item.disp, item.reason = "drop", f"inactive {item.kind}"
        return

    avunu = _avunu_package(item, catalog)
    if avunu:
        item.disp, (item.package, item.constraint) = "avunu", avunu
        item.reason = "an Avunu package on Packagist"
        return

    update_uri = item.header.get("Update URI", "")
    if slug in premium:
        item.disp, item.reason = "commit", "premium (premium-slugs.json)"
        return
    if not _wporg_update_uri(update_uri):
        item.disp, item.reason = "commit", f"Update URI {update_uri!r}: not wordpress.org's"
        return

    info = catalog.info(item.kind, slug)
    if info is None:
        item.disp, item.reason = "commit", "not on wordpress.org: custom or premium"
        return
    if info.closed:
        item.flag("delisted", "wordpress.org has closed this slug; the code is committed")
        item.disp, item.reason = "commit", "closed on wordpress.org"
        return
    if not author_matches(item.header.get("Author", ""), info.author):
        reason = data.drop_reason(slug, collision=True)
        item.flag(
            "collision",
            f"the slug is wordpress.org's ({info.author or '?'}), this copy's author is "
            f"{item.header.get('Author', '?')!r}",
        )
        if reason and not kept:
            item.disp, item.reason = "drop", reason
        else:
            item.disp, item.reason = "commit", "a different plugin under a wordpress.org slug"
        return

    registry = catalog.registry(item.kind, slug)
    if not item.version or registry is None or item.version not in registry:
        item.flag(
            "not-in-registry",
            f"WP Packages has no wp-{item.kind}/{slug} {item.version or '(no version)'}",
        )
        item.disp, item.reason = "commit", "the installed version is not in the registry"
        return

    published = catalog.files(item.kind, slug, item.version)
    if published is None:
        item.flag("not-in-registry", f"wordpress.org publishes no files for {slug} {item.version}")
        item.disp, item.reason = "commit", "no published files to verify against"
        return
    changed = wporg.modified_files(st.root / item.source, published) if item.is_dir else []
    if changed:
        how = accepted.get(slug) or accepted.get(item.name)
        item.flag("modified", f"{len(changed)} file(s) differ from {slug} {item.version}: {', '.join(changed[:5])}")
        if how == "vendor":
            item.decided.append("modified")
            item.disp, item.reason = "commit", "--accept-modified vendor: the modified copy is committed"
            return
        if how == "replace":
            item.decided.append("modified")
        else:
            item.disp, item.reason = "commit", "modified: decide with --accept-modified"
            return

    item.disp = "composer"
    item.package = f"wp-{item.kind}/{slug}"
    item.constraint = item.version
    item.reason = "wordpress.org, verified against the published files"


def scan_suspicious(path: Path) -> list[str]:
    """PHP files in committed code with an obfuscation or eval-of-input pattern."""
    hits = []
    files = [path] if path.is_file() else sorted(path.rglob("*.php"))
    for f in files:
        try:
            if f.stat().st_size > SCAN_LIMIT:
                continue
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if SUSPICIOUS.search(text):
            hits.append(f.name if path.is_file() else f.relative_to(path).as_posix())
    return hits


def flag_item(st: Run, item: Item) -> None:
    """The warnings and decisions that apply to kept code."""
    if item.disp in ("drop", "platform") or not item.present:
        return
    slug = item.slug
    writers = data.load("runtime-writers").get("plugins", {})
    w = writers.get(slug)
    if w:
        if w.get("mpdf"):
            item.flag("mpdf-tempdir", w.get("note", ""))
        if w.get("writes"):
            item.flag("writes-to-disk", f"{', '.join(w['writes'])}: {w.get('note', '')}")
    salts = data.load("salt-dependent-plugins").get("plugins", {})
    if slug in salts:
        item.flag("salt-dependent", salts[slug])
    floor = item.header.get("Requires PHP", "")
    if floor and compare(floor, st.preset.platform) > 0:
        item.flag("php-floor", f"requires PHP {floor}; the site builds with {st.preset.platform}")
    dbcode = data.load("db-code-plugins").get("plugins", {}).get(slug)
    if dbcode:
        item.flag("db-code", dbcode.get("why", ""))
        default = dbcode.get("default")
        if slug in st.args.keep_plugins:
            item.decided.append("db-code")
            item.notes.append("db-code: kept with --keep-plugin")
        elif default:
            item.decided.append("db-code")
            if default.get("developmentOnly") and slug not in st.development_only:
                st.development_only.append(slug)
            item.notes.append(f"db-code: {default.get('note', 'the default plan applies')}")
    if item.disp == "commit":
        hits = scan_suspicious(st.root / item.source)
        if hits:
            item.flag("suspicious-code", ", ".join(hits[:5]))


def classify(st: Run, catalog: wporg.Catalog) -> None:
    """st.items from the tree and the active set, every one classified."""
    items = inventory(st.root)
    st.items = items
    themes = {i.name: i for i in items if i.kind == "theme"}
    active_themes = {t for t in (st.stylesheet, st.template) if t}
    # A child theme's parent stays with it whatever the survey says.
    for t in list(active_themes):
        parent = themes.get(t)
        if parent and parent.parent:
            active_themes.add(parent.parent)
    for item in items:
        if item.kind == "plugin" and st.active_plugins is not None:
            item.active = item.name in st.active_plugins
        elif item.kind == "theme" and active_themes:
            item.active = item.name in active_themes
        elif item.kind == "mu-plugin":
            item.active = True
        classify_item(st, item, catalog)
        flag_item(st, item)

    # Active in the database, absent on disk.
    present = {i.name for i in items if i.kind == "plugin"}
    for name in sorted((st.active_plugins or set()) - present):
        item = Item(kind="plugin", name=name, is_dir=not name.endswith(".php"), active=True, present=False)
        why = data.drop_reason(item.slug)
        if why or item.slug in st.args.drop_plugins:
            item.disp = "drop"
            item.reason = f"active in the database but absent on disk; {why or '--drop-plugin'}"
        else:
            item.disp = "missing"
            item.flag("missing-code", "active in the database but absent on disk")
        st.items.append(item)
    for name in sorted(active_themes - set(themes)):
        item = Item(kind="theme", name=name, active=True, present=False, disp="missing")
        item.flag("missing-code", "the active theme (or its parent) is absent on disk")
        st.items.append(item)

    # The companion of a db-code default (wpcodebox_functionality_plugin) is committed
    # code even when inactive: it is what production runs instead.
    for item in st.items:
        if item.kind != "plugin" or "db-code" not in item.decided:
            continue
        default = (data.load("db-code-plugins").get("plugins", {}).get(item.slug) or {}).get("default") or {}
        companion = default.get("companion")
        for other in st.items:
            if other.name == companion and other.disp == "drop" and other.reason.startswith("inactive"):
                other.disp, other.reason = "commit", f"the committed snippets of {item.slug} (D12)"

    st.woocommerce = any(i.slug == "woocommerce" and i.disp != "drop" for i in st.items)
    active_theme = st.stylesheet if st.stylesheet in themes else ""
    st.active_theme = active_theme
