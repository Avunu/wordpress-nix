from standards_core.ci import verdict as core_verdict

from wordpress_nix_tools.product import PRODUCT


def test_product_wiring():
    assert PRODUCT.ci.relock_patch_paths == ("nix/vendor-hash",)
    assert PRODUCT.ci.verify_relock is not None
    assert core_verdict.problems(PRODUCT) == []
    assert PRODUCT.identity.env_prefix == "WORDPRESS_NIX"


def decide(eco, update="", group="", names=""):
    return core_verdict.decide(PRODUCT, eco, update, group, names, engine_repo="Avunu/wordpress")


def test_decision_table():
    assert decide("composer", "version-update:semver-patch", "wp-plugins", "wp-plugin/x") == "relock"
    assert (
        decide("github_actions", "version-update:semver-minor", "", "Avunu/wordpress/.github/workflows/site-ci.yml")
        == "close"
    )
    assert decide("github_actions", "version-update:semver-minor", "", "actions/checkout") == "auto"
