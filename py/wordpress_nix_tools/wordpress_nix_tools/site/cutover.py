"""``wordpress-nix cutover <slug> --domain D [--rollback]`` (plan §8.6, §13).

Cutover, in the zone's account:
1. Snapshot, exactly: the DNS records at the apex and ``www`` (type, content,
   proxied, TTL, comment, tags, priority), the zone's Page Rules, its Cache and
   Transform rulesets, and APO, into ``.wordpress-nix-backup/cutover-<slug>.json``
   (printed too, for the cluster-ovh-01 issue). A rerun reuses the snapshot: it
   never records the state it made itself.
2. Disable the xCloud-era HTML caching (Page Rules caching everything, other
   Cache Rules that cache) and APO; enable ``wordpress-nix-html-cache``.
3. Proxied CNAMEs for the apex and ``www`` to ``<tunnel>.cfargotunnel.com``.
4. Purge the zone.

``--rollback`` puts the records, the rules and APO back as the snapshot has them
and purges again. Only the record ids differ afterwards (Cloudflare assigns them).
"""

import copy
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from wordpress_nix_tools.common import records
from wordpress_nix_tools.common.cloudflare import ADDRESS_TYPES, Cloudflare, CloudflareError, Zone
from wordpress_nix_tools.edge.provision import REF_HTML, clean_rule, tunnel_name

PHASES = ("http_request_cache_settings", "http_request_transform", "http_response_headers_transform")
RECORD_KEYS = ("type", "name", "content", "proxied", "ttl", "comment", "tags", "priority", "data")
APO = "automatic_platform_optimization"


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_path(root: Path, slug: str) -> Path:
    return records.backup_dir(root) / f"cutover-{slug}.json"


def dns_body(r: dict) -> dict:
    return {k: r[k] for k in RECORD_KEYS if k in r and r[k] is not None}


def snapshot(cf: Cloudflare, zone: Zone, domain: str) -> dict:
    snap: dict = {"dns": {}, "pagerules": [], "rulesets": {}, "apo": None}
    for name in (domain, f"www.{domain}"):
        snap["dns"][name] = [dns_body(r) for r in cf.records(zone, name)]
    pr = cf.api("GET", f"/zones/{zone.id}/pagerules")
    snap["pagerules"] = copy.deepcopy(pr.get("result") or []) if pr.get("success") else []
    for phase in PHASES:
        env = cf.api("GET", f"/zones/{zone.id}/rulesets/phases/{phase}/entrypoint")
        snap["rulesets"][phase] = (
            [clean_rule(r) for r in (env.get("result") or {}).get("rules") or []] if env.get("success") else None
        )
    apo = cf.api("GET", f"/zones/{zone.id}/settings/{APO}")
    snap["apo"] = (apo.get("result") or {}).get("value") if apo.get("success") else None
    return snap


def caches_everything(rule: dict) -> bool:
    return any(a.get("id") == "cache_level" and a.get("value") == "cache_everything" for a in rule.get("actions") or [])


def disable_legacy_cache(cf: Cloudflare, zone: Zone, snap: dict, log) -> None:
    for rule in snap["pagerules"]:
        if caches_everything(rule) and rule.get("status") == "active":
            cf.ok(
                "PATCH",
                f"/zones/{zone.id}/pagerules/{rule['id']}",
                {"status": "disabled"},
                context="disabling a page rule",
            )
            log(f"page rule {rule['id']}: disabled")
    rules = snap["rulesets"].get("http_request_cache_settings")
    if rules is not None:
        new = []
        for r in rules:
            r = dict(r)
            ours = str(r.get("ref", "")).startswith("wordpress-nix-")
            if r.get("ref") == REF_HTML:
                r["enabled"] = True
            elif (
                not ours and r.get("action") == "set_cache_settings" and (r.get("action_parameters") or {}).get("cache")
            ):
                r["enabled"] = False
                log(f"cache rule {r.get('description') or r.get('ref') or '?'}: disabled")
            new.append(r)
        if new != rules:
            cf.ok(
                "PUT",
                f"/zones/{zone.id}/rulesets/phases/http_request_cache_settings/entrypoint",
                {"rules": new},
                context="updating the cache rules",
            )
    apo = snap.get("apo")
    if isinstance(apo, dict) and apo.get("enabled"):
        cf.ok(
            "PATCH", f"/zones/{zone.id}/settings/{APO}", {"value": {**apo, "enabled": False}}, context="disabling APO"
        )
        log("APO: disabled")


def purge(cf: Cloudflare, zone: Zone) -> None:
    cf.ok("POST", f"/zones/{zone.id}/purge_cache", {"purge_everything": True}, context="purging the zone")


def find_tunnel(cf: Cloudflare, account: str, name: str) -> str:
    env = cf.api("GET", f"/accounts/{account}/cfd_tunnel?name={name}&is_deleted=false")
    found = (env.get("result") or []) if env.get("success") else []
    if not found:
        raise CloudflareError(f"no tunnel {name} in account {account}: run `wordpress-nix edge provision`", {})
    return found[0]["id"]


def cutover(
    cf: Cloudflare, root: Path, slug: str, domain: str, *, tunnel: str = "", cluster: str = "ovh-01", log=print
) -> dict:
    zone = cf.require_full_zone(domain)
    path = record_path(root, slug)
    record = records.read(path) if path.is_file() else None
    if record is None or record.get("state") == "rolled-back":
        if record is not None:
            path.rename(path.with_name(f"cutover-{slug}.{record.get('rolledBackAt', 'old').replace(':', '')}.json"))
        record = {
            "slug": slug,
            "domain": domain,
            "zone": {"id": zone.id, "name": zone.name, "account": zone.account},
            "takenAt": now(),
            "snapshot": snapshot(cf, zone, domain),
            "state": "snapshot",
        }
        records.write(path, record)
        log(f"snapshot: {path}")
    else:
        log(f"reusing the snapshot of {record['takenAt']} ({path})")
    tunnel = tunnel or find_tunnel(cf, zone.account, tunnel_name(cluster, slug))
    record["tunnel"] = tunnel
    disable_legacy_cache(cf, zone, record["snapshot"], log)
    for name in (domain, f"www.{domain}"):
        log(f"{name}: {cf.cname(name, f'{tunnel}.cfargotunnel.com')}")
    purge(cf, zone)
    log("purged")
    record["state"] = "cut-over"
    record["cutAt"] = now()
    records.write(path, record)
    return record


def rollback(cf: Cloudflare, root: Path, slug: str, *, log=print) -> dict:
    path = record_path(root, slug)
    if not path.is_file():
        raise CloudflareError(f"no cutover record at {path}: nothing to roll back to", {})
    record = records.read(path)
    snap = record["snapshot"]
    zone = cf.zone_for(record["domain"])
    if zone is None:
        raise CloudflareError(f"no zone for {record['domain']}", {})
    for name, snapped in snap["dns"].items():
        # Only the address records were flipped; MX, TXT and the rest stay as they are.
        wanted = [r for r in snapped if r.get("type") in ADDRESS_TYPES]
        for r in cf.address_records(zone, name):
            cf.ok("DELETE", f"/zones/{zone.id}/dns_records/{r['id']}", context=f"removing {name}")
        for r in wanted:
            cf.ok("POST", f"/zones/{zone.id}/dns_records", r, context=f"restoring {name}")
        log(f"{name}: restored {len(wanted)} record(s)")
    for rule in snap["pagerules"]:
        cf.ok(
            "PATCH",
            f"/zones/{zone.id}/pagerules/{rule['id']}",
            {"status": rule.get("status")},
            context="restoring a page rule",
        )
    for phase, rules in snap["rulesets"].items():
        if rules is None:
            continue
        cf.ok(
            "PUT",
            f"/zones/{zone.id}/rulesets/phases/{phase}/entrypoint",
            {"rules": rules},
            context=f"restoring {phase}",
        )
    if snap.get("apo") is not None:
        cf.ok("PATCH", f"/zones/{zone.id}/settings/{APO}", {"value": snap["apo"]}, context="restoring APO")
    purge(cf, zone)
    log("restored and purged")
    record["state"] = "rolled-back"
    record["rolledBackAt"] = now()
    records.write(path, record)
    return record


def issue_text(record: dict) -> str:
    return (
        f"### Cutover record: {record['slug']} ({record['domain']})\n\n"
        f"State: {record['state']}. Snapshot taken {record['takenAt']}.\n\n"
        "```json\n" + json.dumps(record, indent=2) + "\n```\n"
    )


def cmd(args, cf: Cloudflare | None = None) -> int:
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    root = Path(args.site_root)
    try:
        cf = cf or Cloudflare()
        if args.rollback:
            record = rollback(cf, root, args.slug, log=log)
        else:
            if not args.domain:
                print("cutover: --domain is required", file=sys.stderr)
                return 2
            record = cutover(
                cf,
                root,
                args.slug,
                args.domain,
                tunnel=args.tunnel or "",
                cluster=args.cluster or os.environ.get("WORDPRESS_NIX_CLUSTER", "ovh-01"),
                log=log,
            )
    except CloudflareError as e:
        log(f"cutover: {e}")
        return 1
    print(issue_text(record))
    return 0
