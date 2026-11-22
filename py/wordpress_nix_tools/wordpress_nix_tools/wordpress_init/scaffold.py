"""Init mode: a new Bedrock site in an empty directory (or with --force, a
non-empty one, never overwriting a file)."""

import os

from wordpress_nix_tools.common import ui
from wordpress_nix_tools.wordpress_init import composer_json
from wordpress_nix_tools.wordpress_init.pipeline import run_pipeline
from wordpress_nix_tools.wordpress_init.state import Run, normalize_name


def cmd_init(st: Run) -> None:
    a = st.args
    st.mode = "init"
    st.name = normalize_name(a.name or os.path.basename(os.path.abspath(st.root)))
    st.php = a.php or st.presets.default_php
    st.home = a.home_url or f"https://{st.name}.localhost"
    st.wp_version = a.version
    st.active_theme = "twentytwentyfive"
    ui.step(f"New site {st.name}")
    ui.info(f"PHP  : {st.preset.label}")
    ui.info(f"home : {st.home}")
    if a.dry_run:
        ui.out("\n--dry-run: nothing was changed.")
        return
    if not st.wp_version:
        # The template's core pin.
        from pathlib import Path

        tpl = composer_json.load(Path(st.config.template) / "composer.json")
        st.wp_version = (tpl.get("require") or {}).get("roots/wordpress", "")
    run_pipeline(st)
