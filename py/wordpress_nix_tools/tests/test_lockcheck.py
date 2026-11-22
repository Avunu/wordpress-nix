"""verify-relock's rule: the URL names the same package and version (lockcheck)."""

from __future__ import annotations

import copy
import json

from conftest import commit, git
from wordpress_nix_tools import lockcheck

HASH = "sha256-GkUt6L2dNIOCtd5w/m+a3Tcy/F3qhAuOBvJNnHxKZLE=\n"
HASH2 = "sha256-AbCt6L2dNIOCtd5w/m+a3Tcy/F3qhAuOBvJNnHxKZLE=\n"


def _pkg(lock: dict, name: str) -> dict:
    return next(p for p in lock["packages"] if p["name"] == name)


def _bump_plugin(lock: dict, version: str) -> dict:
    lock = copy.deepcopy(lock)
    p = _pkg(lock, "wp-plugin/two-factor")
    p["version"] = version
    p["dist"]["url"] = f"https://downloads.wordpress.org/plugin/two-factor.{version}.zip"
    p["source"]["reference"] = f"tags/{version}"
    return lock


def _run(site, base_lock: dict, head_lock: dict, head_hash: str = HASH2):
    base = commit(site, {"composer.lock": json.dumps(base_lock), "nix/vendor-hash": HASH})
    head = commit(
        site, {"composer.lock": json.dumps(head_lock), "nix/vendor-hash": head_hash}
    )
    return lockcheck.verify_relock(site, base, head)


def test_every_package_of_stock_bedrock_passes(bedrock_lock):
    assert lockcheck.check_all(json.dumps(bedrock_lock)) == []


def test_a_plugin_bump_from_wordpress_org_is_ok(site, bedrock_lock):
    res = _run(site, bedrock_lock, _bump_plugin(bedrock_lock, "0.11.0"))
    assert res.decision() == "ok", res.reasons()


def test_a_tree_is_accepted_as_head(site, bedrock_lock):
    base = commit(site, {"composer.lock": json.dumps(bedrock_lock), "nix/vendor-hash": HASH})
    commit(site, {"composer.lock": json.dumps(_bump_plugin(bedrock_lock, "0.11.0"))})
    tree = git(site, "write-tree")
    assert lockcheck.verify_relock(site, base, tree).decision() == "ok"


def test_a_tampered_host_is_rejected(site, bedrock_lock):
    head = _bump_plugin(bedrock_lock, "0.11.0")
    _pkg(head, "wp-plugin/two-factor")["dist"]["url"] = (
        "https://evil.example/two-factor.0.11.0.zip"
    )
    res = _run(site, bedrock_lock, head)
    assert res.decision() == "reject"
    assert "evil.example" in res.rejects[0]


def test_a_plugin_url_naming_another_version_is_rejected(site, bedrock_lock):
    head = _bump_plugin(bedrock_lock, "0.11.0")
    _pkg(head, "wp-plugin/two-factor")["dist"]["url"] = (
        "https://downloads.wordpress.org/plugin/two-factor.0.9.0.zip"
    )
    assert _run(site, bedrock_lock, head).decision() == "reject"


def test_a_plugin_url_naming_another_slug_is_rejected(site, bedrock_lock):
    head = _bump_plugin(bedrock_lock, "0.11.0")
    _pkg(head, "wp-plugin/two-factor")["dist"]["url"] = (
        "https://downloads.wordpress.org/plugin/wp-file-manager.0.11.0.zip"
    )
    assert _run(site, bedrock_lock, head).decision() == "reject"


def test_a_php_lib_owner_mismatch_is_rejected(site, bedrock_lock):
    head = copy.deepcopy(bedrock_lock)
    p = _pkg(head, "vlucas/phpdotenv")
    ref = "1" * 40
    p["version"] = "v5.6.9"
    p["source"]["reference"] = ref
    p["dist"]["reference"] = ref
    p["dist"]["url"] = f"https://api.github.com/repos/attacker/phpdotenv/zipball/{ref}"
    res = _run(site, bedrock_lock, head)
    assert res.decision() == "reject"
    assert "attacker/phpdotenv" in res.rejects[0]


def test_a_php_lib_bump_from_its_own_repository_is_ok(site, bedrock_lock):
    head = copy.deepcopy(bedrock_lock)
    p = _pkg(head, "vlucas/phpdotenv")
    ref = "2" * 40
    p["version"] = "v5.6.9"
    p["source"]["reference"] = ref
    p["dist"]["reference"] = ref
    p["dist"]["url"] = f"https://api.github.com/repos/phpdotenv/phpdotenv/zipball/{ref}"
    assert _run(site, bedrock_lock, head).decision() == "ok"


def test_a_dist_ref_that_is_not_the_source_ref_is_rejected(site, bedrock_lock):
    head = copy.deepcopy(bedrock_lock)
    p = _pkg(head, "vlucas/phpdotenv")
    p["dist"]["url"] = (
        "https://api.github.com/repos/phpdotenv/phpdotenv/zipball/" + "3" * 40
    )
    assert _run(site, bedrock_lock, head).decision() == "reject"


def test_a_php_lib_dist_on_another_host_than_its_github_source_is_rejected(
    site, bedrock_lock
):
    head = copy.deepcopy(bedrock_lock)
    p = _pkg(head, "vlucas/phpdotenv")
    ref = p["source"]["reference"]
    p["version"] = "v5.6.9"
    p["dist"]["url"] = f"https://evil.example/phpdotenv/{ref}.zip"
    res = _run(site, bedrock_lock, head)
    assert res.decision() == "reject"
    assert "evil.example" in res.rejects[0]


def _off_github(lock: dict, dist: dict) -> dict:
    lock = copy.deepcopy(lock)
    p = _pkg(lock, "vlucas/phpdotenv")
    ref = "4" * 40
    p["version"] = "v5.6.9"
    p["source"] = {
        "type": "git",
        "url": "https://gitlab.com/phpdotenv/phpdotenv.git",
        "reference": ref,
    }
    p["dist"] = {**dist, "reference": ref}
    return lock


def test_a_php_lib_from_another_host_is_reviewed_not_rejected(site, bedrock_lock):
    head = _off_github(
        bedrock_lock,
        {
            "type": "zip",
            "url": "https://gitlab.com/api/v4/projects/phpdotenv%2Fphpdotenv/repository/archive.zip?sha="
            + "4" * 40,
        },
    )
    res = _run(site, bedrock_lock, head)
    assert res.decision() == "needs-review", res.reasons()
    assert "gitlab.com" in res.reviews[0]


def test_a_path_dist_is_reviewed_not_rejected(site, bedrock_lock):
    head = _off_github(bedrock_lock, {"type": "path", "url": "../wp-cloud-files"})
    res = _run(site, bedrock_lock, head)
    assert res.decision() == "needs-review", res.reasons()
    assert "'path' dist" in res.reviews[0]


def test_a_wordpress_org_package_is_never_foreign(site, bedrock_lock):
    head = _bump_plugin(bedrock_lock, "0.11.0")
    _pkg(head, "wp-plugin/two-factor")["dist"]["type"] = "path"
    assert _run(site, bedrock_lock, head).decision() == "reject"


def test_core_needs_its_shasum(site, bedrock_lock):
    head = copy.deepcopy(bedrock_lock)
    p = _pkg(head, "roots/wordpress-no-content")
    p["version"] = "7.1.6"
    p["dist"]["url"] = "https://downloads.w.org/release/wordpress-7.1.6-no-content.zip"
    p["dist"]["shasum"] = ""
    assert _run(site, bedrock_lock, head).decision() == "reject"
    p["dist"]["shasum"] = "a" * 40
    assert _run(site, bedrock_lock, head).decision() == "ok"


def test_a_new_package_is_reviewed(site, bedrock_lock):
    head = copy.deepcopy(bedrock_lock)
    head["packages"].append(
        {
            "name": "wp-plugin/akismet",
            "version": "5.3",
            "dist": {
                "type": "zip",
                "url": "https://downloads.wordpress.org/plugin/akismet.5.3.zip",
            },
            "source": {
                "type": "svn",
                "url": "https://plugins.svn.wordpress.org/akismet/",
                "reference": "tags/5.3",
            },
        }
    )
    res = _run(site, bedrock_lock, head)
    assert res.decision() == "needs-review"
    assert "a new package" in res.reviews[0]


def test_a_fake_vendor_hash_is_rejected(site, bedrock_lock):
    res = _run(site, bedrock_lock, bedrock_lock, head_hash=lockcheck.FAKE_HASH + "\n")
    assert res.decision() == "reject"
    assert "lib.fakeHash" in res.rejects[0]


def test_every_package_of_a_packagist_heavy_lock_passes(fixtures):
    """wp-cloud-files' lock (aws, guzzle, dompdf, phpoffice): GitHub zipballs at the
    source reference."""
    text = (fixtures / "wp-cloud-files.composer.lock").read_text()
    assert lockcheck.check_all(text) == []


# --- the §6.4 rows the version change decides (update_review) ---------------------


def _core(lock: dict, version: str, host: str = "https://downloads.w.org") -> dict:
    lock = copy.deepcopy(lock)
    p = _pkg(lock, "roots/wordpress-no-content")
    p["version"] = version
    p["dist"]["url"] = f"{host}/release/wordpress-{version}-no-content.zip"
    return lock


def test_core_from_either_wordpress_org_host(site, bedrock_lock):
    head = _core(bedrock_lock, "7.1.6", "https://downloads.wordpress.org")
    assert _run(site, bedrock_lock, head).decision() == "ok"
    wrong = _core(bedrock_lock, "7.1.6")
    _pkg(wrong, "roots/wordpress-no-content")["dist"]["url"] = (
        "https://downloads.w.org/release/wordpress-7.1.5-no-content.zip"
    )
    assert _run(site, bedrock_lock, wrong).decision() == "reject"


def test_core_patch_is_ok_and_minor_and_major_are_reviewed(site, bedrock_lock):
    assert _run(site, bedrock_lock, _core(bedrock_lock, "7.1.6")).decision() == "ok"
    res = _run(site, bedrock_lock, _core(bedrock_lock, "7.2.0"))
    assert res.decision() == "needs-review"
    assert "core minor" in res.reviews[0], res.reasons()
    assert _run(site, bedrock_lock, _core(bedrock_lock, "8.0.0")).decision() == "needs-review"


def test_any_major_is_reviewed(site, bedrock_lock):
    head = _bump_plugin(bedrock_lock, "1.0.0")
    res = _run(site, bedrock_lock, head)
    assert res.decision() == "needs-review"
    assert "a major update" in res.reviews[0], res.reasons()


def _with_woo(lock: dict, version: str) -> dict:
    lock = copy.deepcopy(lock)
    woo = copy.deepcopy(_pkg(lock, "wp-plugin/two-factor"))
    woo.update(name="wp-plugin/woocommerce", version=version)
    woo["dist"]["url"] = f"https://downloads.wordpress.org/plugin/woocommerce.{version}.zip"
    woo["source"].update(
        url="https://plugins.svn.wordpress.org/woocommerce/", reference=f"tags/{version}"
    )
    lock["packages"].append(woo)
    return lock


def test_a_woocommerce_minor_is_reviewed_on_a_woocommerce_site_only(
    site, bedrock_lock
):
    base, minor = _with_woo(bedrock_lock, "10.2.1"), _with_woo(bedrock_lock, "10.3.0")
    assert lockcheck.compare(
        lockcheck.packages(json.dumps(base), "base"),
        lockcheck.packages(json.dumps(minor), "head"),
    ).decision() == "ok"
    commit(site, {".github/wordpress-nix.json": '{"woocommerce": true}\n'})
    res = _run(site, base, minor)
    assert res.decision() == "needs-review"
    assert "WooCommerce" in res.reviews[0], res.reasons()
    assert _run(site, base, _with_woo(bedrock_lock, "10.2.2")).decision() == "ok"


def test_update_type():
    assert lockcheck.update_type("7.1.5", "7.1.6") == "patch"
    assert lockcheck.update_type("7.1", "7.2.0") == "minor"
    assert lockcheck.update_type("v1.9.0", "v2.0.0") == "major"
    assert lockcheck.update_type("dev-main", "dev-main") == "other"
    assert lockcheck.update_type("dev-main", "1.0.0") == "major"
