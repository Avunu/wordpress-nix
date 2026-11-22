import copy
import json
import subprocess

from wordpress_nix_tools import lockcheck

HASH = "sha256-GkUt6L2dNIOCtd5w/m+a3Tcy/F3qhAuOBvJNnHxKZLE="


def lock_text(repo):
    return (repo / "templates/bedrock-site/composer.lock").read_text()


def bump(text, name, version, url=None):
    doc = json.loads(text)
    for p in doc["packages"]:
        if p["name"] == name:
            p["version"] = version
            if url is not None:
                p["dist"]["url"] = url
    return json.dumps(doc)


def test_real_bedrock_lock_is_ok_from_nothing(repo):
    """Every entry of the stock Bedrock lock names its package and version."""
    v = lockcheck.check(None, lock_text(repo), HASH)
    assert v.decision() == "ok", v.reasons()


def test_real_wp_cloud_files_lock_is_ok(fixtures):
    """A Packagist-heavy lock (aws, guzzle, dompdf, phpoffice): GitHub zipballs at
    the source reference."""
    text = (fixtures / "wp-cloud-files.composer.lock").read_text()
    assert lockcheck.check(None, text, HASH).decision() == "ok"


def test_unchanged_is_ok(repo):
    t = lock_text(repo)
    assert lockcheck.check(t, t, HASH).decision() == "ok"


def test_plugin_bump_ok_and_foreign_host_rejected(repo):
    base = lock_text(repo)
    good = bump(base, "wp-plugin/two-factor", "0.11.0", "https://downloads.wordpress.org/plugin/two-factor.0.11.0.zip")
    assert lockcheck.check(base, good, HASH).decision() == "ok"
    bad = bump(base, "wp-plugin/two-factor", "0.11.0", "https://evil.example/two-factor.0.11.0.zip")
    v = lockcheck.check(base, bad, HASH)
    assert v.decision() == "reject"
    wrong_version = bump(
        base, "wp-plugin/two-factor", "0.11.0", "https://downloads.wordpress.org/plugin/two-factor.0.10.0.zip"
    )
    assert lockcheck.check(base, wrong_version, HASH).decision() == "reject"


def test_owner_mismatch_rejected(repo):
    base = lock_text(repo)
    doc = json.loads(base)
    for p in doc["packages"]:
        if p["name"] == "oscarotero/env":
            p["dist"]["url"] = p["dist"]["url"].replace("oscarotero/env", "attacker/env")
    assert lockcheck.check(base, json.dumps(doc), HASH).decision() == "reject"


def test_new_package_needs_review(repo):
    base = lock_text(repo)
    doc = json.loads(base)
    extra = copy.deepcopy(next(p for p in doc["packages"] if p["name"] == "wp-plugin/two-factor"))
    extra["name"] = "wp-plugin/akismet"
    extra["version"] = "5.3"
    extra["dist"]["url"] = "https://downloads.wordpress.org/plugin/akismet.5.3.zip"
    doc["packages"].append(extra)
    v = lockcheck.check(base, json.dumps(doc), HASH)
    assert v.decision() == "needs-review", v.reasons()


def test_core_release_url(repo):
    base = lock_text(repo)
    good = bump(
        base,
        "roots/wordpress-no-content",
        "7.1.6",
        "https://downloads.wordpress.org/release/wordpress-7.1.6-no-content.zip",
    )
    assert lockcheck.check(base, good, HASH).decision() == "ok"
    bad = bump(
        base, "roots/wordpress-no-content", "7.1.6", "https://downloads.w.org/release/wordpress-7.1.5-no-content.zip"
    )
    assert lockcheck.check(base, bad, HASH).decision() == "reject"


def test_vendor_hash_rules(repo):
    t = lock_text(repo)
    assert lockcheck.check(t, t, lockcheck.FAKE_HASH).decision() == "reject"
    assert lockcheck.check(t, t, "nonsense\n").decision() == "reject"
    assert lockcheck.check(t, None, HASH).decision() == "reject"


def test_verify_relock_reads_git(tmp_path, repo):
    def git(*a):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@e")
    git("config", "user.name", "t")
    (tmp_path / "nix").mkdir()
    (tmp_path / "composer.lock").write_text(lock_text(repo))
    (tmp_path / "nix/vendor-hash").write_text(HASH + "\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "composer.lock").write_text(
        bump(lock_text(repo), "wp-plugin/two-factor", "0.11.0", "https://evil.example/x.zip")
    )
    git("commit", "-qam", "head")
    head = git("rev-parse", "HEAD")
    assert lockcheck.verify_relock(tmp_path, base, head).decision() == "reject"


def core(text, version):
    return bump(
        text,
        "roots/wordpress-no-content",
        version,
        f"https://downloads.wordpress.org/release/wordpress-{version}-no-content.zip",
    )


def test_core_patch_auto_minor_and_major_review(repo):
    base = core(lock_text(repo), "7.1.5")
    assert lockcheck.check(base, core(base, "7.1.6"), HASH).decision() == "ok"
    v = lockcheck.check(base, core(base, "7.2.0"), HASH)
    assert v.decision() == "needs-review" and "core minor" in v.reasons()[0], v.reasons()
    assert lockcheck.check(base, core(base, "8.0.0"), HASH).decision() == "needs-review"


def test_any_major_needs_review(repo):
    base = lock_text(repo)
    doc = json.loads(base)
    env = next(p for p in doc["packages"] if p["name"] == "oscarotero/env")
    major = str(int(env["version"].lstrip("v").split(".")[0]) + 1) + ".0.0"
    v = lockcheck.check(base, bump(base, "oscarotero/env", major), HASH)
    assert v.decision() == "needs-review" and "a major update" in v.reasons()[0], v.reasons()
    minor = bump(base, "wp-plugin/two-factor", "0.11.0", "https://downloads.wordpress.org/plugin/two-factor.0.11.0.zip")
    assert lockcheck.check(base, minor, HASH).decision() == "ok"
    plugin_major = bump(
        base, "wp-plugin/two-factor", "1.0.0", "https://downloads.wordpress.org/plugin/two-factor.1.0.0.zip"
    )
    assert lockcheck.check(base, plugin_major, HASH).decision() == "needs-review"


def with_woo(text, version):
    doc = json.loads(text)
    woo = copy.deepcopy(next(p for p in doc["packages"] if p["name"] == "wp-plugin/two-factor"))
    woo.update(name="wp-plugin/woocommerce", version=version)
    woo["dist"]["url"] = f"https://downloads.wordpress.org/plugin/woocommerce.{version}.zip"
    doc["packages"].append(woo)
    return json.dumps(doc)


def test_woo_minor_needs_review_on_a_woo_site(repo):
    base, head = with_woo(lock_text(repo), "10.2.1"), with_woo(lock_text(repo), "10.3.0")
    assert lockcheck.check(base, head, HASH).decision() == "ok"
    v = lockcheck.check(base, head, HASH, woocommerce=True)
    assert v.decision() == "needs-review" and "WooCommerce" in v.reasons()[0], v.reasons()
    patch = with_woo(lock_text(repo), "10.2.2")
    assert lockcheck.check(base, patch, HASH, woocommerce=True).decision() == "ok"


def test_update_type():
    assert lockcheck.update_type("7.1.5", "7.1.6") == "patch"
    assert lockcheck.update_type("7.1", "7.2.0") == "minor"
    assert lockcheck.update_type("v1.9.0", "v2.0.0") == "major"
    assert lockcheck.update_type("dev-main", "dev-main") == "other"
    assert lockcheck.update_type("dev-main", "1.0.0") == "major"


def test_verify_relock_reads_woocommerce_from_the_head(tmp_path, repo):
    def git(*a):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@e")
    git("config", "user.name", "t")
    (tmp_path / "nix").mkdir()
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/wordpress-nix.json").write_text('{"woocommerce": true}')
    (tmp_path / "composer.lock").write_text(with_woo(lock_text(repo), "10.2.1"))
    (tmp_path / "nix/vendor-hash").write_text(HASH + "\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "composer.lock").write_text(with_woo(lock_text(repo), "10.3.0"))
    git("commit", "-qam", "head")
    assert lockcheck.verify_relock(tmp_path, base, git("rev-parse", "HEAD")).decision() == "needs-review"
