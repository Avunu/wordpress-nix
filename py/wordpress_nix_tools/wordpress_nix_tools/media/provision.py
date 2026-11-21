"""``wordpress-nix media provision <slug> --domain D [--adopt BUCKET]`` (plan §4.13).

In the zone's account (D7): the R2 bucket ``wp-<slug>-media`` (created, or adopted
as it is), its custom domain ``media.<domain>``, CORS (PUT/GET/HEAD from
``https://<domain>``, ``https://www.<domain>`` and ``https://staging.<domain>``), an
account-owned bucket-scoped read-write key for the site and a read-only key for the
OVH mirror. Ported from moonshot's provision.sh: R2 S3 credentials are an API token
(the access key id is the token's id, the secret the SHA-256 of its value, shown
once), minted account-owned when the token may (the 9d83999 probe), and a rotation
mints first and revokes after.

No state file: an existing key (by name) is never re-minted, since its secret cannot
be read back; ``--rotate`` mints a new one and revokes the old. The secrets are
printed once, as the fragments ``avc secrets provision`` takes, and never stored.
The probe (put/get/delete and a CORS preflight) runs against the new key.
"""

import hashlib
import json
import sys
import time
from collections.abc import Callable

from wordpress_nix_tools.common.cloudflare import Cloudflare, CloudflareError

RW_GROUP = "Workers R2 Storage Bucket Item Write"
RO_GROUP = "Workers R2 Storage Bucket Item Read"


def bucket_name(slug: str) -> str:
    return f"wp-{slug}-media"


def cors_rules(domain: str) -> dict:
    origins = [f"https://{domain}", f"https://www.{domain}", f"https://staging.{domain}"]
    return {
        "rules": [
            {
                "allowed": {"origins": origins, "methods": ["GET", "PUT", "HEAD"], "headers": ["*"]},
                "exposeHeaders": ["ETag"],
                "maxAgeSeconds": 3600,
            }
        ]
    }


def ensure_bucket(cf: Cloudflare, account: str, bucket: str, adopt: bool) -> str:
    env = cf.api("GET", f"/accounts/{account}/r2/buckets/{bucket}")
    if env.get("success"):
        return "exists"
    if adopt:
        raise CloudflareError(f"--adopt {bucket}: no such bucket in account {account}", env)
    cf.ok("POST", f"/accounts/{account}/r2/buckets", {"name": bucket}, context="creating the R2 bucket")
    return "created"


def ensure_domain(cf: Cloudflare, account: str, bucket: str, host: str, zone_id: str) -> str:
    env = cf.api("GET", f"/accounts/{account}/r2/buckets/{bucket}/domains/custom")
    domains = ((env.get("result") or {}).get("domains") or []) if env.get("success") else []
    if any(d.get("domain") == host for d in domains):
        return "exists"
    cf.ok(
        "POST",
        f"/accounts/{account}/r2/buckets/{bucket}/domains/custom",
        {"domain": host, "zoneId": zone_id, "enabled": True},
        context=f"attaching {host}",
    )
    return "created"


def ensure_cors(cf: Cloudflare, account: str, bucket: str, domain: str) -> str:
    want = cors_rules(domain)
    env = cf.api("GET", f"/accounts/{account}/r2/buckets/{bucket}/cors")
    if env.get("success") and (env.get("result") or {}).get("rules") == want["rules"]:
        return "unchanged"
    cf.ok("PUT", f"/accounts/{account}/r2/buckets/{bucket}/cors", want, context="setting CORS")
    return "set"


def token_body(name: str, account: str, bucket: str, group: str) -> dict:
    return {
        "name": name,
        "policies": [
            {
                "effect": "allow",
                "resources": {f"com.cloudflare.edge.r2.bucket.{account}_default_{bucket}": "*"},
                "permission_groups": [{"id": group}],
            }
        ],
    }


def find_token(cf: Cloudflare, account: str, name: str) -> dict | None:
    env = cf.api("GET", f"/accounts/{account}/tokens?per_page=100")
    for t in (env.get("result") or []) if env.get("success") else []:
        if t.get("name") == name:
            return t
    return None


def mint(cf: Cloudflare, account: str, bucket: str, name: str, group_name: str) -> dict:
    """An account-owned bucket-scoped token, as S3 credentials."""
    if not cf.can_mint_account_token(account):
        raise CloudflareError(
            f"the provisioning token cannot mint account-owned tokens in {account} "
            "(Account → API Tokens:Edit); a user-owned key would die with its operator",
            {},
        )
    group = cf.permission_group(group_name)
    result = cf.ok(
        "POST", f"/accounts/{account}/tokens", token_body(name, account, bucket, group), context=f"minting {name}"
    )
    return {
        "tokenId": result["id"],
        "S3_KEY": result["id"],
        "S3_SECRET": hashlib.sha256(result["value"].encode()).hexdigest(),
    }


def revoke(cf: Cloudflare, account: str, token_id: str) -> None:
    cf.ok("DELETE", f"/accounts/{account}/tokens/{token_id}", context=f"revoking {token_id}")


Probe = Callable[[dict, str], list[str]]


def no_probe(creds: dict, origin: str) -> list[str]:
    return []


def provision(
    cf: Cloudflare,
    slug: str,
    domain: str,
    *,
    adopt: str = "",
    rotate: bool = False,
    probe: Probe = no_probe,
    log=print,
) -> dict:
    zone = cf.require_full_zone(domain)
    account = zone.account
    bucket = adopt or bucket_name(slug)
    media_host = f"media.{domain}"
    log(f"bucket {bucket}: {ensure_bucket(cf, account, bucket, bool(adopt))}")
    log(f"custom domain {media_host}: {ensure_domain(cf, account, bucket, media_host, zone.id)}")
    log(f"CORS: {ensure_cors(cf, account, bucket, domain)}")
    endpoint = f"https://{account}.r2.cloudflarestorage.com"
    common = {
        "S3_BUCKET": bucket,
        "S3_ENDPOINT": endpoint,
        "S3_PUBLIC_URL": f"https://{media_host}",
        "S3_REGION": "auto",
    }
    fragments: dict = {}
    for suffix, group, secret in (
        ("media", RW_GROUP, f"{slug}-cloud-files"),
        ("media-ro", RO_GROUP, f"{slug}-media-ro"),
    ):
        name = f"wordpress-nix-{slug}-{suffix}"
        existing = find_token(cf, account, name)
        if existing and not rotate:
            log(f"key {name} exists ({existing.get('id')}); its secret is not readable: --rotate mints a new one")
            continue
        creds = mint(cf, account, bucket, name, group)
        if suffix == "media":
            # A new token can take a few seconds to reach R2.
            problems = probe({**common, **creds}, f"https://{domain}")
            if problems:
                revoke(cf, account, creds["tokenId"])
                raise CloudflareError(f"the new key failed its probe ({'; '.join(problems)}); it was revoked", {})
        if existing:
            revoke(cf, account, existing["id"])
            log(f"revoked the previous {name} ({existing['id']})")
        fragments[secret] = {**common, "S3_KEY": creds["S3_KEY"], "S3_SECRET": creds["S3_SECRET"]}
        log(f"minted {name} ({creds['tokenId']}, account-owned)")
    return {"bucket": bucket, "account": account, "endpoint": endpoint, "mediaHost": media_host, "secrets": fragments}


def rclone_probe(creds: dict, origin: str, runner=None, http=None) -> list[str]:
    """put/get/delete through rclone with the new key, and a CORS preflight."""
    import os
    import subprocess
    import urllib.request

    env = dict(os.environ)
    env.update(
        {
            "RCLONE_CONFIG_PROBE_TYPE": "s3",
            "RCLONE_CONFIG_PROBE_PROVIDER": "Cloudflare",
            "RCLONE_CONFIG_PROBE_ACCESS_KEY_ID": creds["S3_KEY"],
            "RCLONE_CONFIG_PROBE_SECRET_ACCESS_KEY": creds["S3_SECRET"],
            "RCLONE_CONFIG_PROBE_ENDPOINT": creds["S3_ENDPOINT"],
            "RCLONE_CONFIG_PROBE_NO_CHECK_BUCKET": "true",
        }
    )
    run = runner or (
        lambda argv, stdin=None: subprocess.run(argv, input=stdin, capture_output=True, env=env, check=False)
    )
    key = f"probe:{creds['S3_BUCKET']}/.wordpress-nix-probe"
    problems = []
    for _attempt in range(6):
        p = run(["rclone", "rcat", key], b"probe")
        if p.returncode == 0:
            break
        time.sleep(5)
    else:
        return ["put failed"]
    p = run(["rclone", "cat", key])
    if p.returncode != 0 or p.stdout != b"probe":
        problems.append("get failed")
    if run(["rclone", "deletefile", key]).returncode != 0:
        problems.append("delete failed")
    req = urllib.request.Request(
        f"{creds['S3_ENDPOINT']}/{creds['S3_BUCKET']}/.wordpress-nix-probe",
        method="OPTIONS",
        headers={"Origin": origin, "Access-Control-Request-Method": "PUT"},
    )
    try:
        with (http or urllib.request.urlopen)(req, timeout=30) as r:
            allowed = r.headers.get("Access-Control-Allow-Origin", "")
    except Exception as e:  # noqa: BLE001
        allowed = ""
        problems.append(f"CORS preflight failed ({e})")
    if allowed not in (origin, "*") and not any(p.startswith("CORS") for p in problems):
        problems.append(f"CORS preflight did not allow {origin}")
    return problems


def cmd(args, cf: Cloudflare | None = None, probe: Probe | None = None) -> int:
    try:
        cf = cf or Cloudflare()
        result = provision(
            cf,
            args.slug,
            args.domain,
            adopt=args.adopt or "",
            rotate=args.rotate,
            probe=probe or (no_probe if args.no_probe else rclone_probe),
            log=lambda m: print(m, file=sys.stderr),
        )
    except CloudflareError as e:
        print(f"media provision: {e}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    if result["secrets"]:
        print(
            "\nThese secrets are shown once. Provision them now (cluster-ovh-01: `avc secrets provision`)"
            f" as {', '.join(result['secrets'])}.",
            file=sys.stderr,
        )
    return 0
