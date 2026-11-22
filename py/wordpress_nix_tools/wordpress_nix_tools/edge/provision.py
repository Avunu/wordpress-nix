"""``wordpress-nix edge provision <slug> --domain D [--access]`` (plan §8.6, D7).

In the zone's own account (a CNAME to ``<id>.cfargotunnel.com`` resolves only for a
tunnel in the zone's account):

- the tunnel ``<cluster>-<slug>``, found by name or created with a secret chosen
  here (no ``cloudflared login``); its id and credentials JSON are printed as the
  ``<slug>-cf/cloudflared`` fragment ``avc secrets provision`` takes;
- the proxied ``staging.<domain>`` CNAME to it;
- the WAF login rate limit (``/wp/wp-login.php``, ``/wp-login.php``) and an
  ``xmlrpc.php`` block;
- the Cache Rules: long TTLs on ``/app/themes/*``, ``/app/plugins/*`` and
  ``/wp/wp-includes/*``, and anonymous HTML with a cookie bypass, created
  *disabled* (the zone still serves the legacy site; ``cutover`` enables it);
- with ``--access``, path-scoped Access on ``/wp/wp-admin`` and ``/wp/wp-login.php``
  with bypasses for ``admin-ajax.php``, ``admin-post.php``, ``load-styles.php`` and
  ``load-scripts.php``.

Each rule carries a ``ref`` (``wordpress-nix-…``) and is upserted in place, so a
rerun changes nothing. It refuses a zone not in a full setup, or one the token
cannot reach.
"""

import base64
import json
import os
import secrets
import sys

from wordpress_nix_tools.common.cloudflare import Cloudflare, CloudflareError, Zone

REF_RATELIMIT = "wordpress-nix-login-ratelimit"
REF_XMLRPC = "wordpress-nix-xmlrpc"
REF_STATIC = "wordpress-nix-static-cache"
REF_HTML = "wordpress-nix-html-cache"
BYPASS_COOKIES = ("wordpress_logged_in_", "wp-postpass_", "woocommerce_", "comment_author_", "wordpress_sec_")
ACCESS_BYPASS = ("admin-ajax.php", "admin-post.php", "load-styles.php", "load-scripts.php")


def tunnel_name(cluster: str, slug: str) -> str:
    return f"{cluster}-{slug}"


def ensure_tunnel(cf: Cloudflare, account: str, name: str) -> tuple[str, str]:
    env = cf.api("GET", f"/accounts/{account}/cfd_tunnel?name={name}&is_deleted=false")
    found = (env.get("result") or []) if env.get("success") else []
    if found:
        return found[0]["id"], "exists"
    secret = base64.b64encode(secrets.token_bytes(32)).decode()
    r = cf.ok(
        "POST",
        f"/accounts/{account}/cfd_tunnel",
        {"name": name, "config_src": "local", "tunnel_secret": secret},
        context=f"creating the tunnel {name}",
    )
    return r["id"], "created"


def tunnel_credentials(cf: Cloudflare, account: str, tunnel: str) -> dict:
    """cloudflared's credentials file, rebuilt from the tunnel's token (moonshot's
    provision_plane_tunnel): kept nowhere but the secret it is provisioned into."""
    token = cf.ok("GET", f"/accounts/{account}/cfd_tunnel/{tunnel}/token", context="reading the tunnel token")
    raw = json.loads(base64.b64decode(token + "=" * (-len(token) % 4)))
    return {"AccountTag": raw["a"], "TunnelID": raw["t"], "TunnelSecret": raw["s"]}


def hosts_expr(domain: str) -> str:
    return f'http.host in {{"{domain}" "www.{domain}" "staging.{domain}"}}'


def desired_rules(domain: str) -> dict[str, list[dict]]:
    hosts = hosts_expr(domain)
    login = '(http.request.uri.path in {"/wp/wp-login.php" "/wp-login.php"} and http.request.method eq "POST")'
    no_cookie = " and ".join(f'not http.cookie contains "{c}"' for c in BYPASS_COOKIES)
    return {
        "http_ratelimit": [
            {
                "ref": REF_RATELIMIT,
                "description": "wordpress-nix: login rate limit",
                "expression": f"({hosts}) and {login}",
                "action": "block",
                "ratelimit": {
                    "characteristics": ["ip.src", "cf.colo.id"],
                    "period": 60,
                    "requests_per_period": 10,
                    "mitigation_timeout": 600,
                },
                "enabled": True,
            }
        ],
        "http_request_firewall_custom": [
            {
                "ref": REF_XMLRPC,
                "description": "wordpress-nix: xmlrpc.php is not served",
                "expression": f'({hosts}) and http.request.uri.path in {{"/xmlrpc.php" "/wp/xmlrpc.php"}}',
                "action": "block",
                "enabled": True,
            }
        ],
        "http_request_cache_settings": [
            {
                "ref": REF_STATIC,
                "description": "wordpress-nix: long TTLs on versioned assets",
                "expression": (
                    f'({hosts}) and (starts_with(http.request.uri.path, "/app/themes/") or '
                    'starts_with(http.request.uri.path, "/app/plugins/") or '
                    'starts_with(http.request.uri.path, "/wp/wp-includes/"))'
                ),
                "action": "set_cache_settings",
                "action_parameters": {
                    "cache": True,
                    "edge_ttl": {"mode": "override_origin", "default": 2592000},
                    "browser_ttl": {"mode": "override_origin", "default": 604800},
                },
                "enabled": True,
            },
            {
                "ref": REF_HTML,
                "description": "wordpress-nix: anonymous HTML (enabled at cutover)",
                "expression": (
                    f'(http.host in {{"{domain}" "www.{domain}"}}) and http.request.method in {{"GET" "HEAD"}} and '
                    'not starts_with(http.request.uri.path, "/wp/") and '
                    'not starts_with(http.request.uri.path, "/wp-json/") and '
                    'not http.request.uri.path contains "/wp-admin" and '
                    f"{no_cookie}"
                ),
                "action": "set_cache_settings",
                "action_parameters": {
                    "cache": True,
                    "edge_ttl": {"mode": "override_origin", "default": 600},
                    "browser_ttl": {"mode": "respect_origin"},
                },
                "enabled": False,
            },
        ],
    }


READ_ONLY = ("id", "version", "last_updated", "logging")


def clean_rule(rule: dict) -> dict:
    return {k: v for k, v in rule.items() if k not in READ_ONLY}


def entrypoint(cf: Cloudflare, zone: Zone, phase: str) -> dict | None:
    env = cf.api("GET", f"/zones/{zone.id}/rulesets/phases/{phase}/entrypoint")
    return env.get("result") if env.get("success") else None


def upsert_rules(cf: Cloudflare, zone: Zone, phase: str, wanted: list[dict], *, keep_enabled: bool = True) -> str:
    """Our rules (by ref) in the phase's entrypoint, the zone's own left as they are.
    An existing rule of ours keeps its ``enabled`` (cutover owns the HTML rule's)."""
    current = entrypoint(cf, zone, phase)
    rules = [clean_rule(r) for r in (current or {}).get("rules") or []]
    by_ref = {r.get("ref"): i for i, r in enumerate(rules) if r.get("ref")}
    changed = False
    for w in wanted:
        if w["ref"] in by_ref:
            i = by_ref[w["ref"]]
            merged = dict(w)
            if keep_enabled and "enabled" in rules[i]:
                merged["enabled"] = rules[i]["enabled"]
            if rules[i] != merged:
                rules[i] = merged
                changed = True
        else:
            rules.append(dict(w))
            changed = True
    if not changed:
        return "unchanged"
    cf.ok("PUT", f"/zones/{zone.id}/rulesets/phases/{phase}/entrypoint", {"rules": rules}, context=f"updating {phase}")
    return "updated" if current else "created"


def ensure_access(cf: Cloudflare, account: str, slug: str, domain: str, emails: list[str]) -> list[str]:
    if not emails:
        raise CloudflareError("--access needs --access-emails (addresses, or @domain for a whole domain)", {})
    include = [{"email_domain": {"domain": e[1:]}} if e.startswith("@") else {"email": {"email": e}} for e in emails]
    env = cf.api("GET", f"/accounts/{account}/access/apps?per_page=100")
    existing = {a.get("domain"): a for a in (env.get("result") or [])} if env.get("success") else {}
    done = []
    apps = [
        (
            f"{domain}/wp/wp-admin",
            {
                "name": f"{slug} admin",
                "type": "self_hosted",
                "domain": f"{domain}/wp/wp-admin",
                "self_hosted_domains": [f"{domain}/wp/wp-admin", f"{domain}/wp/wp-login.php"],
                "session_duration": "24h",
                "policies": [{"name": "Allow staff", "decision": "allow", "include": include}],
            },
        )
    ]
    for path in ACCESS_BYPASS:
        apps.append(
            (
                f"{domain}/wp/wp-admin/{path}",
                {
                    "name": f"{slug} admin bypass {path}",
                    "type": "self_hosted",
                    "domain": f"{domain}/wp/wp-admin/{path}",
                    "policies": [{"name": "Bypass", "decision": "bypass", "include": [{"everyone": {}}]}],
                },
            )
        )
    for key, body in apps:
        if key in existing:
            done.append(f"{key}: exists")
            continue
        cf.ok("POST", f"/accounts/{account}/access/apps", body, context=f"creating Access for {key}")
        done.append(f"{key}: created")
    return done


def provision(
    cf: Cloudflare, slug: str, domain: str, *, cluster: str, access: bool, emails: list[str], log=print
) -> dict:
    zone = cf.require_full_zone(domain)
    account = zone.account
    name = tunnel_name(cluster, slug)
    tunnel, state = ensure_tunnel(cf, account, name)
    log(f"tunnel {name}: {state} ({tunnel})")
    creds = tunnel_credentials(cf, account, tunnel)
    log(f"staging.{domain}: {cf.cname(f'staging.{domain}', f'{tunnel}.cfargotunnel.com')}")
    for phase, rules in desired_rules(domain).items():
        log(f"{phase}: {upsert_rules(cf, zone, phase, rules)}")
    out = {
        "tunnel": {"id": tunnel, "name": name, "accountId": account},
        "zone": {"id": zone.id, "name": zone.name},
        "secrets": {f"{slug}-cf": {"cloudflared": creds}},
    }
    if access:
        out["access"] = ensure_access(cf, account, slug, domain, emails)
        for line in out["access"]:
            log(f"access {line}")
    return out


def cmd(args, cf: Cloudflare | None = None) -> int:
    try:
        cf = cf or Cloudflare()
        emails = [e.strip() for e in (args.access_emails or "").split(",") if e.strip()]
        result = provision(
            cf,
            args.slug,
            args.domain,
            cluster=args.cluster or os.environ.get("WORDPRESS_NIX_CLUSTER", "ovh-01"),
            access=args.access,
            emails=emails,
            log=lambda m: print(m, file=sys.stderr),
        )
    except CloudflareError as e:
        print(f"edge provision: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    print(
        f'\nIn cluster-ovh-01: tunnel.id = "{result["tunnel"]["id"]}"; tunnel.accountId = '
        f'"{result["tunnel"]["accountId"]}"; provision {args.slug}-cf/cloudflared with the JSON above.',
        file=sys.stderr,
    )
    return 0
