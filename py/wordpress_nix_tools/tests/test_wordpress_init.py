import json

import pytest

from wordpress_nix_tools.common import data
from wordpress_nix_tools.wordpress_init import args as argsmod
from wordpress_nix_tools.wordpress_init import classify, composer_json, gitignore, template
from wordpress_nix_tools.wordpress_init.args import UsageError, parse_args
from wordpress_nix_tools.wordpress_init.config import InitConfig
from wordpress_nix_tools.wordpress_init.presets import load as load_presets
from wordpress_nix_tools.wordpress_init.state import Item, Run, normalize_name


def run_for(tmp_path, repo, **kw) -> Run:
    st = Run(
        args=parse_args(kw.pop("argv", [])),
        config=InitConfig(template=str(repo / "templates" / "bedrock-site"), data=str(repo / "data")),
        presets=load_presets(),
    )
    st.root = tmp_path
    st.php = "php84"
    st.name = "fixture"
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def test_args_shared_flags():
    a = parse_args(["--migrate", "-y", "--skip-lock", "--commit=feat: x", "--name", "s", "--home-url=https://x", "d"])
    assert (a.mode, a.assume_yes, a.skip_lock, a.commit, a.commit_msg, a.name, a.home_url, a.target) == (
        "migrate",
        True,
        True,
        True,
        "feat: x",
        "s",
        "https://x",
        "d",
    )


def test_args_repeated_and_accept_modified():
    a = parse_args(["--keep-plugin", "a", "--keep-plugin=b", "--accept-modified", "x:vendor"])
    assert a.keep_plugins == ["a", "b"]
    assert a.accepted() == {"x": "vendor"}
    with pytest.raises(UsageError) as e:
        parse_args(["--accept-modified", "x:delete"])
    assert e.value.code == 1


def test_args_usage_codes():
    with pytest.raises(UsageError) as e:
        parse_args(["--bogus"])
    assert e.value.code == 1
    with pytest.raises(UsageError) as e:
        parse_args(["--sync", "--bogus"])
    assert e.value.code == 2
    with pytest.raises(UsageError):
        parse_args(["--php", "php82"])


def test_usage_lists_every_documented_flag(repo):
    doc = (repo / "docs" / "reference" / "scaffolder.md").read_text()
    for flag in ("--home-url", "--keep-salts", "--accept-modified", "--no-loosen", "--survey", "--dump", "--strict"):
        assert flag in argsmod.USAGE
        assert flag in doc


def test_normalize_name():
    assert normalize_name("site-ExamplePainting.com") == "examplepainting-com"
    assert normalize_name("___") == "site"


def test_headers():
    text = "<?php\n/**\n * Plugin Name: Akismet\n * Version: 5.3\n * Update URI: false\n */"
    h = classify.headers(text, classify.PLUGIN_KEYS)
    assert h == {"Plugin Name": "Akismet", "Version": "5.3", "Update URI": "false"}


def test_author_matches():
    assert classify.author_matches("<a href='x'>Automattic</a>", "Automattic - Anti-spam Team")
    assert not classify.author_matches("Avunu", "Bagus")
    assert classify.author_matches("", "Anyone")


def test_drop_reason_only_collision():
    assert data.drop_reason("mainwp-child")
    assert data.drop_reason("xcloud-anything")
    assert data.drop_reason("jwt-auth") is None
    assert data.drop_reason("jwt-auth", collision=True)


def test_gitignore_block_is_derived_and_idempotent(tmp_path):
    (tmp_path / "web/app/plugins/premium").mkdir(parents=True)
    (tmp_path / "web/app/plugins/akismet").mkdir(parents=True)
    (tmp_path / "web/app/plugins/single.php").write_text("<?php")
    (tmp_path / "web/app/mu-plugins/custom").mkdir(parents=True)
    (tmp_path / "composer.json").write_text(
        json.dumps({"require": {"wp-plugin/akismet": "5.3", "wp-theme/astra": "4.0"}})
    )
    lines = gitignore.block_lines(tmp_path)
    assert lines == [
        "/.wordpress-nix-backup/",
        "!web/app/plugins/premium/",
        "!web/app/plugins/single.php",
        "!web/app/mu-plugins/custom/",
        "web/app/themes/astra/",
    ]
    once = gitignore.with_block("web/wp\n", lines)
    assert once.startswith("web/wp\n\n# BEGIN wordpress-nix (managed)")
    assert gitignore.with_block(once, lines) == once
    with pytest.raises(ValueError):
        gitignore.with_block("# BEGIN wordpress-nix (managed)\nx\n", lines)


def test_template_flake_render(repo, tmp_path):
    st = run_for(tmp_path, repo, development_only=["wpcodebox2"], active_theme="cwicly", php="php85")
    text = template.render_flake((repo / "templates/bedrock-site/flake.nix").read_text(), st)
    assert 'name = "fixture";' in text
    assert 'php = "php85";' in text
    assert 'developmentOnlyPlugins = [ "wpcodebox2" ];' in text
    assert 'activeTheme = "cwicly";' in text


def test_dependabot_time_is_stable_and_early():
    t = template.dependabot_time("examplepainting")
    assert t == template.dependabot_time("examplepainting")
    assert "00:00" <= t <= "05:55"


def test_composer_reconcile(repo, tmp_path):
    st = run_for(tmp_path, repo, mode="migrate", wp_version="6.8.3")
    st.staging = template.render_template(st)
    st.items = [
        Item(kind="plugin", name="akismet", disp="composer", package="wp-plugin/akismet", constraint="5.3"),
        Item(kind="plugin", name="wp-cloud-files", disp="avunu", package="avunu/wp-cloud-files", constraint="^0.6"),
        Item(kind="theme", name="cwicly", disp="commit"),
    ]
    composer_json.reconcile(st)
    doc = json.loads((tmp_path / "composer.json").read_text())
    req = doc["require"]
    assert doc["name"] == "avunu/site-fixture"
    assert req["roots/wordpress"] == "6.8.3"
    assert req["wp-plugin/akismet"] == "5.3"
    assert req["avunu/wp-cloud-files"] == "^0.6"
    assert "wp-plugin/two-factor" in req
    assert "wp-theme/twentytwentyfive" not in req
    assert list(req)[0] == "php"
    assert doc["config"]["platform"]["php"] == "8.4"
    wp = next(r for r in doc["repositories"] if r["url"] == composer_json.WP_PACKAGES)
    assert wp["only"] == ["wp-plugin/*", "wp-theme/*"]
    before = (tmp_path / "composer.json").read_text()
    composer_json.reconcile(st)
    assert (tmp_path / "composer.json").read_text() == before
    assert composer_json.loosen(tmp_path)
    loose = json.loads((tmp_path / "composer.json").read_text())["require"]
    assert loose["wp-plugin/akismet"] == "^5.3"
    assert loose["roots/wordpress"] == "6.8.3"
    assert not composer_json.loosen(tmp_path)


def test_only_guard_added_when_missing():
    doc = {"repositories": [{"type": "composer", "url": "https://example.org"}]}
    composer_json.ensure_only_guard(doc)
    assert doc["repositories"][0]["url"] == composer_json.WP_PACKAGES


def test_suspicious_scan(tmp_path):
    (tmp_path / "a.php").write_text("<?php eval(base64_decode('x'));")
    (tmp_path / "b.php").write_text("<?php echo 1;")
    assert classify.scan_suspicious(tmp_path) == ["a.php"]
