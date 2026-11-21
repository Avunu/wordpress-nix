"""site import, verify's pieces, repo adopt and legacy survey, with recorded runners."""

import json
from types import SimpleNamespace

from fakes import Recorder
from wordpress_nix_tools import repo_adopt
from wordpress_nix_tools.legacy import model, survey
from wordpress_nix_tools.site import importer, verify
from wordpress_nix_tools.site.node import Container, find_node


def import_args(tmp_path, **kw):
    dump = tmp_path / "import.sql"
    dump.write_text("-- dump\n")
    base = dict(
        slug="fine",
        dump=str(dump),
        files=None,
        home="https://staging.fine.example",
        from_home=None,
        media_url="",
        inventory=str(tmp_path / "none.json"),
        replace=False,
        node="n1",
    )
    base.update(kw)
    return SimpleNamespace(**base)


SITE = {"home": "https://fine.example", "dropPlugins": ["mainwp-child", "jwt-auth"], "woocommerce": True}


def test_import_refuses_an_installed_site(tmp_path):
    r = Recorder().on("core is-installed", rc=0)
    rc = importer.run_import(Container("fine", "n1", runner=r), import_args(tmp_path), SITE, log=lambda m: None)
    assert rc == 1
    assert not r.ran("db import")


def test_import_sequence_on_staging(tmp_path):
    r = (
        Recorder()
        .on("core is-installed", rc=1)
        .on("information_schema", out="0\t0\t0\t0\n")
        .on("user list", out="1\n4\n")
        .on("wp eval", out="mainwp-child/mainwp-child.php\n")
    )
    c = Container("fine", "n1", runner=r)
    rc = importer.run_import(c, import_args(tmp_path), SITE, log=lambda m: None)
    assert rc == 0
    lines = [line for line, _ in r.calls]
    assert all(
        line.startswith("ssh -o BatchMode=yes root@n1 nixos-container run fine -- runuser -u wordpress --")
        for line in lines
    )
    order = [
        next(i for i, line in enumerate(lines) if needle in line)
        for needle in (
            "db import -",
            "information_schema",
            "search-replace",
            "wp eval",
            "woocommerce_file_download_method",
            "reset-password",
            "core update-db",
            "rewrite flush",
        )
    ]
    assert order == sorted(order)
    assert "--skip-email" in r.ran("reset-password")[0]
    assert "https://staging.fine.example" in " ".join(r.ran("search-replace"))
    assert not r.ran("cluster-backup")


def test_import_replace_backs_up_and_emails_at_production(tmp_path):
    r = Recorder().on("core is-installed", rc=0).on("information_schema", out="0\t0\t0\t0\n").on("user list", out="1\n")
    c = Container("fine", "n1", runner=r)
    rc = importer.run_import(
        c, import_args(tmp_path, replace=True, home="https://fine.example"), SITE, log=lambda m: None
    )
    assert rc == 0
    assert r.ran("cluster-backup fine")
    assert "--skip-email" not in r.ran("reset-password")[0]


def test_import_refuses_executables(tmp_path):
    r = Recorder().on("core is-installed", rc=1).on("information_schema", out="1\t0\t0\t0\n")
    rc = importer.run_import(Container("fine", "n1", runner=r), import_args(tmp_path), SITE, log=lambda m: None)
    assert rc == 1
    assert not r.ran("search-replace")


def test_deactivate_php_names_directories():
    php = importer.deactivate_php(["jwt-auth", "mainwp-child"])
    assert '["jwt-auth", "mainwp-child"]' in php and "deactivate_plugins($off, true)" in php


def test_find_node_from_nomad():
    allocs = [{"ClientStatus": "complete", "NodeName": "n0"}, {"ClientStatus": "running", "NodeName": "n2"}]
    r = Recorder().on("nomad job allocs -json app-fine", out=json.dumps(allocs))
    assert find_node("fine", "", runner=r) == "n2"


def test_counts_and_delta_sql():
    assert verify.parse_counts("wp_posts\t12\nwp_users\t3\n") == {"wp_posts": 12, "wp_users": 3}
    assert "UNION ALL" in verify.counts_sql(["a", "b"])
    sql = verify.delta_sql("wpx_", "2026-11-21 10:00:00")
    assert "`wpx_posts` WHERE `post_modified_gmt` > '2026-11-21 10:00:00'" in sql
    rep = verify.Report()
    verify.compare_counts(rep, {"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert rep.failed == ["rows b: legacy 2 v3 3"]


def test_http_checks():
    answers = {
        "/": (200, "", '<img src="https://media.fine.example/a.jpg">'),
        "/xmlrpc.php": (403, "", ""),
        "/wp-cron.php": (404, "", ""),
        "/.env": (404, "", ""),
        "/app/uploads/2024/01/wordpress-nix-probe.php": (403, "", ""),
        "/app/uploads/woocommerce_uploads/wordpress-nix-probe.zip": (403, "", ""),
        "/wp-admin/": (301, "https://fine.example/wp/wp-admin/", ""),
        "/wp-content/uploads/2024/01/wordpress-nix-probe.jpg": (301, "https://media.fine.example/2024/01/x.jpg", ""),
    }
    rep = verify.Report()
    verify.http_checks(
        rep, lambda url, host: answers[url.removeprefix("https://fine.example")], "https://fine.example", "fine.example"
    )
    assert rep.failed == [], rep.failed


def test_repo_adopt_decisions(tmp_path):
    r = Recorder().on("gh repo view", rc=1)
    assert repo_adopt.Adopter(r).decide("Avunu", "fine").action == "create"
    r = Recorder().on("gh repo view", rc=0).on("--name-only", out="wp-config.php\nwp-content/s212682.sql\nREADME.md\n")
    d = repo_adopt.Adopter(r).decide("Avunu", "fine")
    assert d.action == "replace" and "s212682.sql" in d.reasons[0]
    r = Recorder().on("gh repo view", rc=0).on("--name-only", out="composer.json\n")
    assert repo_adopt.Adopter(r).decide("Avunu", "fine").action == "pr"


def test_repo_adopt_replace_verifies_the_push():
    head = "a" * 40
    r = (
        Recorder()
        .on("rev-parse HEAD", out=head + "\n")
        .on("remote get-url origin", rc=0, out="https://github.com/Avunu/site-fine.git\n")
        .on("ls-remote", out=f"{head}\trefs/heads/main\n")
    )
    a = repo_adopt.Adopter(r)
    a.apply(repo_adopt.Decision("replace", "Avunu/site-fine"), "fine", log=lambda m: None)
    lines = [c for c, _ in r.calls]
    assert any("gh repo rename site-fine-legacy --repo Avunu/site-fine" in c for c in lines)
    assert any("gh repo archive Avunu/site-fine-legacy" in c for c in lines)
    assert any("gh repo create Avunu/site-fine --private" in c for c in lines)
    bad = Recorder().on("rev-parse HEAD", out=head).on("ls-remote", out="b" * 40 + "\trefs/heads/main\n")
    try:
        repo_adopt.Adopter(bad).push_verified("origin", "main")
    except OSError as e:
        assert "refusing" in str(e)
    else:
        raise AssertionError("a mismatched remote tip must fail")


def test_survey_compare_and_csv():
    old = {
        "wordpress": {"version": "6.8.3"},
        "plugins": [{"name": "a", "version": "1", "active": True}, {"name": "b", "version": "2", "active": False}],
        "themes": [{"name": "t", "version": "1", "active": True}],
        "activeTheme": {"stylesheet": "t", "template": "t"},
    }
    assert survey.compare(old, json.loads(json.dumps(old))) == []
    new = json.loads(json.dumps(old))
    new["plugins"][0]["version"] = "1.1"
    new["plugins"][1]["active"] = True
    new["plugins"].append({"name": "c", "version": "3"})
    assert survey.compare(old, new) == ["plugin a 1 → 1.1", "plugin b activated", "plugin c added (3)"]
    csv = survey.to_csv([dict(old, host="h", path="/p", score=3)])
    assert csv.splitlines()[0].startswith("host,path,home") and ",3" in csv.splitlines()[1]


def test_complexity_score():
    s = {
        "plugins": [{"premium": True, "writer": True}, {}],
        "woocommerce": True,
        "php": "8.2.1",
        "wordpress": {"locale": "de_DE"},
        "dbCode": {"triggers": [1]},
        "cloudflare": {"status": "active", "type": "full"},
    }
    assert model.complexity(s) == 2 + 3 + 2 + 15 + 5 + 5 + 10


def test_survey_runs_php_on_stdin():
    out = json.dumps(
        {
            "schema": 1,
            "wordpress": {"version": "6.8.3", "home": "https://fine.example"},
            "plugins": [{"name": "mainwp-child", "active": True}],
            "themes": [],
        }
    )
    r = Recorder().on("php -- /var/www/fine", out=out).on("bash -s", out="core ok\n")
    from wordpress_nix_tools.common.remote import Remote

    s = survey.run_survey(Remote("legacy@host", runner=r), "/var/www/fine")
    assert s["host"] == "legacy@host" and s["checksums"]["core"] == "ok"
    assert s["plugins"][0]["drop"]
    php_call = next(stdin for line, stdin in r.calls if "php -- " in line)
    assert b"SELECT" in php_call and b"INSERT" not in php_call.upper().replace(b"INSERTED", b"")
