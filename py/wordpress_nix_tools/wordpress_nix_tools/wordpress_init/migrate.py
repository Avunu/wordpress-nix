"""Migrate mode: convert a pulled classic WordPress install (or a v2 wordpress-nix
site) into a stock Bedrock site, in place, and reconcile one that already is.

Runs on a pulled copy, never on the legacy server (docs/scaffolding/migrate.md). It
needs the survey or the dump to know what is active, never deletes, stages before
locking, and fails hard when the relock fails.
"""

import json
import os
from pathlib import Path

from wordpress_nix_tools.common import records, ui
from wordpress_nix_tools.common.proc import git, probe
from wordpress_nix_tools.common.versions import at_least
from wordpress_nix_tools.db import dump as dumpmod
from wordpress_nix_tools.legacy import model
from wordpress_nix_tools.wordpress_init import classify, detect, wporg
from wordpress_nix_tools.wordpress_init.pipeline import load_plan, run_pipeline
from wordpress_nix_tools.wordpress_init.plan import print_plan
from wordpress_nix_tools.wordpress_init.state import Run, normalize_name


def find_records(st: Run) -> None:
    """--survey/--dump, else auto-discovered in .wordpress-nix-backup/."""
    a = st.args
    survey = a.survey or (str(records.find(st.root, "survey.json") or ""))
    dump = a.dump
    if not dump:
        for name in ("dump.sql", "import.sql", "database.sql"):
            found = records.find(st.root, name)
            if found:
                dump = str(found)
                break
    if survey:
        try:
            st.survey = json.loads(Path(survey).read_text())
        except (OSError, ValueError) as e:
            ui.die(f"cannot read the survey {survey}: {e}", 2)
        st.survey_path = survey
    if dump:
        if not Path(dump).is_file():
            ui.die(f"no dump at {dump}", 2)
        st.dump_path = dump


def resolve_active_set(st: Run) -> None:
    if st.survey is not None:
        st.active_plugins = model.active_plugins(st.survey)
        st.stylesheet, st.template = model.active_theme(st.survey)
        return
    if st.dump_path:
        opts = dumpmod.options(st.dump_path, st.db_prefix)
        st.active_plugins, st.stylesheet, st.template = model.active_from_options(opts)
        st.home = st.home or opts.get("home", "")
        st.locale = opts.get("WPLANG") or st.locale
        return
    st.active_plugins = None


def resolve(st: Run) -> None:
    a, root = st.args, st.root
    st.name = normalize_name(a.name or os.path.basename(os.path.abspath(root)))
    st.php = a.php or st.presets.default_php
    wp = (st.survey or {}).get("wordpress") or {}

    st.db_prefix = (
        detect.table_prefix(root)
        or wp.get("tablePrefix")
        or (dumpmod.detect_prefix(st.dump_path) if st.dump_path else "wp_")
    )
    st.locale = wp.get("locale") or "en_US"
    st.home = a.home_url or wp.get("home") or ""
    cfg = detect.config_values(root)
    st.home = st.home or cfg.get("WP_HOME", "")
    st.legacy_salts = {k: cfg.get(k, "") for k in detect.SALT_KEYS}

    resolve_active_set(st)

    st.wp_version = a.version or detect.wp_version(root) or wp.get("version", "")
    if not st.wp_version:
        ui.die("cannot tell the WordPress version (no wp-includes/version.php, no survey); pass --version", 5)
    floor = st.presets.minimum_wordpress
    if not at_least(st.wp_version, floor):
        ui.die(
            f"WordPress {st.wp_version} is below {floor}, the oldest wordpress-nix converts. "
            "Update the legacy site first, or pass --version to pin a newer core and accept the database upgrade.",
            5,
        )
    if not st.home:
        st.warn("no home URL (no --home-url, no survey or dump home): .github/wordpress-nix.json leaves it empty")


def guards(st: Run) -> None:
    a, root = st.args, st.root
    if detect.repo_state(root) == "nested":
        top = probe(git("rev-parse", "--show-toplevel", cwd=str(root))) or ""
        st.warn(f"this directory is inside another git repository ({top}); a site is its own repository")
        if not a.force:
            ui.die("re-run with --force to create a nested repository anyway", 8)
    find_records(st)
    if detect.is_multisite(root) or ((st.survey or {}).get("wordpress") or {}).get("multisite"):
        ui.die("this is a multisite install; wordpress-nix converts single sites", 6)
    if not st.survey and not st.dump_path and not a.keep_inactive:
        ui.die(
            "no survey or dump to tell what is active (looked for .wordpress-nix-backup/survey.json "
            "and dump.sql): run `wordpress-nix legacy pull`, pass --survey or --dump, or --keep-inactive "
            "to keep everything on disk",
            6,
        )


def cmd_migrate(st: Run) -> None:
    a = st.args
    st.mode = "migrate"
    guards(st)
    resolve(st)
    ui.step("Classifying wp-content")
    classify.classify(st, wporg.load())
    print_plan(st)

    if a.dry_run:
        ui.out("\n--dry-run: nothing was changed.")
        return
    if st.blocked:
        ui.die(f"{len(st.blocked)} item(s) are blocked (above); decide them and rerun", 1)
    if not a.assume_yes and not ui.confirm("Convert this site to wordpress-nix?"):
        ui.die("aborted. Re-run with --yes (or --dry-run to inspect the plan).", 6)
    if detect.has_foreign_flake(st.root) and not a.force:
        ui.die(
            "flake.nix exists and is not a wordpress-nix flake. Re-run with --force: the site flake "
            "will be written to flake.nix.wordpress-nix for you to merge.",
            4,
        )
    run_pipeline(st)


def cmd_resume(st: Run) -> None:
    """A conversion interrupted after the template landed: wp-content is still there
    and plan.json says what to do with it."""
    st.mode = "migrate"
    st.resumed = True
    st.php = st.args.php or st.presets.default_php
    load_plan(st)
    ui.info("resuming the conversion recorded in .wordpress-nix-backup/plan.json")
    if st.args.dry_run:
        print_plan(st)
        ui.out("\n--dry-run: nothing was changed.")
        return
    run_pipeline(st)


def _flake_value(text: str, key: str) -> str:
    import re

    m = re.search(rf'\b{key}\s*=\s*"([^"]*)"', text)
    return m.group(1) if m else ""


def cmd_reconcile(st: Run) -> None:
    """A site that already is one: add what is missing, rewrite the managed block, and
    change nothing else (a no-op when nothing drifted)."""
    st.mode = "reconcile"
    ui.info("already a wordpress-nix site — reconciling")
    flake = detect.read(st.root / "flake.nix")
    site = {}
    try:
        site = json.loads((st.root / ".github" / "wordpress-nix.json").read_text())
    except (OSError, ValueError):
        pass
    st.name = normalize_name(st.args.name or site.get("name") or _flake_value(flake, "name") or st.root.resolve().name)
    php = st.args.php or site.get("php") or _flake_value(flake, "php")
    st.php = php if php in st.presets.php else st.presets.default_php
    st.home = st.args.home_url or site.get("home", "")
    st.db_prefix = site.get("dbPrefix", "wp_")
    st.active_theme = site.get("activeTheme") or ""
    st.production_only = site.get("productionOnlyPlugins", [])
    st.development_only = site.get("developmentOnlyPlugins", [])
    st.woocommerce = bool(site.get("woocommerce"))
    if st.args.dry_run:
        ui.out("\n--dry-run: nothing was changed.")
        return
    run_pipeline(st)
