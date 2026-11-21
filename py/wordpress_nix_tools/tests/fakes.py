"""An in-memory Cloudflare API, enough of it for every command's unit tests, and a
recording command runner."""

import base64
import copy
import json
import re
from dataclasses import dataclass, field


def ok(result=None, **extra):
    return {"success": True, "errors": [], "result": result, **extra}


def fail(code=1000, message="error"):
    return {"success": False, "errors": [{"code": code, "message": message}], "result": None}


@dataclass
class FakeCloudflare:
    zones: dict = field(default_factory=dict)  # name → {id, account, status, type}
    dns: dict = field(default_factory=dict)  # zone id → [records]
    pagerules: dict = field(default_factory=dict)  # zone id → [rules]
    rulesets: dict = field(default_factory=dict)  # (zone id, phase) → [rules]
    apo: dict = field(default_factory=dict)  # zone id → value
    buckets: dict = field(default_factory=dict)  # (account, name) → {domains, cors}
    tokens: dict = field(default_factory=dict)  # account → [tokens]
    tunnels: dict = field(default_factory=dict)  # account → [tunnels]
    access: dict = field(default_factory=dict)  # account → [apps]
    can_mint: bool = True
    calls: list = field(default_factory=list)
    purges: int = 0
    _n: int = 0

    def add_zone(self, name, account="acct1", status="active", type="full"):
        zid = f"zone-{name}"
        self.zones[name] = {"id": zid, "name": name, "account": {"id": account}, "status": status, "type": type}
        self.dns.setdefault(zid, [])
        return zid

    def next_id(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def zone_by_id(self, zid):
        return next(z for z in self.zones.values() if z["id"] == zid)

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        p, _, query = path.partition("?")
        q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)

        if p == "/zones" and method == "GET":
            z = self.zones.get(q.get("name"))
            return ok([z] if z else [])
        m = re.fullmatch(r"/zones/([^/]+)/dns_records", p)
        if m:
            zid = m.group(1)
            if method == "GET":
                return ok([r for r in self.dns[zid] if r["name"] == q.get("name")])
            if method == "POST":
                r = {"id": self.next_id("rec"), **body}
                self.dns[zid].append(r)
                return ok(r)
        m = re.fullmatch(r"/zones/([^/]+)/dns_records/([^/]+)", p)
        if m:
            zid, rid = m.groups()
            recs = self.dns[zid]
            idx = next(i for i, r in enumerate(recs) if r["id"] == rid)
            if method == "PUT":
                recs[idx] = {"id": rid, **body}
                return ok(recs[idx])
            if method == "DELETE":
                recs.pop(idx)
                return ok({"id": rid})
        m = re.fullmatch(r"/zones/([^/]+)/pagerules", p)
        if m and method == "GET":
            return ok(copy.deepcopy(self.pagerules.get(m.group(1), [])))
        m = re.fullmatch(r"/zones/([^/]+)/pagerules/([^/]+)", p)
        if m and method == "PATCH":
            for r in self.pagerules[m.group(1)]:
                if r["id"] == m.group(2):
                    r.update(body)
                    return ok(r)
        m = re.fullmatch(r"/zones/([^/]+)/rulesets/phases/([^/]+)/entrypoint", p)
        if m:
            key = (m.group(1), m.group(2))
            if method == "GET":
                if key not in self.rulesets:
                    return fail(10003, "not found")
                rules = [dict(r, id=r.get("id") or self.next_id("rule"), version="1") for r in self.rulesets[key]]
                return ok({"id": "rs", "phase": key[1], "rules": rules})
            if method == "PUT":
                self.rulesets[key] = [{k: v for k, v in r.items() if k not in ("id", "version")} for r in body["rules"]]
                return ok({"rules": body["rules"]})
        m = re.fullmatch(r"/zones/([^/]+)/settings/automatic_platform_optimization", p)
        if m:
            if method == "GET":
                if m.group(1) not in self.apo:
                    return fail(1003, "no APO")
                return ok({"id": "automatic_platform_optimization", "value": copy.deepcopy(self.apo[m.group(1)])})
            if method == "PATCH":
                self.apo[m.group(1)] = copy.deepcopy(body["value"])
                return ok({"value": body["value"]})
        m = re.fullmatch(r"/zones/([^/]+)/purge_cache", p)
        if m and method == "POST":
            self.purges += 1
            return ok({"id": m.group(1)})

        m = re.fullmatch(r"/accounts/([^/]+)/r2/buckets", p)
        if m and method == "POST":
            self.buckets[(m.group(1), body["name"])] = {"domains": [], "cors": None}
            return ok({"name": body["name"]})
        m = re.fullmatch(r"/accounts/([^/]+)/r2/buckets/([^/]+)", p)
        if m and method == "GET":
            return ok({"name": m.group(2)}) if (m.group(1), m.group(2)) in self.buckets else fail(10006, "no bucket")
        m = re.fullmatch(r"/accounts/([^/]+)/r2/buckets/([^/]+)/domains/custom", p)
        if m:
            b = self.buckets[(m.group(1), m.group(2))]
            if method == "GET":
                return ok({"domains": [{"domain": d} for d in b["domains"]]})
            b["domains"].append(body["domain"])
            return ok(body)
        m = re.fullmatch(r"/accounts/([^/]+)/r2/buckets/([^/]+)/cors", p)
        if m:
            b = self.buckets[(m.group(1), m.group(2))]
            if method == "GET":
                return ok(b["cors"]) if b["cors"] else fail(10059, "no cors")
            b["cors"] = copy.deepcopy(body)
            return ok(None)
        if p == "/user/tokens/permission_groups":
            return ok(
                [
                    {"id": "g-write", "name": "Workers R2 Storage Bucket Item Write"},
                    {"id": "g-read", "name": "Workers R2 Storage Bucket Item Read"},
                ]
            )
        m = re.fullmatch(r"/accounts/([^/]+)/tokens", p)
        if m:
            acct = m.group(1)
            if method == "GET":
                return ok(copy.deepcopy(self.tokens.get(acct, [])))
            if method == "POST":
                if not body:
                    return fail(9109, "Unauthorized") if not self.can_mint else fail(400, "bad body")
                tid = self.next_id("tok")
                self.tokens.setdefault(acct, []).append({"id": tid, "name": body["name"], "policies": body["policies"]})
                return ok({"id": tid, "value": f"value-{tid}"})
        m = re.fullmatch(r"/accounts/([^/]+)/tokens/([^/]+)", p)
        if m and method == "DELETE":
            self.tokens[m.group(1)] = [t for t in self.tokens.get(m.group(1), []) if t["id"] != m.group(2)]
            return ok({"id": m.group(2)})
        m = re.fullmatch(r"/accounts/([^/]+)/cfd_tunnel", p)
        if m:
            acct = m.group(1)
            if method == "GET":
                return ok([t for t in self.tunnels.get(acct, []) if t["name"] == q.get("name")])
            tid = self.next_id("tun")
            self.tunnels.setdefault(acct, []).append({"id": tid, "name": body["name"], "secret": body["tunnel_secret"]})
            return ok({"id": tid})
        m = re.fullmatch(r"/accounts/([^/]+)/cfd_tunnel/([^/]+)/token", p)
        if m:
            t = next(t for t in self.tunnels[m.group(1)] if t["id"] == m.group(2))
            raw = json.dumps({"a": m.group(1), "t": t["id"], "s": t["secret"]}).encode()
            return ok(base64.b64encode(raw).decode().rstrip("="))
        m = re.fullmatch(r"/accounts/([^/]+)/cfd_tunnel/([^/]+)", p)
        if m and method == "GET":
            return ok({"id": m.group(2), "status": "healthy", "connections": [{}, {}]})
        m = re.fullmatch(r"/accounts/([^/]+)/access/apps", p)
        if m:
            acct = m.group(1)
            if method == "GET":
                return ok(copy.deepcopy(self.access.get(acct, [])))
            self.access.setdefault(acct, []).append(copy.deepcopy(body))
            return ok(body)
        return fail(7000, f"fake: no route {method} {path}")


@dataclass
class Recorder:
    """A command runner: (argv, stdin) → (rc, stdout bytes, stderr), answered by the
    first matching rule (a substring of the joined argv)."""

    rules: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def on(self, needle, rc=0, out=b"", err=""):
        self.rules.append((needle, rc, out if isinstance(out, bytes) else out.encode(), err))
        return self

    def __call__(self, argv, stdin=None):
        line = " ".join(argv)
        self.calls.append((line, stdin))
        for needle, rc, out, err in self.rules:
            if needle in line:
                return rc, out, err
        return 0, b"", ""

    def ran(self, needle):
        return [c for c, _ in self.calls if needle in c]
