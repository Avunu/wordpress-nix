import json

from wordpress_nix_tools import audit

FEED = {
    "v1": {
        "id": "v1",
        "title": "Akismet XSS",
        "cvss": {"score": 6.1, "rating": "Medium"},
        "software": [
            {
                "type": "plugin",
                "slug": "akismet",
                "affected_versions": {
                    "* - 5.3": {"from_version": "*", "from_inclusive": True, "to_version": "5.3", "to_inclusive": True}
                },
                "patched_versions": ["5.3.1"],
            }
        ],
    },
    "v2": {
        "id": "v2",
        "title": "Core thing",
        "software": [
            {
                "type": "core",
                "slug": "wordpress",
                "affected_versions": {
                    "6.0 - 6.8.2": {
                        "from_version": "6.0",
                        "from_inclusive": True,
                        "to_version": "6.8.3",
                        "to_inclusive": False,
                    }
                },
            }
        ],
    },
    "v3": {
        "id": "v3",
        "title": "Premium SQLi",
        "software": [
            {
                "type": "plugin",
                "slug": "premium",
                "affected_versions": {
                    "<2": {"from_version": "*", "from_inclusive": True, "to_version": "2.0", "to_inclusive": False}
                },
            }
        ],
    },
}


def lock(*pkgs):
    return json.dumps({"packages": [{"name": n, "version": v} for n, v in pkgs]})


def test_ranges():
    r = {"from_version": "1.0", "from_inclusive": False, "to_version": "2.0", "to_inclusive": True}
    assert not audit.in_range("1.0", r)
    assert audit.in_range("1.0.1", r)
    assert audit.in_range("2.0", r)
    assert not audit.in_range("2.0.1", r)
    assert audit.in_range("0.1", {"from_version": "*", "to_version": "*"})


def test_lock_and_tree(tmp_path):
    p = tmp_path / "web/app/plugins/premium"
    p.mkdir(parents=True)
    (p / "premium.php").write_text("<?php\n/*\n * Plugin Name: Premium\n * Version: 1.9\n */")
    text = lock(("wp-plugin/akismet", "5.3"), ("roots/wordpress-no-content", "6.8.2"), ("roots/wordpress", "6.8.2"))
    introduced, existing = audit.audit(tmp_path, text, FEED, None)
    assert sorted(f.id for f in introduced) == ["v1", "v2", "v3"]
    assert existing == []


def test_only_new_findings_fail(tmp_path):
    base = lock(("wp-plugin/akismet", "5.3"))
    head = lock(("wp-plugin/akismet", "5.3"), ("roots/wordpress-no-content", "6.8.0"))
    introduced, existing = audit.audit(tmp_path, head, FEED, base)
    assert [f.id for f in introduced] == ["v2"]
    assert [f.id for f in existing] == ["v1"]
    fixed = lock(("wp-plugin/akismet", "5.3.1"))
    assert audit.audit(tmp_path, fixed, FEED, base) == ([], [])
