"""``wordpress-nix``: standards-core's commands (``ci``, ``repo``, ``sync``, …) with
wordpress-nix's product, and the conversion commands (plan §8.1).

    wordpress-nix legacy survey|pull       read-only on the legacy host
    wordpress-nix db audit|rewrite-urls    the dump, and the database behind `wp`
    wordpress-nix media provision|offload  the site's R2 bucket
    wordpress-nix edge provision           tunnel, staging CNAME, WAF, cache, Access
    wordpress-nix repo adopt               D6: a fresh repository or a pull request
    wordpress-nix site import|status       the site on the cluster
    wordpress-nix verify                   legacy against v3
    wordpress-nix cutover [--rollback]     the DNS flip, and its exact undo
    wordpress-nix audit                    Wordfence Intelligence against composer.lock

Exit codes are standards-core's: 0 ok; 1 a refusal or a failed gate; 2 a bad
argument; 3 a GitHub, git, SSH or API error.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from standards_core.commands import Command


def _run(module: str, attr: str = "cmd"):
    def run(args: argparse.Namespace) -> int:
        import importlib

        return getattr(importlib.import_module(f"wordpress_nix_tools.{module}"), attr)(args)

    return run


def _group(subparsers, name: str, help: str) -> argparse._SubParsersAction:
    p = subparsers.add_parser(name, help=help, description=help)
    return p.add_subparsers(dest=f"{name}_command", metavar="<subcommand>", required=True)


def register_legacy(subparsers, product) -> None:
    sub = _group(subparsers, "legacy", "survey and pull a legacy site, read-only")
    p = sub.add_parser("survey", help="what the legacy site is (JSON or CSV)")
    p.add_argument("host", help="the legacy host (ssh destination)")
    p.add_argument("--path", help="the site's directory on the host")
    p.add_argument("--root", default="/var/www", help="--fleet: where to look for sites")
    p.add_argument("--fleet", action="store_true", help="every site under --root, one row each")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--json", action="store_true", help="JSON (the default)")
    out.add_argument("--csv", action="store_true", help="the fleet inventory CSV")
    p.add_argument("--compare", metavar="SURVEY", help="exit 1 on any change since this survey (§13.2)")
    p.set_defaults(func=_run("legacy.survey"))
    p = sub.add_parser("pull", help="code, full dump and survey, in one SSH session")
    p.add_argument("host")
    p.add_argument("--path", required=True)
    p.add_argument("--into", required=True, metavar="DIR")
    p.add_argument("--db-only", action="store_true", help="the dump and survey only (cutover day)")
    p.add_argument("--age-recipient", help="encrypt the evidence dump to this age recipient or recipients file")
    p.set_defaults(func=_run("legacy.pull"))


def register_db(subparsers, product) -> None:
    sub = _group(subparsers, "db", "audit a dump; rewrite URLs in a database")
    p = sub.add_parser("audit", help="refuse or strip what would execute, and report the rest")
    p.add_argument("dump")
    p.add_argument("--survey", help="survey.json: cross-check counts and administrators")
    p.add_argument("--strip", action="store_true")
    p.add_argument("--out", help="where --strip writes")
    p.add_argument("--accept", action="append", metavar="ID", help="acknowledge a finding (repeatable)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_run("db.audit"))
    p = sub.add_parser("rewrite-urls", help="the §4.9 URL rewrite, through `wp` on PATH")
    p.add_argument("--from-home", required=True, help="the legacy home URL")
    p.add_argument("--to-home", default="", help="the new home, when it differs (staging)")
    p.add_argument("--media-url", default="", help="default: https://media.<domain>")
    p.add_argument("--exclude-prefix-from", metavar="INVENTORY", help="media offload's inventory.json")
    p.add_argument("--exclude-prefix", action="append", help="another uploads prefix to keep local")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--report-changes", action="store_true", help="per-table counts")
    p.add_argument("--print", action="store_true", help="print the steps and run nothing")
    p.set_defaults(func=_run("db.rewrite_urls"))


def register_media(subparsers, product) -> None:
    sub = _group(subparsers, "media", "the site's R2 bucket")
    p = sub.add_parser("provision", help="bucket, media.<domain>, CORS and bucket-scoped keys")
    p.add_argument("slug")
    p.add_argument("--domain", required=True)
    p.add_argument("--adopt", metavar="BUCKET", help="use this existing bucket")
    p.add_argument("--rotate", action="store_true", help="mint new keys, then revoke the old")
    p.add_argument("--no-probe", action="store_true", help="skip the put/get/delete and CORS probe")
    p.set_defaults(func=_run("media.provision"))
    p = sub.add_parser("offload", help="attachments to R2, the rest into a JuiceFS tarball")
    p.add_argument("slug")
    p.add_argument("--from", dest="from_host", required=True, metavar="HOST")
    p.add_argument("--path", required=True)
    p.add_argument("--dump", required=True, help="the audited dump")
    p.add_argument("--adopt", action="store_true")
    p.add_argument("--bucket")
    p.add_argument("--prefix", help="the table prefix (default: from the dump)")
    p.add_argument("--credentials", help="media provision's output (the <slug>-cloud-files fragment)")
    p.add_argument("--site-root", default=".")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=_run("media.offload"))


def register_edge(subparsers, product) -> None:
    sub = _group(subparsers, "edge", "the site's Cloudflare edge, in the zone's account")
    p = sub.add_parser("provision", help="tunnel, staging CNAME, WAF, cache rules, Access")
    p.add_argument("slug")
    p.add_argument("--domain", required=True)
    p.add_argument("--cluster", help="the tunnel's name prefix (default: WORDPRESS_NIX_CLUSTER or ovh-01)")
    p.add_argument("--access", action="store_true", help="path-scoped Access on the admin")
    p.add_argument("--access-emails", help="comma-separated; @domain for a whole domain")
    p.set_defaults(func=_run("edge.provision"))


def register_site(subparsers, product) -> None:
    sub = _group(subparsers, "site", "the site on the cluster")
    p = sub.add_parser("import", help="the dump and the uploads tarball into the container")
    p.add_argument("slug")
    p.add_argument("--dump", required=True)
    p.add_argument("--files", help="media offload's private.tar.gz")
    p.add_argument("--home", required=True, help="the home URL to rewrite to")
    p.add_argument("--from-home", help="the legacy home (default: .github/wordpress-nix.json)")
    p.add_argument("--media-url", default="")
    p.add_argument("--inventory", default=".wordpress-nix-backup/inventory.json")
    p.add_argument("--replace", action="store_true", help="replace an installed site (cluster-backup first)")
    p.add_argument("--node")
    p.add_argument("--site-json", default=".github/wordpress-nix.json")
    p.set_defaults(func=_run("site.importer"))
    p = sub.add_parser("status", help="read-only probes")
    p.add_argument("slug", nargs="?")
    p.add_argument("--domain")
    p.add_argument("--json", action="store_true")
    p.add_argument("--site-root", default=".")
    p.set_defaults(func=_run("site.status"))


def register_verify(subparsers, product) -> None:
    p = subparsers.add_parser("verify", help="legacy against v3: rows, administrators, media, checksums, HTTP")
    p.add_argument("slug")
    p.add_argument("--against", required=True, metavar="HOST", help="the legacy host")
    p.add_argument("--path", required=True)
    p.add_argument("--via-socket", action="store_true", help="HTTP checks through the FrankenPHP socket")
    p.add_argument("--delta-since", metavar="TS", help="rows legacy gained after TS")
    p.add_argument("--home")
    p.add_argument("--prefix")
    p.add_argument("--node")
    p.add_argument("--cwicly-css", help="a Cwicly stylesheet under uploads to fetch")
    p.add_argument("--skip-checksums", action="store_true")
    p.add_argument("--site-root", default=".")
    p.set_defaults(func=_run("site.verify"))


def register_cutover(subparsers, product) -> None:
    p = subparsers.add_parser("cutover", help="snapshot, disable legacy caching, flip DNS, purge (--rollback undoes)")
    p.add_argument("slug")
    p.add_argument("--domain")
    p.add_argument("--rollback", action="store_true")
    p.add_argument("--tunnel", help="the tunnel id (default: found by name)")
    p.add_argument("--cluster")
    p.add_argument("--site-root", default=".")
    p.set_defaults(func=_run("site.cutover"))


def register_audit(subparsers, product) -> None:
    from wordpress_nix_tools import audit

    p = subparsers.add_parser("audit", help="Wordfence Intelligence against composer.lock and committed code")
    audit.add_arguments(p)
    p.set_defaults(func=_run("audit", "run"))


def register_repo_adopt(subparsers, product) -> None:
    p = subparsers.add_parser("repo-adopt", help="`repo adopt`: a fresh repository or a pull request (D6)")
    p.add_argument("slug")
    p.add_argument("--org", default="Avunu")
    p.add_argument("--check", action="store_true", help="print the decision, change nothing")
    p.add_argument("--branch", default="main")
    p.add_argument("--dir", default=".")
    p.set_defaults(func=_run("repo_adopt"))


EXTRA = (
    Command("legacy", register_legacy),
    Command("db", register_db),
    Command("media", register_media),
    Command("edge", register_edge),
    Command("site", register_site),
    Command("verify", register_verify),
    Command("cutover", register_cutover),
    Command("audit", register_audit),
    Command("repo-adopt", register_repo_adopt, "repo"),
)


def main(argv: Sequence[str] | None = None) -> int:
    from standards_core import cli

    from wordpress_nix_tools.product import PRODUCT

    args = list(sys.argv[1:] if argv is None else argv)
    # `repo adopt` lives beside standards-core's `repo apply|doctor|…`.
    if args[:2] == ["repo", "adopt"]:
        args = ["repo-adopt", *args[2:]]
    return cli.main(PRODUCT, args, extra_commands=EXTRA)
