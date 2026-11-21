from standards_core.ci import verdict as core_verdict

from wordpress_nix_tools import verdict
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


def after(**kw):
    base = dict(group="", update_type="version-update:semver-patch", names=[], woocommerce=False, security=False)
    base.update(kw)
    return verdict.composer_after_relock(**base)


def test_after_relock_rows():
    assert after(group="wordpress-core", names=["roots/wordpress"]) == "auto"
    assert (
        after(group="wordpress-core", update_type="version-update:semver-minor", names=["roots/wordpress"]) == "review"
    )
    assert after(group="wp-plugins", update_type="version-update:semver-minor", names=["wp-plugin/x"]) == "auto"
    assert (
        after(
            group="wp-plugins",
            update_type="version-update:semver-minor",
            names=["wp-plugin/woocommerce"],
            woocommerce=True,
        )
        == "review"
    )
    assert after(group="php-libs", update_type="version-update:semver-minor", names=["guzzlehttp/guzzle"]) == "auto"
    assert after(names=["avunu/wp-cloud-files"]) == "auto"
    assert after(update_type="version-update:semver-major", group="wp-plugins") == "review"
    assert after(names=["vendor/new"], update_type="version-update:semver-minor") == "review"
    assert after(names=["vendor/new"], update_type="version-update:semver-minor", security=True) == "auto"
