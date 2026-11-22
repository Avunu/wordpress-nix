import importlib.util
import io
import shutil
import subprocess
import sys

import pytest

from wordpress_nix_tools.db import dump
from wordpress_nix_tools.legacy import model

DUMP = r"""CREATE TABLE `wpx_options` (`option_id` bigint);
INSERT INTO `wpx_options` VALUES (1,'siteurl','https://fine.example','yes'),(2,'home','https://fine.example','yes'),(3,'active_plugins','a:2:{i:0;s:19:\"akismet/akismet.php\";i:1;s:9:\"hello.php\";}','yes'),(4,'stylesheet','child','yes'),(5,'template','parent','yes');
INSERT INTO `wpx_postmeta` VALUES
(1,10,'_wp_attached_file','2024/01/a.jpg'),
(2,10,'_wp_attachment_metadata','a:1:{s:5:\"sizes\";a:1:{s:5:\"thumb\";a:1:{s:4:\"file\";s:9:\"a-150.jpg\";}}}'),
(3,11,'_edit_lock','1;2');
"""


def test_options_and_attachments(tmp_path):
    p = tmp_path / "d.sql"
    p.write_text(DUMP)
    assert dump.detect_prefix(p) == "wpx_"
    opts = dump.options(p, "wpx_")
    plugins, stylesheet, template = model.active_from_options(opts)
    assert plugins == {"akismet", "hello.php"}
    assert (stylesheet, template) == ("child", "parent")
    atts = dump.attachments(p, "wpx_")
    assert atts["10"]["file"] == "2024/01/a.jpg"
    from wordpress_nix_tools.media.offload import attachment_paths

    assert attachment_paths(atts) == {"2024/01/a.jpg", "2024/01/a-150.jpg"}


@pytest.fixture
def auditor(repo):
    spec = importlib.util.spec_from_file_location("audit_mysql_dump", repo / "tools" / "audit-mysql-dump.py")
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_strip_keeps_other_tuples_byte_for_byte(auditor):
    src = (
        "INSERT INTO `wp_usermeta` VALUES (1,1,'nickname','a, (b) \\'c\\''),(2,1,'session_tokens','x'),"
        "(3,1,'_application_passwords','y'),(4,2,'nickname','d');\n"
        "INSERT INTO `wp_options` VALUES (9,'mainwp_child_pubkey','k','yes');\n"
        "INSERT INTO `wp_posts` VALUES (1,'session_tokens');\n"
    )
    out = io.StringIO()
    f = auditor.audit(io.StringIO(src).readlines(), out, strip=True)
    text = out.getvalue()
    assert "INSERT INTO `wp_usermeta` VALUES (1,1,'nickname','a, (b) \\'c\\''),(4,2,'nickname','d');" in text
    assert "mainwp_child_pubkey" not in text
    assert "INSERT INTO `wp_posts` VALUES (1,'session_tokens');" in text
    assert (f.session_tokens, f.app_passwords, f.mainwp_options) == (1, 1, ["mainwp_child_pubkey"])


def test_exit_codes(auditor, repo, tmp_path):
    v3 = str(repo / "tools/test/fixture-v3-dump.sql")
    assert auditor.main([v3]) == 1
    ids = [
        "option-obfuscated:wp_cache_x",
        "option-script:widget_custom_html",
        "snippet:wpcb_snippets:3",
        "home-mismatch",
    ]
    accept = [x for i in ids for x in ("--accept", i)]
    assert auditor.main([v3, "--strip", "--out", str(tmp_path / "c.sql"), *accept]) == 0
    assert auditor.main([str(tmp_path / "c.sql"), *accept]) == 0
    with pytest.raises(SystemExit) as e:
        auditor.main(["--nope"])
    assert e.value.code == 2


@pytest.mark.skipif(shutil.which("composer") is None, reason="composer is not on PATH")
def test_composer_validate_flags_pass_on_stock_bedrock(repo, tmp_path):
    """The site's composer-validate check (modules/site.nix) on the template."""
    for name in ("composer.json", "composer.lock"):
        shutil.copy(repo / "templates/bedrock-site" / name, tmp_path / name)
    p = subprocess.run(
        [
            "composer",
            "validate",
            "--strict",
            "--no-interaction",
            "--no-check-all",
            "--no-check-publish",
            "--check-lock",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            "HOME": str(tmp_path),
            "COMPOSER_HOME": str(tmp_path / ".composer"),
            "PATH": __import__("os").environ["PATH"],
        },
    )
    assert p.returncode == 0, p.stdout + p.stderr
