"""wordpress-nix's product: every value standards-core reads (core DESIGN.md §3).

A checkout is a **site repository** (``Avunu/site-<slug>``): stock Bedrock, a thin
flake on ``wordpress-nix.site``, deployed by the cluster from ``main``. It is a
*deployment project*: the built-in ``deployment@1.0`` profile (the ``wordpress-site``
standards profile of moonshot v3 §10.1) turns on everything but releases.

Its table is ``[tool.wordpress-nix]`` in ``wordpress-nix.toml`` at the root (a Bedrock
tree has no ``pyproject.toml``). The managed files (``standards/manifest.d``, the
package's data root; ``data/`` holds the conversion's tables):

- ``.github/dependabot.yml`` (dependabot): Composer daily in four groups, at a time
  hashed from the slug, with the cooldowns of §5.4; github-actions weekly;
- ``.github/workflows/{ci,deps,audit}.yml`` (ci): thin callers of wordpress-nix's
  ``site-ci.yml``, ``site-deps.yml`` and ``site-audit.yml``, pinned to the revision
  ``flake.lock`` locks wordpress-nix at;
- ``.github/wordpress-nix.json`` (site): the facts the workflows, the decision table,
  ``site import`` and ``db rewrite-urls`` read without Python;
- ``.editorconfig`` (editorconfig), ``.pre-commit-config.yaml`` (lint) and
  ``committed.toml`` (commits).

The required checks are the gates: ``ci / flake``, ``ci / smoke`` and ``ci / audit``.
A Dependabot Composer PR is relocked (``nix/vendor-hash`` is the one path a relock
pushes) and checked by ``lockcheck.verify_relock`` before it can merge.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

from standards_core.product import (
    CiConfig,
    Engine,
    Fixture,
    HeaderKind,
    Hooks,
    Identity,
    JsonStyle,
    Markers,
    Modules,
    Paths,
    Pins,
    Product,
)
from standards_core.scaffold.context import Checkout, default_load_checkout

from wordpress_nix_tools import __version__, lockcheck, verdict

DATA = Path(__file__).resolve().parent / "standards"
DOCS = "https://github.com/Avunu/wordpress/tree/main/docs"
CONFIG = "wordpress-nix.toml"

OPT_IN_HINT = (
    "this site repository has not opted in to wordpress-nix's standards: add a"
    ' [tool.wordpress-nix] table (schema = 1, profile = "deployment") to'
    f" {CONFIG}, or run `nix run github:Avunu/wordpress -- --sync --standards deployment`"
)

MODULES = (
    "dev-shell",
    "editorconfig",
    "lint",
    "commits",
    "releases",
    "dependabot",
    "ci",
    "site",
    "repo-policy",
)

# The site's own facts (.github/wordpress-nix.json): what composer.lock says, unless
# the table says otherwise.
WOOCOMMERCE = ("wp-plugin/woocommerce",)


def _table(checkout: Checkout) -> Mapping[str, Any]:
    tool = checkout.config.get("tool", {})
    table = tool.get("wordpress-nix") if isinstance(tool, dict) else None
    return table if isinstance(table, dict) else {}


def slug(name: str) -> str:
    """The site's slug: the repository's name without ``site-``."""
    return name.removeprefix("site-")


def _locked(checkout: Checkout) -> set[str]:
    if "composer.lock" not in checkout.tracked:
        return set()
    try:
        doc = json.loads((checkout.root / "composer.lock").read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        str(p.get("name", "")).lower()
        for key in ("packages", "packages-dev")
        for p in doc.get(key) or []
        if isinstance(p, dict)
    }


def load_checkout(root: Path) -> Checkout:
    return default_load_checkout(PRODUCT, root)


def discover(
    checkout: Checkout, cfg: Mapping[str, Any], modules: Mapping[str, bool]
) -> dict[str, Any]:
    locked = _locked(checkout)
    return {
        "slug": slug(checkout.name),
        "woocommerce": any(name in locked for name in WOOCOMMERCE),
        "composer": "composer.json" in checkout.tracked,
    }


def gates(
    modules: Mapping[str, bool], cfg: Mapping[str, Any], facts: Mapping[str, Any]
) -> list[str]:
    """Every gate is on with ``ci``: a site has no gate it may skip."""
    return list(PRODUCT.ci.gates) if modules.get("ci") else []


def dependabot_time(name: str) -> str:
    """Composer's daily run, spread over the fleet: a minute between 02:00 and 05:59
    (America/New_York), hashed from the slug, so ~40 sites never relock at once."""
    minute = int(hashlib.sha256(name.encode()).hexdigest()[:8], 16) % 240 + 120
    return f"{minute // 60:02d}:{minute % 60:02d}"


def template_globals(checkout: Checkout, resolved: Any, ctx: Any) -> dict[str, Any]:
    facts = ctx["discover"]
    site = resolved.cfg.get("site", {})
    woo = site.get("woocommerce")
    return {
        "slug": facts["slug"],
        "dependabot_time": dependabot_time(facts["slug"]),
        "woocommerce": facts["woocommerce"] if woo is None else bool(woo),
    }


# --- the offline fixture: the template's site, as `nix flake init` lays it down -----

TEMPLATE_TABLE = """
[site]
home-url = "https://example.org"
"""

FIXTURES = MappingProxyType(
    {
        "site": Fixture(
            table=TEMPLATE_TABLE,
            files=MappingProxyType(
                {
                    "composer.json": '{"name": "avunu/site-fixture", "require": {}}\n',
                    "composer.lock": '{"packages": [], "packages-dev": []}\n',
                    "nix/vendor-hash": lockcheck.FAKE_HASH + "\n",
                }
            ),
        ),
    }
)


def write_fixture(root: Path, name: str, fixture: Fixture, profile: str) -> None:
    for rel, text in fixture.files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    (root / CONFIG).write_text(
        f'[tool.wordpress-nix]\nschema = 1\nprofile = "{profile}"\n'
        + "".join(
            line.replace("[site]", "[tool.wordpress-nix.site]") + "\n"
            for line in fixture.table.strip().splitlines()
        )
    )


PRODUCT = Product(
    identity=Identity(
        name="wordpress-nix",
        cli="wordpress-nix",
        distribution="wordpress-nix-tools",
        version=__version__,
        table="wordpress-nix",
        env_prefix="WORDPRESS_NIX",
        report_key="wordpress_nix",
        sync_command="wordpress-init --sync",
        opt_in_hint=OPT_IN_HINT,
        docs_url=DOCS,
        config_file=CONFIG,
        config_required=False,
        description=(
            "wordpress-nix's conversion commands, and the standards tools for WordPress"
            " site repositories: managed files, the Dependabot pipeline's steps, the"
            f" vulnerability audit. See {DOCS}."
        ),
    ),
    markers=Markers(
        managed="wordpress-nix:managed",
        header="wordpress-nix:managed — generated by wordpress-nix (`wordpress-init --sync`);",
        header_local="edit only inside the wordpress-nix:local region.",
        local="wordpress-nix:local",
        block_begin=">>> wordpress-nix: {name} (managed) >>>",
        block_end="<<< wordpress-nix: {name} (managed) <<<",
        html_begin="<!-- wordpress-nix:begin {name} -->",
        html_end="<!-- wordpress-nix:end {name} -->",
    ),
    engine=Engine(
        flake_input="wordpress-nix",
        repo="Avunu/wordpress",
        release_branch="main",
        url_key="dev-shell.wordpress-nix-url",
        install_spec=(
            "wordpress-nix-tools @ git+https://github.com/{owner}/{repo}@{rev}"
            "#subdirectory=py/wordpress_nix_tools"
        ),
        caller_stem="site-*",
        init_app="wordpress-init",
        tool_python="3.13",
    ),
    pins=Pins(
        dir=".wordpress-nix/pins",
        managed_inputs=lambda ctx: ("wordpress-nix",),
    ),
    modules=Modules(
        names=MODULES,
        needs=MappingProxyType({"repo-policy": ("ci",), "ci": ("site",)}),
        github_only=("dependabot", "ci", "repo-policy", "releases"),
    ),
    data_root=DATA,
    hooks=Hooks(
        load_checkout=load_checkout,
        discover=discover,
        gates=gates,
        template_globals=template_globals,
        # A site deploys from main; it has no release flow, so no branching names it.
        integration_default=lambda cfg: "main",
        semantic_compare=lambda ctx: True,
        write_fixture=write_fixture,
    ),
    ci=CiConfig(
        gates=("flake", "smoke", "audit"),
        check_context="ci / {gate}",
        contexts=(
            ("ci / flake", "ci.yml"),
            ("ci / smoke", "ci.yml"),
            ("ci / audit", "ci.yml"),
        ),
        labels=MappingProxyType(
            {
                "dependencies": (
                    "0366d6",
                    "Pull requests that update a dependency",
                    "dependabot",
                ),
                "needs-review": (
                    "d93f0b",
                    "A dependency update the decision table leaves to a person",
                    "dependabot",
                ),
                "security": (
                    "b60205",
                    "A vulnerable version, or a vendor hash that changed under an unchanged lock",
                    "ci",
                ),
            }
        ),
        decision_table=verdict.DECISION_TABLE,
        ci_workflow="ci.yml",
        nightly_workflow="audit.yml",
        relock_patch_paths=(lockcheck.VENDOR_HASH,),
        verify_relock=lockcheck.verify_relock,
    ),
    paths=Paths(
        never_write=frozenset({"vendor", "web/wp", ".devenv", ".direnv", "node_modules"}),
        not_source=("vendor", "web/wp", ".devenv", ".direnv", "node_modules", "nix"),
        github_marks=(".github",),
    ),
    header_kinds=MappingProxyType({"md": HeaderKind("<!--", "-->")}),
    json_style=JsonStyle(indent="  ", tab_width=2, width=80, trailing_commas="none"),
    fixtures=FIXTURES,
    self_repo="Avunu/wordpress",
)
