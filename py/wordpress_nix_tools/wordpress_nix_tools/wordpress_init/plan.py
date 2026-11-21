"""The plan (§8.5 step 3): the disposition table and flags, the runtime writers,
the tracked-secret audit and the existing-repository note. Pure inspection, so
``--dry-run`` prints all of it."""

import os

from wordpress_nix_tools import repo_adopt
from wordpress_nix_tools.common import data, ui
from wordpress_nix_tools.wordpress_init import detect
from wordpress_nix_tools.wordpress_init.secret_files import audit_tracked_secrets
from wordpress_nix_tools.wordpress_init.state import Run

LABEL = {
    "composer": "composer",
    "avunu": "avunu",
    "commit": "commit",
    "drop": "DROP",
    "platform": "platform",
    "missing": "MISSING",
}


def _row(item) -> str:
    what = f"{item.kind}/{item.name}"
    disp = LABEL.get(item.disp, item.disp)
    if item.disp in ("composer", "avunu"):
        detail = f"{item.package}:{item.constraint}"
    elif item.disp == "commit":
        detail = f"→ {item.target}  ({item.reason})"
    else:
        detail = item.reason
    flags = f"  [{','.join(item.flags)}]" if item.flags else ""
    version = f" {item.version}" if item.version else ""
    return f"  {what + version:<44} {disp:<9} {detail}{flags}"


def adopt_check(st: Run) -> str:
    """What `wordpress-nix repo adopt <name> --check` decides (D6). It asks GitHub; without
    gh or the network the plan says so and carries on."""
    try:
        d = repo_adopt.Adopter(cwd=str(st.root)).decide(repo_adopt.ORG, st.name)
    except Exception as e:  # noqa: BLE001 — the note is advisory; the plan goes on.
        return f"unavailable ({str(e).strip() or type(e).__name__}); run `wordpress-nix repo adopt {st.name} --check`"
    return f"{d.repo}: {d.action} — {'; '.join(d.reasons)}"


def print_plan(st: Run) -> None:
    ui.step(f"Plan for {os.getcwd()}")
    ui.info(f"site           : {st.name}")
    ui.info(f"WordPress      : {st.wp_version or '?'}  (roots/wordpress)")
    ui.info(f"PHP            : {st.preset.label} ({st.php}; config.platform.php {st.preset.platform})")
    ui.info(f"home           : {st.home or '(set --home-url)'}")
    ui.info(f"table prefix   : {st.db_prefix}  (DB_PREFIX)")
    if st.survey_path or st.dump_path:
        ui.info(f"active set     : {st.survey_path or st.dump_path}")
    else:
        ui.info("active set     : unknown (--keep-inactive)")
    if st.woocommerce:
        ui.info("WooCommerce    : yes (Woo minors go to review)")
    ui.out()
    for item in st.items:
        ui.out(_row(item))
        for note in item.notes:
            ui.out(f"      {note}")
    ui.out()

    if st.development_only:
        ui.info(f"developmentOnlyPlugins: {', '.join(st.development_only)}")
    if st.active_theme:
        ui.info(f"activeTheme: {st.active_theme}")

    writers = [i for i in st.items if i.has("writes-to-disk") or i.has("mpdf-tempdir")]
    if writers:
        ui.step("Runtime writers (§4.8)")
        local = ", ".join(data.load("runtime-writers").get("localPrefixes", []))
        ui.info(f"uploads/{{{local}}} and private prefixes live on the JuiceFS volume, never R2")
        for i in writers:
            for note in i.notes:
                if note.startswith(("writes-to-disk", "mpdf-tempdir")):
                    ui.info(f"{i.name}: {note}")

    if st.locale not in ("", "en_US"):
        st.warn(f"non-english-locale: the site's locale is {st.locale}; add inpsyde/wp-translation-downloader for it")
    salted = [i.name for i in st.items if i.has("salt-dependent")]
    if salted and not st.args.keep_salts:
        st.warn(
            f"salt-dependent: {', '.join(salted)} keep secrets encrypted with the legacy salts; "
            "re-enter those credentials after cutover, or rerun with --keep-salts on a trusted host"
        )
    php_uploads = [
        f
        for f in ((st.survey or {}).get("uploads") or {}).get("phpFiles") or []
        # The WPCodeBox stub is the one executable media offload keeps (§4.8).
        if f != "wpcodebox/index.php"
    ]
    if php_uploads:
        st.warn(
            f"uploads holds {len(php_uploads)} PHP file(s) ({', '.join(php_uploads[:5])}): "
            "media offload never copies them; treat the host as compromised"
        )
    for item in st.items:
        if item.has("suspicious-code"):
            st.warn(f"suspicious-code in committed {item.kind} {item.name}: {item.notes[-1] if item.notes else ''}")
        if item.has("collision") and item.disp != "drop":
            st.warn(f"collision: {item.kind} {item.name} is not the wordpress.org plugin of that slug")

    audit_tracked_secrets(st)

    remote = detect.has_remote(st.root)
    history = detect.history_secrets(st.root)
    if remote or history:
        ui.step("Existing repository")
        if remote:
            ui.info(f"origin: {remote}")
            ui.info(f"repo adopt --check: {adopt_check(st)}")
        if history:
            st.warn(
                "this repository's history holds "
                + ", ".join(history[:5])
                + (" …" if len(history) > 5 else "")
                + ": `repo adopt` archives it as site-<slug>-legacy and pushes the conversion to a fresh repository (D6)"
            )

    blocked = st.blocked
    if blocked:
        ui.step("Blocked")
        for item in blocked:
            for flag in item.blocking:
                hint = {
                    "modified": f"--accept-modified {item.slug}:replace|vendor",
                    "missing-code": f"--drop-plugin {item.slug} (or restore the code)",
                    "db-code": f"--keep-plugin {item.slug} or --drop-plugin {item.slug}",
                }[flag]
                ui.err(f"{item.kind} {item.name}: {flag} — decide with {hint}")

    if st.args.strict and st.warnings:
        ui.die(f"--strict: {len(st.warnings)} warning(s)")
