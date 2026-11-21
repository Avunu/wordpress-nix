"""The Cloudflare REST API, as moonshot's lib.sh used it, with the provisioning
token from ``WORDPRESS_NIX_CLOUDFLARE_TOKEN``.

Ported helpers: ``zone_for`` (walk up the labels from the hostname itself, so an
apex resolves), ``account_for`` (the zone's account: where a tunnel, a bucket and
an Access app must live), ``cname`` (proxied, idempotent, replacing only the name's A/AAAA/CNAME records),
``can_mint_account_token`` (the 9d83999 probe: an empty POST is refused with 9109
before the body is read when the token cannot mint).

The transport is injectable: ``Cloudflare(token, transport=fake)`` is how the unit
tests drive every command without the network. A transport takes
``(method, path, body)`` and returns the decoded JSON envelope.
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

API = "https://api.cloudflare.com/client/v4"
TOKEN_ENV = "WORDPRESS_NIX_CLOUDFLARE_TOKEN"
ADDRESS_TYPES = ("A", "AAAA", "CNAME")

Transport = Callable[[str, str, Any], dict]


class CloudflareError(Exception):
    def __init__(self, context: str, envelope: dict) -> None:
        errors = envelope.get("errors") or []
        detail = "; ".join(f"[{e.get('code')}] {e.get('message')}" for e in errors if isinstance(e, dict))
        super().__init__(f"{context} failed{': ' + detail if detail else ''}")
        self.envelope = envelope
        self.codes = [e.get("code") for e in errors if isinstance(e, dict)]
        # What an operation had already done when it failed, for the caller to show.
        self.partial: dict | None = None


@dataclass
class Zone:
    id: str
    name: str
    account: str
    status: str = ""
    type: str = ""


def http_transport(token: str) -> Transport:
    def call(method: str, path: str, body: Any = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            f"{API}{path}",
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}")
            except ValueError:
                return {"success": False, "errors": [{"code": e.code, "message": str(e)}]}

    return call


class Cloudflare:
    def __init__(self, token: str | None = None, transport: Transport | None = None) -> None:
        if transport is None:
            token = token or os.environ.get(TOKEN_ENV, "")
            if not token:
                raise CloudflareError(f"no provisioning token: set {TOKEN_ENV} (decrypt it from cluster-ovh-01)", {})
            transport = http_transport(token)
        self.call = transport
        self._zones: dict[str, Zone | None] = {}

    def api(self, method: str, path: str, body: Any = None) -> dict:
        return self.call(method, path, body)

    def ok(self, method: str, path: str, body: Any = None, *, context: str = "") -> Any:
        env = self.api(method, path, body)
        if not env.get("success"):
            raise CloudflareError(context or f"{method} {path}", env)
        return env.get("result")

    # ── zones and accounts ────────────────────────────────────────────────
    def zone_for(self, host: str) -> Zone | None:
        name = host.strip(".").lower()
        while name:
            if name in self._zones:
                if self._zones[name]:
                    return self._zones[name]
            else:
                env = self.api("GET", f"/zones?name={name}")
                result = env.get("result") or []
                zone = None
                if env.get("success") and result:
                    r = result[0]
                    zone = Zone(
                        id=r["id"],
                        name=r["name"],
                        account=(r.get("account") or {}).get("id", ""),
                        status=r.get("status", ""),
                        type=r.get("type", ""),
                    )
                self._zones[name] = zone
                if zone:
                    return zone
            if "." not in name:
                return None
            name = name.split(".", 1)[1]
        return None

    def account_for(self, host: str) -> str | None:
        z = self.zone_for(host)
        return z.account if z else None

    def require_full_zone(self, host: str) -> Zone:
        """The zone, refusing one that is not active in a full setup (§8.6)."""
        z = self.zone_for(host)
        if z is None:
            raise CloudflareError(f"no zone the token can reach owns {host}", {})
        if z.status and z.status != "active":
            raise CloudflareError(f"zone {z.name} is {z.status}, not active", {})
        if z.type and z.type != "full":
            raise CloudflareError(f"zone {z.name} is a {z.type} setup; edge provisioning needs a full setup", {})
        return z

    # ── DNS ───────────────────────────────────────────────────────────────
    def records(self, zone: Zone, name: str) -> list[dict]:
        return self.ok("GET", f"/zones/{zone.id}/dns_records?name={name}", context=f"listing {name}") or []

    def address_records(self, zone: Zone, name: str) -> list[dict]:
        """The A, AAAA and CNAME records at a name: the ones a CNAME flip replaces.
        MX, TXT (SPF, site verification), CAA, SRV and the rest are never touched."""
        return [r for r in self.records(zone, name) if r.get("type") in ADDRESS_TYPES]

    def cname(self, host: str, target: str, *, proxied: bool = True, ttl: int = 1) -> str:
        """CNAME host → target. Replaces the A, AAAA or CNAME records at the name (the
        cutover flip) and leaves every other type alone. Returns "created", "updated"
        or "unchanged"."""
        zone = self.zone_for(host)
        if zone is None:
            raise CloudflareError(f"no zone owns {host}", {})
        body = {"type": "CNAME", "name": host, "content": target, "proxied": proxied, "ttl": ttl}
        existing = self.address_records(zone, host)
        if existing:
            r = existing[0]
            if len(existing) == 1 and (r.get("type"), r.get("content"), r.get("proxied")) == ("CNAME", target, proxied):
                return "unchanged"
            # A name holds one CNAME or A/AAAA records, never both: the other
            # address records go first, or Cloudflare refuses the CNAME.
            for extra in existing[1:]:
                self.ok("DELETE", f"/zones/{zone.id}/dns_records/{extra['id']}", context=f"removing {host}")
            self.ok("PUT", f"/zones/{zone.id}/dns_records/{r['id']}", body, context=f"updating {host}")
            return "updated"
        self.ok("POST", f"/zones/{zone.id}/dns_records", body, context=f"creating {host}")
        return "created"

    # ── tokens ────────────────────────────────────────────────────────────
    def can_mint_account_token(self, account: str) -> bool:
        env = self.api("POST", f"/accounts/{account}/tokens", {})
        codes = [e.get("code") for e in env.get("errors") or [] if isinstance(e, dict)]
        return 9109 not in codes

    def permission_group(self, name: str) -> str:
        groups = self.ok("GET", "/user/tokens/permission_groups", context="listing permission groups") or []
        for g in groups:
            if g.get("name") == name:
                return g["id"]
        raise CloudflareError(f"permission group {name!r} not found", {})
