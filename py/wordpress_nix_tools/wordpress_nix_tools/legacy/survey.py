"""``wordpress-nix legacy survey``: what a legacy site is, read over SSH (§8.2).

Read-only by construction: one PHP script on the legacy host's own ``php`` reads
wp-config.php's constants (``require …wp-settings.php`` removed, as moonshot's
survey did), opens the database with them and runs SELECTs only, and walks
wp-content. When wp-cli is there, ``wp core|plugin verify-checksums`` add the
checksum columns. Nothing is written on the host: no temporary file, no option.

The zone's Cloudflare status (full setup, account, token reach, Page/Cache/Transform
rules, APO) comes from the API with ``WORDPRESS_NIX_CLOUDFLARE_TOKEN`` when set.
"""

import csv
import io
import json
import shlex
import sys
from datetime import UTC, datetime
from urllib.parse import urlparse

from wordpress_nix_tools.common import data
from wordpress_nix_tools.common.cloudflare import Cloudflare, CloudflareError
from wordpress_nix_tools.common.remote import Remote, RemoteError
from wordpress_nix_tools.legacy import model

# The survey, as PHP on the legacy host. stdin is the script; argv[1] the site path.
SURVEY_PHP = r"""<?php
error_reporting(E_ERROR);
$path = rtrim($argv[1], '/');
if (!chdir($path) || !is_file('wp-config.php')) { fwrite(STDERR, "no wp-config.php in $path\n"); exit(3); }
define('ABSPATH', $path . '/');
$src = file_get_contents('wp-config.php');
$src = preg_replace('/^\s*(require|include)(_once)?[^;]*wp-settings\.php[^;]*;/m', '', $src);
eval('?>' . $src);
$out = ['schema' => 1];
$wp_version = '';
if (is_file('wp-includes/version.php')) { include 'wp-includes/version.php'; }
$p = isset($table_prefix) ? $table_prefix : 'wp_';
$out['php'] = PHP_VERSION;
$out['wordpress'] = ['version' => $wp_version, 'tablePrefix' => $p,
  'multisite' => (defined('MULTISITE') && MULTISITE) || (defined('WP_ALLOW_MULTISITE') && WP_ALLOW_MULTISITE)];
$cfg = [];
foreach (['S3_BUCKET', 'S3_ENDPOINT', 'S3_PUBLIC_URL', 'S3_EXCLUDE_PREFIXES', 'WP_HOME', 'WP_SITEURL'] as $k) {
  if (defined($k)) { $cfg[$k] = constant($k); }
}
$out['config'] = $cfg;
$host = DB_HOST; $port = null; $sock = null;
if (strpos($host, ':') !== false) { list($host, $rest) = explode(':', $host, 2); if (is_numeric($rest)) { $port = (int) $rest; } else { $sock = $rest; } }
$db = @new mysqli($host, DB_USER, DB_PASSWORD, DB_NAME, $port, $sock);
if ($db->connect_errno) { fwrite(STDERR, "database: " . $db->connect_error . "\n"); exit(4); }
$db->set_charset('utf8mb4');
function rows($db, $sql) { $r = $db->query($sql); $o = []; if ($r) { while ($row = $r->fetch_assoc()) { $o[] = $row; } } return $o; }
function one($db, $sql) { $r = rows($db, $sql); return $r ? array_values($r[0])[0] : null; }
$opt = function ($name) use ($db, $p) {
  $n = $db->real_escape_string($name);
  return one($db, "SELECT option_value FROM `{$p}options` WHERE option_name = '$n' LIMIT 1");
};
$active = @unserialize((string) $opt('active_plugins'), ['allowed_classes' => false]) ?: [];
$out['wordpress']['siteurl'] = $opt('siteurl');
$out['wordpress']['home'] = $opt('home');
$out['wordpress']['locale'] = $opt('WPLANG') ?: (defined('WPLANG') && WPLANG ? WPLANG : 'en_US');
$out['activeTheme'] = ['stylesheet' => $opt('stylesheet'), 'template' => $opt('template')];
$out['mariadb'] = one($db, 'SELECT VERSION()');
$out['dbBytes'] = (int) one($db, 'SELECT SUM(data_length + index_length) FROM information_schema.tables WHERE table_schema = DATABASE()');
function header_of($file, $keys) {
  $h = @file_get_contents($file, false, null, 0, 8192); $o = [];
  if ($h === false) { return $o; }
  foreach ($keys as $k) { if (preg_match('/^[ \t\/*#@]*' . preg_quote($k, '/') . ':(.*)$/mi', $h, $m)) { $o[$k] = trim(preg_replace('/\s*(?:\*\/|\?>).*$/', '', $m[1])); } }
  return $o;
}
$plugins = [];
$activeNames = [];
foreach ($active as $f) { $activeNames[explode('/', $f)[0]] = true; }
foreach ((array) @scandir('wp-content/plugins') as $e) {
  if ($e === '.' || $e === '..' || $e === 'index.php' || $e[0] === '.') { continue; }
  $d = "wp-content/plugins/$e"; $file = null; $h = [];
  if (is_dir($d)) {
    foreach (array_merge(["$d/$e.php"], (array) glob("$d/*.php")) as $c) { $hh = header_of($c, ['Plugin Name', 'Version', 'Author', 'Update URI']); if (isset($hh['Plugin Name'])) { $file = substr($c, strlen('wp-content/plugins/')); $h = $hh; break; } }
  } elseif (substr($e, -4) === '.php') { $h = header_of($d, ['Plugin Name', 'Version', 'Author', 'Update URI']); if (!isset($h['Plugin Name'])) { continue; } $file = $e; }
  else { continue; }
  $plugins[] = ['name' => $e, 'file' => $file, 'version' => isset($h['Version']) ? $h['Version'] : '', 'author' => isset($h['Author']) ? $h['Author'] : '', 'active' => isset($activeNames[$e])];
}
foreach (array_keys($activeNames) as $n) {
  $seen = false; foreach ($plugins as $pl) { if ($pl['name'] === $n) { $seen = true; } }
  if (!$seen) { $plugins[] = ['name' => $n, 'file' => null, 'version' => '', 'active' => true, 'missing' => true]; }
}
$out['plugins'] = $plugins;
$themes = [];
foreach ((array) @scandir('wp-content/themes') as $e) {
  if ($e === '.' || $e === '..' || !is_dir("wp-content/themes/$e")) { continue; }
  $h = header_of("wp-content/themes/$e/style.css", ['Theme Name', 'Version', 'Template', 'Author']);
  $themes[] = ['name' => $e, 'version' => isset($h['Version']) ? $h['Version'] : '', 'parent' => isset($h['Template']) ? $h['Template'] : '',
    'active' => $e === $out['activeTheme']['stylesheet'] || $e === $out['activeTheme']['template']];
}
$out['themes'] = $themes;
$out['muPlugins'] = array_values(array_filter((array) @scandir('wp-content/mu-plugins'), function ($e) { return $e !== '.' && $e !== '..'; }));
$out['dropins'] = array_values(array_filter((array) @scandir('wp-content'), function ($e) { return is_file("wp-content/$e") && substr($e, -4) === '.php' && $e !== 'index.php'; }));
$out['administrators'] = rows($db, "SELECT u.ID AS id, u.user_login AS login, u.user_email AS email, u.user_registered AS registered FROM `{$p}users` u JOIN `{$p}usermeta` m ON m.user_id = u.ID AND m.meta_key = '{$p}capabilities' WHERE m.meta_value LIKE '%\"administrator\"%' ORDER BY u.ID");
$out['dbCode'] = [
  'triggers' => rows($db, 'SELECT TRIGGER_NAME AS name, EVENT_OBJECT_TABLE AS `table`, ACTION_TIMING AS timing, EVENT_MANIPULATION AS event, ACTION_STATEMENT AS definition, DEFINER AS definer FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = DATABASE()'),
  'routines' => rows($db, 'SELECT ROUTINE_NAME AS name, ROUTINE_TYPE AS type, ROUTINE_DEFINITION AS definition, DEFINER AS definer FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE()'),
  'events' => rows($db, 'SELECT EVENT_NAME AS name, EVENT_DEFINITION AS definition, STATUS AS status, DEFINER AS definer FROM information_schema.EVENTS WHERE EVENT_SCHEMA = DATABASE()'),
  'views' => rows($db, 'SELECT TABLE_NAME AS name, VIEW_DEFINITION AS definition, DEFINER AS definer FROM information_schema.VIEWS WHERE TABLE_SCHEMA = DATABASE()'),
];
$up = 'wp-content/uploads'; $bytes = 0; $php = []; $dirs = [];
if (is_dir($up)) {
  foreach (scandir($up) as $e) { if ($e[0] !== '.' && is_dir("$up/$e")) { $dirs[] = $e; } }
  $it = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($up, FilesystemIterator::SKIP_DOTS));
  foreach ($it as $f) {
    if (!$f->isFile()) { continue; }
    $bytes += $f->getSize();
    if (preg_match('/\.(ph(p\d?|tml|ar|ps)|cgi|pl|py|sh)$|^\.htaccess$|^\.user\.ini$/i', $f->getFilename())) {
      $rel = substr($f->getPathname(), strlen($up) + 1);
      if ($rel === 'wpcodebox/index.php' && trim(file_get_contents($f->getPathname())) === '<?php die();') { continue; }
      $php[] = $rel;
    }
  }
}
$out['uploads'] = ['bytes' => $bytes, 'dirs' => $dirs, 'phpFiles' => $php];
$out['woocommerce'] = isset($activeNames['woocommerce']);
$out['xcloud'] = (bool) glob('wp-content/mu-plugins/xcloud-*') || is_dir('/etc/xcloud');
echo json_encode($out);
"""

CHECKSUMS_SH = r"""
set -u
cd "$1" || exit 3
if command -v wp >/dev/null 2>&1; then
  if wp core verify-checksums --skip-plugins --skip-themes >/dev/null 2>&1; then echo "core ok"; else echo "core failed"; fi
  wp plugin verify-checksums --all --format=json --skip-plugins --skip-themes 2>/dev/null | sed 's/^/plugins /' || true
else
  echo "core unavailable"
fi
"""


def run_survey(remote: Remote, path: str) -> dict:
    rc, out, err = remote.run(f"php -- {shlex.quote(path)}", SURVEY_PHP.encode())
    if rc != 0:
        raise RemoteError(remote.host, "php survey", rc, err)
    try:
        survey = json.loads(out.decode(errors="replace").strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise RemoteError(remote.host, "php survey", 1, f"not JSON: {e}") from e
    survey["host"] = remote.host
    survey["path"] = path
    survey["takenAt"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    survey["checksums"] = checksums(remote, path)
    annotate(survey)
    return survey


def checksums(remote: Remote, path: str) -> dict:
    rc, out, _ = remote.script(CHECKSUMS_SH, path)
    result: dict = {"core": "unavailable", "plugins": {}}
    for line in out.decode(errors="replace").splitlines():
        if line.startswith("core "):
            result["core"] = line.split(" ", 1)[1]
        elif line.startswith("plugins "):
            try:
                for row in json.loads(line.split(" ", 1)[1]):
                    result["plugins"][row.get("plugin_name", "?")] = row.get("message", "failed")
            except ValueError:
                pass
    return result


def annotate(survey: dict) -> None:
    """premium/writer/drop columns per plugin, and the fleet score."""
    premium = set(data.load("premium-slugs").get("premium", []))
    writers = data.load("runtime-writers").get("plugins", {})
    for p in survey.get("plugins", []):
        p["premium"] = p["name"] in premium
        p["writer"] = p["name"] in writers
        reason = data.drop_reason(p["name"])
        if reason:
            p["drop"] = reason
    survey["score"] = model.complexity(survey)


def cloudflare_status(survey: dict, cf: Cloudflare | None) -> None:
    home = (survey.get("wordpress") or {}).get("home") or ""
    host = urlparse(home).hostname or ""
    if not host:
        return
    if cf is None:
        survey["cloudflare"] = {"host": host, "reachable": False, "note": "no token"}
        return
    zone = cf.zone_for(host)
    if zone is None:
        survey["cloudflare"] = {"host": host, "reachable": False, "status": "not on Cloudflare or not reachable"}
        return
    info = {
        "host": host,
        "zone": zone.name,
        "zoneId": zone.id,
        "account": zone.account,
        "status": zone.status,
        "type": zone.type,
        "reachable": True,
    }
    apo = cf.api("GET", f"/zones/{zone.id}/settings/automatic_platform_optimization")
    info["apo"] = bool(((apo.get("result") or {}).get("value") or {}).get("enabled")) if apo.get("success") else None
    rules = {}
    for phase in ("http_request_cache_settings", "http_request_transform", "http_response_headers_transform"):
        env = cf.api("GET", f"/zones/{zone.id}/rulesets/phases/{phase}/entrypoint")
        rules[phase] = len((env.get("result") or {}).get("rules") or []) if env.get("success") else 0
    pr = cf.api("GET", f"/zones/{zone.id}/pagerules")
    rules["pagerules"] = len(pr.get("result") or []) if pr.get("success") else 0
    info["rules"] = rules
    survey["cloudflare"] = info


def find_sites(remote: Remote, root: str) -> list[str]:
    """Fleet mode: every directory under ROOT (depth ≤ 3) with a wp-config.php."""
    out = remote.text(f"find {shlex.quote(root)} -maxdepth 3 -name wp-config.php -type f 2>/dev/null")
    return sorted({line.rsplit("/", 1)[0] for line in out.splitlines() if line.strip()})


def compare(old: dict, new: dict) -> list[str]:
    """What changed since the T-7 survey: core, plugin or theme versions, the active set."""
    changes = []
    ov, nv = (old.get("wordpress") or {}).get("version"), (new.get("wordpress") or {}).get("version")
    if ov != nv:
        changes.append(f"core {ov} → {nv}")
    for key in ("plugins", "themes"):
        a = {p["name"]: p for p in old.get(key, [])}
        b = {p["name"]: p for p in new.get(key, [])}
        for name in sorted(set(a) | set(b)):
            pa, pb = a.get(name), b.get(name)
            if pa is None:
                changes.append(f"{key[:-1]} {name} added ({pb.get('version')})")
            elif pb is None:
                changes.append(f"{key[:-1]} {name} removed")
            else:
                if pa.get("version") != pb.get("version"):
                    changes.append(f"{key[:-1]} {name} {pa.get('version')} → {pb.get('version')}")
                if bool(pa.get("active")) != bool(pb.get("active")):
                    changes.append(f"{key[:-1]} {name} {'activated' if pb.get('active') else 'deactivated'}")
    if model.active_theme(old) != model.active_theme(new):
        changes.append(f"active theme {model.active_theme(old)} → {model.active_theme(new)}")
    return changes


CSV_FIELDS = (
    "host",
    "path",
    "home",
    "wordpress",
    "php",
    "plugins",
    "premium",
    "woocommerce",
    "writers",
    "locale",
    "dbCode",
    "cfZone",
    "cfAccount",
    "cfReachable",
    "score",
)


def csv_row(s: dict) -> dict:
    code = s.get("dbCode") or {}
    cf = s.get("cloudflare") or {}
    wp = s.get("wordpress") or {}
    return {
        "host": s.get("host"),
        "path": s.get("path"),
        "home": wp.get("home"),
        "wordpress": wp.get("version"),
        "php": s.get("php"),
        "plugins": len(s.get("plugins", [])),
        "premium": sum(1 for p in s.get("plugins", []) if p.get("premium")),
        "woocommerce": s.get("woocommerce"),
        "writers": sum(1 for p in s.get("plugins", []) if p.get("writer")),
        "locale": wp.get("locale"),
        "dbCode": sum(len(code.get(k) or []) for k in ("triggers", "routines", "events", "views")),
        "cfZone": cf.get("zone"),
        "cfAccount": cf.get("account"),
        "cfReachable": cf.get("reachable"),
        "score": s.get("score"),
    }


def to_csv(surveys: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for s in surveys:
        w.writerow(csv_row(s))
    return buf.getvalue()


def _cf() -> Cloudflare | None:
    try:
        return Cloudflare()
    except CloudflareError:
        return None


def cmd(args, remote: Remote | None = None, cf: Cloudflare | None = None) -> int:
    remote = remote or Remote(args.host)
    cf = cf if cf is not None else _cf()
    if args.fleet:
        paths = find_sites(remote, args.root)
    elif args.path:
        paths = [args.path]
    else:
        print("legacy survey: give --path P, or --root DIR --fleet", file=sys.stderr)
        return 2
    surveys = []
    for path in paths:
        try:
            s = run_survey(remote, path)
        except RemoteError as e:
            print(f"legacy survey: {e}", file=sys.stderr)
            if not args.fleet:
                return 3
            continue
        cloudflare_status(s, cf)
        s["score"] = model.complexity(s)
        surveys.append(s)
    if args.compare:
        old = json.loads(open(args.compare).read())
        changes = compare(old, surveys[0])
        for c in changes:
            print(f"changed: {c}")
        if changes:
            print(f"{len(changes)} change(s) since {args.compare}: stop the cutover", file=sys.stderr)
            return 1
        print("no change since the survey")
        return 0
    if args.csv:
        sys.stdout.write(to_csv(surveys))
    elif args.fleet:
        json.dump(surveys, sys.stdout, indent=2)
        print()
    else:
        json.dump(surveys[0], sys.stdout, indent=2)
        print()
    return 0
