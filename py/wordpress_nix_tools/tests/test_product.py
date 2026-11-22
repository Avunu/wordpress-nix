"""The product against standards-core's contract, its decision table (moonshot v3
§6.4), and the managed files of a site."""

from __future__ import annotations

import json
from pathlib import Path

from standards_core.ci import verdict as core_verdict
from standards_core.testing import conformance

from conftest import commit, git
from wordpress_nix_tools import cli
from wordpress_nix_tools.product import PRODUCT, dependabot_time

TABLE = """[tool.wordpress-nix]
schema = 1
profile = "deployment"

[tool.wordpress-nix.site]
home-url = "https://fixture.example"
"""


def test_conforms():
    assert conformance.check(PRODUCT) == []


def test_the_gates_and_the_relock_hooks():
    assert PRODUCT.ci.gates == ("flake", "smoke", "audit")
    assert PRODUCT.ci.relock_patch_paths == ("nix/vendor-hash",)
    assert PRODUCT.ci.verify_relock is not None


def test_product_wiring():
    assert PRODUCT.identity.env_prefix == "WORDPRESS_NIX"
    assert core_verdict.problems(PRODUCT) == []


def decide(eco, update="", group="", names=""):
    return core_verdict.decide(PRODUCT, eco, update, group, names, engine_repo="Avunu/wordpress")


def test_decision_table():
    """Every Composer PR is relocked; verify-relock gives the §6.4 rows after it."""
    for update, group, names in (
        ("version-update:semver-patch", "wp-plugins", "wp-plugin/x"),
        ("version-update:semver-minor", "wordpress-core", "roots/wordpress"),
        ("version-update:semver-major", "", "wp-plugin/x"),
        ("version-update:semver-minor", "php-libs", "vlucas/phpdotenv"),
    ):
        assert decide("composer", update, group, names) == "relock", (update, group, names)
    assert (
        decide("github_actions", "version-update:semver-minor", "", "Avunu/wordpress/.github/workflows/site-ci.yml")
        == "close"
    )
    assert decide("github_actions", "version-update:semver-minor", "", "actions/checkout") == "auto"


def test_dependabot_time_is_stable_and_early():
    t = dependabot_time("example-site")
    assert t == dependabot_time("example-site")
    assert "02:00" <= t <= "05:59"


def _site(root: Path, lock: dict) -> None:
    git(root, "remote", "add", "origin", "https://github.com/Avunu/site-fixture.git")
    commit(
        root,
        {
            "wordpress-nix.toml": TABLE,
            "composer.json": '{"name": "avunu/site-fixture"}\n',
            "composer.lock": json.dumps(lock),
        },
    )


def test_sync_writes_the_site_files_and_checks_clean(site, bedrock_lock, monkeypatch):
    _site(site, bedrock_lock)
    monkeypatch.chdir(site)
    assert cli.main(["sync", "--write", "--offline"]) == 0
    for rel in (
        ".github/dependabot.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/deps.yml",
        ".github/workflows/audit.yml",
        ".github/wordpress-nix.json",
        ".editorconfig",
        ".pre-commit-config.yaml",
        "committed.toml",
        ".gitignore",
    ):
        assert (site / rel).is_file(), rel
    ignored = (site / ".gitignore").read_text().splitlines()
    assert ignored[0] == "# >>> wordpress-nix: site (managed) >>>"
    assert ignored[-1] == "# <<< wordpress-nix: site (managed) <<<"
    for line in ("auth.json", ".env", "*.sql", "*.sql.gz", "/vendor", "web/wp", "web/app/uploads/*"):
        assert line in ignored, line
    facts = json.loads((site / ".github/wordpress-nix.json").read_text())
    assert facts == {
        "slug": "fixture",
        "woocommerce": False,
        "homeUrl": "https://fixture.example",
        "dropPlugins": [],
        "localUploadPrefixes": ["cwicly", "fonts", "wpcodebox", "fluentform"],
    }
    ci = (site / ".github/workflows/ci.yml").read_text()
    assert "site-ci.yml@" in ci and "smoke-runs-on" in ci
    commit(site, {})
    assert cli.main(["sync", "--check", "--offline"]) == 0


def test_the_gitignore_block_goes_after_the_sites_own_lines(site, bedrock_lock, monkeypatch):
    _site(site, bedrock_lock)
    own = "web/app/plugins/*\n# BEGIN wordpress-nix\n!web/app/plugins/premium\n# END wordpress-nix\n"
    commit(site, {".gitignore": own})
    monkeypatch.chdir(site)
    assert cli.main(["sync", "--check", "--offline"]) == 1
    assert cli.main(["sync", "--write", "--offline"]) == 0
    text = (site / ".gitignore").read_text()
    assert text.startswith(own)
    assert "web/app/plugins/*" not in text[len(own) :]
    commit(site, {})
    assert cli.main(["sync", "--check", "--offline"]) == 0


def test_woocommerce_comes_from_the_lock(site, bedrock_lock, monkeypatch):
    bedrock_lock["packages"].append({"name": "wp-plugin/woocommerce", "version": "9.0.0"})
    _site(site, bedrock_lock)
    monkeypatch.chdir(site)
    assert cli.main(["sync", "--write", "--offline"]) == 0
    assert json.loads((site / ".github/wordpress-nix.json").read_text())["woocommerce"]

