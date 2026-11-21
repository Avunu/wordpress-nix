"""edge provision, media provision and cutover against the in-memory Cloudflare."""

import copy

import pytest

from fakes import FakeCloudflare
from wordpress_nix_tools.common.cloudflare import Cloudflare, CloudflareError
from wordpress_nix_tools.edge import provision as edge
from wordpress_nix_tools.media import provision as media
from wordpress_nix_tools.site import cutover as cut

QUIET = dict(log=lambda m: None)


@pytest.fixture
def fake():
    f = FakeCloudflare()
    zid = f.add_zone("fine.example", account="client-acct")
    f.dns[zid] = [
        {
            "id": "r1",
            "type": "A",
            "name": "fine.example",
            "content": "203.0.113.7",
            "proxied": True,
            "ttl": 1,
            "comment": "xcloud",
        },
        {"id": "r2", "type": "A", "name": "fine.example", "content": "203.0.113.8", "proxied": True, "ttl": 1},
        {
            "id": "r3",
            "type": "CNAME",
            "name": "www.fine.example",
            "content": "fine.example",
            "proxied": False,
            "ttl": 60,
            "tags": ["a:b"],
        },
    ]
    f.pagerules[zid] = [
        {
            "id": "pr1",
            "status": "active",
            "targets": [],
            "actions": [{"id": "cache_level", "value": "cache_everything"}],
        },
        {"id": "pr2", "status": "active", "targets": [], "actions": [{"id": "always_use_https"}]},
    ]
    f.rulesets[(zid, "http_request_cache_settings")] = [
        {
            "ref": "xcloud-html",
            "description": "xCloud HTML",
            "expression": "true",
            "action": "set_cache_settings",
            "action_parameters": {"cache": True},
            "enabled": True,
        }
    ]
    f.rulesets[(zid, "http_request_transform")] = [
        {"ref": "t1", "expression": "true", "action": "rewrite", "enabled": True}
    ]
    f.apo[zid] = {"enabled": True, "cf": True, "wordpress": True, "wp_plugin": True}
    return f


def test_zone_walks_labels_from_the_host(fake):
    cf = Cloudflare(transport=fake)
    assert cf.zone_for("fine.example").name == "fine.example"
    assert cf.zone_for("staging.fine.example").account == "client-acct"
    assert cf.zone_for("nowhere.test") is None


def test_partial_zone_refused():
    f = FakeCloudflare()
    f.add_zone("cname.example", type="partial")
    with pytest.raises(CloudflareError):
        Cloudflare(transport=f).require_full_zone("cname.example")


def test_edge_provision_is_idempotent(fake):
    cf = Cloudflare(transport=fake)
    first = edge.provision(cf, "fine", "fine.example", cluster="ovh-01", access=True, emails=["@avunu.com"], **QUIET)
    tunnel = first["tunnel"]["id"]
    assert first["tunnel"]["accountId"] == "client-acct"
    creds = first["secrets"]["fine-cf"]["cloudflared"]
    assert creds["TunnelID"] == tunnel and creds["AccountTag"] == "client-acct" and creds["TunnelSecret"]
    zid = "zone-fine.example"
    staging = [r for r in fake.dns[zid] if r["name"] == "staging.fine.example"]
    assert staging == [
        {
            "id": staging[0]["id"],
            "type": "CNAME",
            "name": "staging.fine.example",
            "content": f"{tunnel}.cfargotunnel.com",
            "proxied": True,
            "ttl": 1,
        }
    ]
    cache = fake.rulesets[(zid, "http_request_cache_settings")]
    assert [r.get("ref") for r in cache] == ["xcloud-html", edge.REF_STATIC, edge.REF_HTML]
    assert next(r for r in cache if r["ref"] == edge.REF_HTML)["enabled"] is False
    assert fake.rulesets[(zid, "http_ratelimit")][0]["ref"] == edge.REF_RATELIMIT
    assert len(fake.access["client-acct"]) == 1 + len(edge.ACCESS_BYPASS)
    writes = len([c for c in fake.calls if c[0] != "GET"])
    again = edge.provision(cf, "fine", "fine.example", cluster="ovh-01", access=True, emails=["@avunu.com"], **QUIET)
    assert again["tunnel"]["id"] == tunnel
    # The rerun only reads.
    assert len([c for c in fake.calls if c[0] != "GET"]) == writes


def test_media_provision_mints_once_and_rotates(fake):
    cf = Cloudflare(transport=fake)
    probes = []
    out = media.provision(cf, "fine", "fine.example", probe=lambda c, o: probes.append(o) or [], **QUIET)
    assert out["bucket"] == "wp-fine-media"
    assert ("client-acct", "wp-fine-media") in fake.buckets
    assert fake.buckets[("client-acct", "wp-fine-media")]["domains"] == ["media.fine.example"]
    origins = fake.buckets[("client-acct", "wp-fine-media")]["cors"]["rules"][0]["allowed"]["origins"]
    assert origins == ["https://fine.example", "https://www.fine.example", "https://staging.fine.example"]
    rw = out["secrets"]["fine-cloud-files"]
    assert rw["S3_ENDPOINT"] == "https://client-acct.r2.cloudflarestorage.com" and len(rw["S3_SECRET"]) == 64
    assert set(out["secrets"]) == {"fine-cloud-files", "fine-media-ro"}
    assert probes == ["https://fine.example"]
    tokens = {t["name"] for t in fake.tokens["client-acct"]}
    assert tokens == {"wordpress-nix-fine-media", "wordpress-nix-fine-media-ro"}
    scope = fake.tokens["client-acct"][0]["policies"][0]["resources"]
    assert list(scope) == ["com.cloudflare.edge.r2.bucket.client-acct_default_wp-fine-media"]

    again = media.provision(cf, "fine", "fine.example", **QUIET)
    assert again["secrets"] == {}
    old = {t["id"] for t in fake.tokens["client-acct"]}
    rotated = media.provision(cf, "fine", "fine.example", rotate=True, **QUIET)
    assert set(rotated["secrets"]) == {"fine-cloud-files", "fine-media-ro"}
    now = {t["id"] for t in fake.tokens["client-acct"]}
    assert len(now) == 2 and not (now & old)


def test_media_provision_refuses_user_owned_and_failed_probe(fake):
    fake.can_mint = False
    with pytest.raises(CloudflareError):
        media.provision(Cloudflare(transport=fake), "fine", "fine.example", **QUIET)
    fake.can_mint = True
    with pytest.raises(CloudflareError):
        media.provision(Cloudflare(transport=fake), "fine", "fine.example", probe=lambda c, o: ["put failed"], **QUIET)
    assert all(t["name"] != "wordpress-nix-fine-media" for t in fake.tokens.get("client-acct", []))


def test_media_adopt_requires_the_bucket(fake):
    with pytest.raises(CloudflareError):
        media.provision(Cloudflare(transport=fake), "ap", "fine.example", adopt="site-files", **QUIET)
    fake.buckets[("client-acct", "site-files")] = {"domains": ["media.fine.example"], "cors": None}
    out = media.provision(Cloudflare(transport=fake), "ap", "fine.example", adopt="site-files", **QUIET)
    assert out["bucket"] == "site-files"
    assert fake.buckets[("client-acct", "site-files")]["domains"] == ["media.fine.example"]


def strip_ids(records):
    return sorted(
        ({k: v for k, v in r.items() if k != "id"} for r in records), key=lambda r: (r["name"], r["type"], r["content"])
    )


def test_cutover_and_rollback_are_exact(fake, tmp_path):
    cf = Cloudflare(transport=fake)
    edge.provision(cf, "fine", "fine.example", cluster="ovh-01", access=False, emails=[], **QUIET)
    zid = "zone-fine.example"
    before = {
        "dns": strip_ids([r for r in fake.dns[zid] if r["name"] in ("fine.example", "www.fine.example")]),
        "pagerules": copy.deepcopy(fake.pagerules[zid]),
        "rulesets": copy.deepcopy(fake.rulesets),
        "apo": copy.deepcopy(fake.apo[zid]),
    }
    record = cut.cutover(cf, tmp_path, "fine", "fine.example", **QUIET)
    tunnel = record["tunnel"]
    apex = [r for r in fake.dns[zid] if r["name"] == "fine.example"]
    assert [(r["type"], r["content"], r["proxied"]) for r in apex] == [("CNAME", f"{tunnel}.cfargotunnel.com", True)]
    assert fake.pagerules[zid][0]["status"] == "disabled" and fake.pagerules[zid][1]["status"] == "active"
    cache = {r["ref"]: r["enabled"] for r in fake.rulesets[(zid, "http_request_cache_settings")]}
    assert cache["xcloud-html"] is False and cache[edge.REF_HTML] is True
    assert fake.apo[zid]["enabled"] is False
    assert fake.purges == 1

    # A rerun reuses the snapshot: it never records the state it made.
    cut.cutover(cf, tmp_path, "fine", "fine.example", **QUIET)
    assert cut.records.read(cut.record_path(tmp_path, "fine"))["snapshot"]["dns"]["fine.example"][0]["type"] == "A"

    cut.rollback(cf, tmp_path, "fine", **QUIET)
    after = {
        "dns": strip_ids([r for r in fake.dns[zid] if r["name"] in ("fine.example", "www.fine.example")]),
        "pagerules": fake.pagerules[zid],
        "rulesets": fake.rulesets,
        "apo": fake.apo[zid],
    }
    assert after == before
    assert fake.purges == 3
    assert cut.records.read(cut.record_path(tmp_path, "fine"))["state"] == "rolled-back"
