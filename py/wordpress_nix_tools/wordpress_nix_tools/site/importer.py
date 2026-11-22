"""``wordpress-nix site import <slug> --dump F [--files T] --home URL [--replace]``
(plan §8.6).

1. Finds the node running the site (node.py).
2. Refuses when ``wp core is-installed`` succeeds and ``--replace`` was not given;
   with ``--replace`` it first runs ``cluster-backup <slug>`` on the node.
3. Streams the audited dump into ``wp db import -``, and the JuiceFS tarball into
   ``tar -xz -C /var/lib/wordpress/uploads`` (``--files``), as ``wordpress``.
4. Confirms the database carries no trigger, routine, event or view.
5. The §4.9 rewrite to ``--home``; deactivates ``dropPlugins``
   (``.github/wordpress-nix.json``), even those whose files are gone;
   ``woocommerce_file_download_method = force`` on a Woo site; resets every
   administrator's and editor's password (staging: ``--skip-email``; at the
   production home: with the email); ``wp core update-db``, ``wp rewrite flush``.

No temporary file is left on the node; rerunning with ``--replace`` is the same.
"""

import json
import sys
from pathlib import Path

from wordpress_nix_tools.db import rewrite_urls
from wordpress_nix_tools.site.node import Container, NodeError, find_node

UPLOADS = "/var/lib/wordpress/uploads"

NO_EXECUTABLES_SQL = (
    "SELECT "
    "(SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()),"
    "(SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE()),"
    "(SELECT COUNT(*) FROM information_schema.EVENTS WHERE EVENT_SCHEMA = DATABASE()),"
    "(SELECT COUNT(*) FROM information_schema.VIEWS WHERE TABLE_SCHEMA = DATABASE())"
)


def deactivate_php(names: list[str]) -> str:
    """PHP for ``wp eval``: deactivate every active plugin whose directory (or file)
    is in NAMES, silently, which works for files that no longer exist."""
    listed = json.dumps(sorted(set(names)))
    return (
        f"$drop = json_decode('{listed}', true);"
        "$active = (array) get_option('active_plugins', array());"
        "$off = array_values(array_filter($active, function ($p) use ($drop) {"
        " return in_array(explode('/', $p)[0], $drop, true); }));"
        "if ($off) { deactivate_plugins($off, true); }"
        'echo implode("\\n", $off);'
    )


def site_json(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def run_import(c: Container, args, site: dict, log=print) -> int:
    rc, _, _ = c.wp(["core", "is-installed"])
    if rc == 0 and not args.replace:
        log(f"site import: {args.slug} already has a WordPress database; pass --replace (a cluster-backup runs first)")
        return 1
    if rc == 0:
        brc, _, err = c.host(f"cluster-backup {args.slug}")
        if brc != 0:
            log(f"site import: cluster-backup {args.slug} failed: {err.strip()}")
            return 3
        log("cluster-backup: done")
        rc2, _, err = c.wp(["db", "clean", "--yes"])
        if rc2 != 0:
            log(f"site import: wp db clean failed: {err.strip()}")
            return 3

    if c.stream(["wp", "db", "import", "-"], args.dump) != 0:
        log("site import: wp db import failed")
        return 3
    log("database imported")
    if args.files:
        if c.stream(["tar", "-xz", "-C", UPLOADS], args.files) != 0:
            log("site import: unpacking the uploads tarball failed")
            return 3
        log(f"uploads unpacked into {UPLOADS}")

    rc, out, _ = c.wp(["db", "query", NO_EXECUTABLES_SQL, "--skip-column-names"])
    counts = out.split()
    if rc != 0 or any(x != "0" for x in counts):
        log(f"site import: the database carries executable objects (triggers, routines, events, views: {counts})")
        return 1

    from_home = args.from_home or site.get("home") or ""
    if not from_home:
        log("site import: no legacy home (--from-home, or home in .github/wordpress-nix.json)")
        return 2
    media = args.media_url or f"https://media.{rewrite_urls.host_of(from_home)}"
    steps = rewrite_urls.build_steps(from_home, args.home, media, rewrite_urls.exclude_prefixes(args.inventory))
    if rewrite_urls.run(c.wp, steps, after=False, log=log) != 0:
        return 1

    drops = site.get("dropPlugins") or []
    if drops:
        rc, out, err = c.wp(["eval", deactivate_php(drops)])
        if rc != 0:
            log(f"site import: deactivating dropPlugins failed: {err.strip()}")
            return 1
        log(f"deactivated: {', '.join(out.split()) or 'none active'}")
    if site.get("woocommerce"):
        c.wp(["option", "update", "woocommerce_file_download_method", "force"])
        log("woocommerce_file_download_method = force")

    production = args.home.rstrip("/") == (site.get("home") or "").rstrip("/")
    rc, out, _ = c.wp(["user", "list", "--role__in=administrator,editor", "--field=ID"])
    ids = out.split()
    if ids:
        argv = ["user", "reset-password", *ids]
        if not production:
            argv.append("--skip-email")
        rc, _, err = c.wp(argv)
        if rc != 0:
            log(f"site import: resetting privileged passwords failed: {err.strip()}")
            return 1
        log(f"reset {len(ids)} privileged password(s){' (emailed)' if production else ' (no email: staging)'}")

    for argv in (["core", "update-db"], ["rewrite", "flush"]):
        rc, _, err = c.wp(argv)
        if rc != 0:
            log(f"site import: wp {' '.join(argv)} failed: {err.strip()}")
            return 1
    log("check: regenerate the Cwicly CSS (save once in the Cwicly editor) if the site uses Cwicly")
    return 0


def cmd(args, runner=None) -> int:
    try:
        kw = {"runner": runner} if runner else {}
        node = find_node(args.slug, args.node, **kw)
        c = Container(args.slug, node, **kw)
    except NodeError as e:
        print(f"site import: {e}", file=sys.stderr)
        return 3
    return run_import(c, args, site_json(args.site_json), log=lambda m: print(m, file=sys.stderr))
