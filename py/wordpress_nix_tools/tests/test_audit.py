import argparse
import json
import subprocess

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


def premium(root, version):
    p = root / "web/app/plugins/premium"
    p.mkdir(parents=True, exist_ok=True)
    (p / "premium.php").write_text(f"<?php\n/*\n * Plugin Name: Premium\n * Version: {version}\n */")


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(root, msg):
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-qm", msg)
    return git(root, "rev-parse", "HEAD")


def test_committed_code_is_compared_with_the_base_commit(tmp_path):
    git(tmp_path, "init", "-q")
    text = lock(("wp-plugin/akismet", "5.3"))
    (tmp_path / "composer.lock").write_text(text)
    premium(tmp_path, "2.1")
    base = commit(tmp_path, "base")
    # The PR downgrades the committed plugin into the vulnerable range.
    premium(tmp_path, "1.9")
    commit(tmp_path, "head")
    introduced, existing = audit.audit(tmp_path, text, FEED, text, base)
    assert [f.id for f in introduced] == ["v3"]
    assert [f.id for f in existing] == ["v1"]
    # A lock-only base never counts the head's committed code as pre-existing.
    introduced, _ = audit.audit(tmp_path, text, FEED, text)
    assert [f.id for f in introduced] == ["v3"]
    # Vulnerable at the base too: reported, not failed.
    vulnerable_base = git(tmp_path, "rev-parse", "HEAD")
    introduced, existing = audit.audit(tmp_path, text, FEED, text, vulnerable_base)
    assert introduced == [] and sorted(f.id for f in existing) == ["v1", "v3"]
    assert audit.from_git(tmp_path, base) == [audit.Software("plugin", "premium", "2.1", "web/app/plugins/premium")]


def test_cli_reads_the_base_lock_from_the_base_rev(tmp_path, capsys):
    git(tmp_path, "init", "-q")
    (tmp_path / "composer.lock").write_text(lock(("wp-plugin/akismet", "5.3")))
    base = commit(tmp_path, "base")
    (tmp_path / "composer.lock").write_text(lock(("wp-plugin/akismet", "5.3"), ("roots/wordpress-no-content", "6.8.0")))
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps(FEED))
    p = argparse.ArgumentParser()
    audit.add_arguments(p)
    rc = audit.run(p.parse_args(["--root", str(tmp_path), "--base-rev", base, "--feed", str(feed)]))
    assert rc == 1
    assert "1 introduced, 1 pre-existing" in capsys.readouterr().out
    assert audit.run(p.parse_args(["--root", str(tmp_path), "--base-rev", "nope", "--feed", str(feed)])) == 2
