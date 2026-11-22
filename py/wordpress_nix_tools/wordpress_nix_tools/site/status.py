"""``wordpress-nix site status [<slug>] [--domain D] --json``: read-only probes of a
site's moving parts: HTTP on the apex, ``staging.`` and ``media.``, the DNS records
and the tunnel's health (with the Cloudflare token), and the cutover record."""

import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from wordpress_nix_tools.common import records
from wordpress_nix_tools.common.cloudflare import Cloudflare, CloudflareError

Http = Callable[[str], int]


def http_status(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "wordpress-nix/status"})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


def status(slug: str, domain: str, root: Path, *, http: Http = http_status, cf: Cloudflare | None = None) -> dict:
    out: dict = {"slug": slug, "domain": domain, "http": {}, "dns": {}, "tunnel": None, "cutover": None}
    for host in (domain, f"staging.{domain}", f"media.{domain}"):
        out["http"][host] = http(f"https://{host}/")
    rec = records.find(root, f"cutover-{slug}.json")
    if rec:
        r = records.read(rec)
        out["cutover"] = {"state": r.get("state"), "takenAt": r.get("takenAt"), "cutAt": r.get("cutAt")}
        tunnel = r.get("tunnel")
    else:
        tunnel = None
    if cf is not None:
        zone = cf.zone_for(domain)
        if zone:
            for name in (domain, f"www.{domain}", f"staging.{domain}"):
                out["dns"][name] = [
                    {"type": x.get("type"), "content": x.get("content"), "proxied": x.get("proxied")}
                    for x in cf.records(zone, name)
                ]
            for recs in out["dns"].values():
                for x in recs:
                    if str(x.get("content", "")).endswith(".cfargotunnel.com"):
                        tunnel = tunnel or x["content"].split(".", 1)[0]
            if tunnel:
                env = cf.api("GET", f"/accounts/{zone.account}/cfd_tunnel/{tunnel}")
                if env.get("success"):
                    t = env.get("result") or {}
                    out["tunnel"] = {
                        "id": tunnel,
                        "status": t.get("status"),
                        "connections": len(t.get("connections") or []),
                    }
    return out


def cmd(args, cf: Cloudflare | None = None, http: Http = http_status) -> int:
    root = Path(args.site_root)
    site = {}
    try:
        site = json.loads((root / ".github" / "wordpress-nix.json").read_text())
    except (OSError, ValueError):
        pass
    slug = args.slug or site.get("name")
    domain = args.domain or urlparse(site.get("home") or "").hostname
    if not slug or not domain:
        print("site status: give <slug> and --domain, or run it in the site repository", file=sys.stderr)
        return 2
    if cf is None:
        try:
            cf = Cloudflare()
        except CloudflareError:
            cf = None
    result = status(slug, domain, root, http=http, cf=cf)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for host, code in result["http"].items():
            print(f"{host:<40} HTTP {code}")
        for name, recs in result["dns"].items():
            shown = ", ".join(f"{r['type']} {r['content']}" for r in recs) or "-"
            print(f"{name:<40} {shown}")
        if result["tunnel"]:
            print(f"tunnel {result['tunnel']['id']}: {result['tunnel']['status']}")
        if result["cutover"]:
            print(f"cutover: {result['cutover']['state']}")
    return 0
