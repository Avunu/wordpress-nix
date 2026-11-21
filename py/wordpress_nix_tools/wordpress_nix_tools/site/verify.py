"""``wordpress-nix verify <slug> --against <ssh-host> --path P [--via-socket]
[--delta-since TS]`` (plan §8.6). Read-only on both sides.

- Per-table row counts, legacy against v3, and the administrators against the
  survey (a new one fails).
- ``wp wp-cloud-files status`` (no local-only attachment) and ``verify --sample 200``.
- ``nix run .#verify-wp-checksums`` when run from the site repository.
- HTTP posture (§7.3): xmlrpc refused, ``/wp-cron.php`` and ``/.env`` not served,
  PHP under ``/app/uploads`` and ``woocommerce_uploads`` 403, ``/wp-admin`` and
  ``/wp-content/uploads`` redirected, media on ``media.<domain>``, a Cwicly CSS 200.
  ``--via-socket`` runs them on the node against the FrankenPHP socket with
  ``Host: <domain>`` (before the DNS flip); otherwise over HTTPS.
- ``--delta-since TS``: the rows legacy gained after the pull (``wp_posts``,
  ``wp_comments``, Fluent Forms submissions, Give donations), to re-enter by hand.
  ``--against`` the v3 side after a rollback lists v3's instead.
"""

import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from wordpress_nix_tools.common import records
from wordpress_nix_tools.common.remote import Remote, RemoteError
from wordpress_nix_tools.site.node import Container, NodeError, find_node

SOCKET = "/run/wordpress/wp.sock"

# Run on the legacy host: SQL through wp-config's credentials (as legacy pull's dump).
LEGACY_SQL_SH = r"""
set -euo pipefail
cd "$1"
opts=$(php -r '
  error_reporting(E_ERROR);
  define("ABSPATH", getcwd() . "/");
  $s = file_get_contents("wp-config.php");
  $s = preg_replace("/^\s*(require|include)(_once)?[^;]*wp-settings\.php[^;]*;/m", "", $s);
  eval("?>" . $s);
  $h = DB_HOST; $port = ""; $sock = "";
  if (strpos($h, ":") !== false) { list($h, $r) = explode(":", $h, 2); if (is_numeric($r)) { $port = $r; } else { $sock = $r; } }
  echo "[client]\nuser=" . DB_USER . "\npassword=\"" . addcslashes(DB_PASSWORD, "\\\"") . "\"\nhost=" . $h . "\n";
  if ($port !== "") { echo "port=$port\n"; }
  if ($sock !== "") { echo "socket=$sock\n"; }
  echo "#db=" . DB_NAME . "\n";
')
db=$(printf '%s\n' "$opts" | sed -n 's/^#db=//p')
exec 3<<<"$opts"
exec mysql --defaults-extra-file=/dev/fd/3 -N -B "$db"
"""

TABLES_SQL = "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' ORDER BY table_name;"


def counts_sql(tables: list[str]) -> str:
    parts = [f"SELECT '{t}', COUNT(*) FROM `{t}`" for t in tables]
    return " UNION ALL ".join(parts) + ";"


def parse_counts(text: str) -> dict[str, int]:
    out = {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) == 2 and cols[1].strip().isdigit():
            out[cols[0]] = int(cols[1])
    return out


@dataclass
class Report:
    ok: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def check(self, good: bool, text: str) -> None:
        (self.ok if good else self.failed).append(text)


SqlRunner = Callable[[str], str]


def legacy_sql(remote: Remote, path: str) -> SqlRunner:
    def run(sql: str) -> str:
        rc, out, err = remote.run(f"bash -c {shlex.quote(LEGACY_SQL_SH)} -- {shlex.quote(path)}", sql.encode())
        if rc != 0:
            raise RemoteError(remote.host, "mysql", rc, err)
        return out.decode(errors="replace")

    return run


def container_sql(c: Container) -> SqlRunner:
    def run(sql: str) -> str:
        rc, out, err = c.wp(["db", "query", sql, "--skip-column-names", "--batch"])
        if rc != 0:
            raise RemoteError(c.node, "wp db query", rc, err)
        return out

    return run


def row_counts(sql: SqlRunner) -> dict[str, int]:
    tables = [t for t in sql(TABLES_SQL).split() if t]
    return parse_counts(sql(counts_sql(tables))) if tables else {}


def compare_counts(report: Report, legacy: dict[str, int], v3: dict[str, int]) -> None:
    for t in sorted(set(legacy) | set(v3)):
        a, b = legacy.get(t), v3.get(t)
        report.check(a == b, f"rows {t}: legacy {a} v3 {b}")


def admins(sql: SqlRunner, prefix: str) -> set[str]:
    out = sql(
        f"SELECT u.user_login FROM `{prefix}users` u JOIN `{prefix}usermeta` m ON m.user_id = u.ID "
        f"AND m.meta_key = '{prefix}capabilities' WHERE m.meta_value LIKE '%\"administrator\"%';"
    )
    return {x for x in out.split() if x}


DELTA_TABLES = {
    "posts": ("post_modified_gmt", "ID"),
    "comments": ("comment_date_gmt", "comment_ID"),
    "fluentform_submissions": ("created_at", "id"),
    "give_donationmeta": (None, "meta_id"),
}


def delta_sql(prefix: str, since: str) -> str:
    parts = []
    for table, (col, idcol) in DELTA_TABLES.items():
        t = f"{prefix}{table}"
        where = f"WHERE `{col}` > '{since}'" if col else ""
        parts.append(f"SELECT '{t}', COUNT(*), COALESCE(MAX(`{idcol}`), 0) FROM `{t}` {where}")
    return " UNION ALL ".join(parts) + ";"


Http = Callable[[str, str], tuple[int, str, str]]  # (url, host) → (status, location, body)


def http_checks(report: Report, http: Http, base: str, domain: str, *, cwicly_css: str = "") -> None:
    def status(path: str) -> tuple[int, str, str]:
        return http(base + path, domain)

    code, _, body = status("/")
    report.check(code == 200, f"GET / → {code}")
    report.check(f"//media.{domain}/" in body or "wp-content/uploads" not in body, "homepage media on media.<domain>")
    for path, want in (
        ("/xmlrpc.php", (403, 404)),
        ("/wp-cron.php", (403, 404)),
        ("/.env", (403, 404)),
        ("/app/uploads/2024/01/wordpress-nix-probe.php", (403,)),
        ("/app/uploads/woocommerce_uploads/wordpress-nix-probe.zip", (403,)),
    ):
        code, _, _ = status(path)
        report.check(code in want, f"GET {path} → {code} (want {'/'.join(map(str, want))})")
    code, loc, _ = status("/wp-admin/")
    report.check(code in (301, 308) and "/wp/wp-admin" in loc, f"GET /wp-admin/ → {code} {loc}")
    code, loc, _ = status("/wp-content/uploads/2024/01/wordpress-nix-probe.jpg")
    report.check(code == 301 and f"media.{domain}" in loc, f"GET /wp-content/uploads/… → {code} {loc}")
    if cwicly_css:
        code, _, _ = status(f"/app/uploads/{cwicly_css}")
        report.check(code == 200, f"GET /app/uploads/{cwicly_css} → {code}")


def curl_http(runner: Callable[[list[str]], tuple[int, str]]) -> Http:
    def get(url: str, host: str) -> tuple[int, str, str]:
        rc, out = runner(["curl", "-s", "-o", "-", "-w", "\n%{http_code} %{redirect_url}", "-H", f"Host: {host}", url])
        body, _, tail = out.rpartition("\n")
        code, _, loc = tail.partition(" ")
        return (int(code) if code.isdigit() else 0), loc, body

    return get


def run_verify(args, legacy: SqlRunner, v3: SqlRunner, c: Container, http: Http, log=print) -> int:
    report = Report()
    root = Path(args.site_root)
    survey = records.read(records.find(root, "survey.json")) if records.find(root, "survey.json") else {}
    prefix = args.prefix or (survey.get("wordpress") or {}).get("tablePrefix") or "wp_"

    if args.delta_since:
        out = legacy(delta_sql(prefix, args.delta_since))
        for line in out.splitlines():
            cols = line.split("\t")
            if len(cols) == 3:
                report.check(cols[1] == "0", f"{cols[0]}: {cols[1]} row(s) since {args.delta_since} (max id {cols[2]})")
        return finish(report, log)

    compare_counts(report, row_counts(legacy), row_counts(v3))
    confirmed = {a.get("login") for a in survey.get("administrators") or []}
    if confirmed:
        new = admins(v3, prefix) - confirmed
        report.check(not new, f"administrators not in the survey: {', '.join(sorted(new)) or 'none'}")
    rc, out, _ = c.wp(["wp-cloud-files", "status", "--format=json"])
    if rc == 0:
        try:
            local_only = json.loads(out).get("local_only", json.loads(out).get("localOnly", 0))
        except (ValueError, AttributeError):
            local_only = "?"
        report.check(local_only == 0, f"wp-cloud-files local-only attachments: {local_only}")
        rc, _, _ = c.wp(["wp-cloud-files", "verify", "--sample=200"])
        report.check(rc == 0, "wp-cloud-files verify --sample=200")
    else:
        report.notes.append("wp-cloud-files status unavailable (plugin not active?)")
    if (root / "flake.nix").is_file() and not args.skip_checksums:
        rc = subprocess.run(["nix", "run", ".#verify-wp-checksums"], cwd=root, check=False).returncode
        report.check(rc == 0, "verify-wp-checksums")
    site = {}
    try:
        site = json.loads((root / ".github" / "wordpress-nix.json").read_text())
    except (OSError, ValueError):
        pass
    domain = urlparse(args.home or site.get("home") or "").hostname or ""
    if domain:
        inv = records.read(records.find(root, "inventory.json")) if records.find(root, "inventory.json") else {}
        css = "cwicly/" if "cwicly" in (inv.get("localDirs") or []) else ""
        base = "http://localhost" if args.via_socket else f"https://{domain}"
        http_checks(report, http, base, domain, cwicly_css=args.cwicly_css or "")
        if css and not args.cwicly_css:
            report.notes.append("pass --cwicly-css <path under uploads> to check a Cwicly stylesheet")
    else:
        report.notes.append("no home URL: HTTP checks skipped")
    return finish(report, log)


def finish(report: Report, log) -> int:
    for line in report.ok:
        log(f"  ✓ {line}")
    for line in report.failed:
        log(f"  ✗ {line}")
    for line in report.notes:
        log(f"  · {line}")
    log(f"{len(report.ok)} ok, {len(report.failed)} failed")
    return 1 if report.failed else 0


def cmd(args, runner=None) -> int:
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    kw = {"runner": runner} if runner else {}
    try:
        node = find_node(args.slug, args.node, **kw)
        c = Container(args.slug, node, **kw)
        legacy = legacy_sql(Remote(args.against, **kw), args.path)
        if args.via_socket:

            def on_node(argv: list[str]) -> tuple[int, str]:
                argv = [*argv[:-1], "--unix-socket", SOCKET, argv[-1]]
                rc, out, _ = c.run(argv, user=None)
                return rc, out.decode(errors="replace")

            http = curl_http(on_node)
        else:

            def local(argv: list[str]) -> tuple[int, str]:
                p = subprocess.run(argv, capture_output=True, text=True, check=False)
                return p.returncode, p.stdout

            http = curl_http(local)
        return run_verify(args, legacy, container_sql(c), c, http, log=log)
    except (NodeError, RemoteError) as e:
        log(f"verify: {e}")
        return 3
