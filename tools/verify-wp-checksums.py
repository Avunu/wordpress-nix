"""verify-wp-checksums: compare a built Bedrock tree with what wordpress.org published.

    verify-wp-checksums [--root DIR] [--lock FILE] [--locale en_US]

Reads composer.lock (default: DIR/composer.lock) and DIR/composer.json, then:

  core       roots/wordpress-no-content (or roots/wordpress) against
             api.wordpress.org/core/checksums/1.0/ (md5 per file), in the
             directory extra.wordpress-install-dir names (web/wp);
  plugins    each wp-plugin/<slug> (or wpackagist-plugin/<slug>) against
             downloads.wordpress.org/plugin-checksums/<slug>/<version>.json
             (sha256 per file), in its installer-paths directory;
  themes     each wp-theme/<slug> (or wpackagist-theme/<slug>) against the
             locked dist zip itself, downloaded and hashed, since wordpress.org
             publishes no checksums for themes.

A PHP file that differs from, or is absent from, the published set fails the
run (exit 1), as does a package whose checksums cannot be had. Anything else
(a changed readme, a file the install lacks) is a warning. Stdlib only: it
runs as a CI job step and from `nix run .#verify-wp-checksums`, with the
network, never inside the Nix sandbox.

--api-base and --downloads-base (or WORDPRESS_NIX_API_BASE and
WORDPRESS_NIX_DOWNLOADS_BASE) point it somewhere else; the flake check that
tests it serves fixtures from a local server.
"""

import argparse
import hashlib
import io
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile

CORE_PACKAGES = ("roots/wordpress-no-content", "roots/wordpress", "johnpbloch/wordpress-core")
PLUGIN_PREFIXES = ("wp-plugin/", "wpackagist-plugin/")
THEME_PREFIXES = ("wp-theme/", "wpackagist-theme/")


class Report:
    def __init__(self):
        self.failures = []
        self.warnings = []

    def fail(self, scope, message):
        self.failures.append(f"{scope}: {message}")

    def warn(self, scope, message):
        self.warnings.append(f"{scope}: {message}")


def fetch(url):
    """Bytes at url: http(s), file://, or a plain path (a lock's local artifact)."""
    if "://" not in url:
        return pathlib.Path(url).read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "wordpress-nix/verify-wp-checksums"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (a lock URL or a flag's)
        return response.read()


def fetch_json(url):
    return json.loads(fetch(url))


def digest(path, algorithm):
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def tree_files(directory):
    return {
        p.relative_to(directory).as_posix(): p
        for p in sorted(directory.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def compare(report, scope, directory, expected, algorithm, ignore_missing=()):
    """expected: {relative path: hex digest or list of them}."""
    if not directory.is_dir():
        report.fail(scope, f"{directory} is not installed")
        return
    installed = tree_files(directory)
    for rel, path in installed.items():
        is_php = rel.endswith(".php")
        if rel not in expected:
            if is_php:
                report.fail(scope, f"{rel}: not in the published files")
            continue
        want = expected[rel]
        wants = want if isinstance(want, list) else [want]
        if digest(path, algorithm) not in wants:
            (report.fail if is_php else report.warn)(scope, f"{rel}: differs from the published file")
    for rel in expected:
        if rel not in installed and not rel.startswith(tuple(ignore_missing)):
            report.warn(scope, f"{rel}: published, but not installed")


def installer_dir(composer, package_type, slug, package):
    """The directory composer/installers put a package in, from extra.installer-paths."""
    name = (package.get("extra") or {}).get("installer-name") or slug
    paths = (composer.get("extra") or {}).get("installer-paths") or {}
    for pattern, matchers in paths.items():
        if package["name"] in matchers or f"type:{package_type}" in matchers:
            return pattern.replace("{$name}", name).rstrip("/")
    defaults = {"wordpress-plugin": "web/app/plugins", "wordpress-theme": "web/app/themes"}
    return f"{defaults[package_type]}/{name}"


def verify_core(report, args, composer, package):
    version = package["version"].lstrip("v")
    scope = f"core {version}"
    query = urllib.parse.urlencode({"version": version, "locale": args.locale})
    try:
        data = fetch_json(f"{args.api_base}/core/checksums/1.0/?{query}")
    except (urllib.error.URLError, ValueError) as e:
        report.fail(scope, f"no checksums from wordpress.org ({e})")
        return
    checksums = data.get("checksums")
    if isinstance(checksums, dict) and version in checksums:
        checksums = checksums[version]
    if not isinstance(checksums, dict) or not checksums:
        report.fail(scope, f"wordpress.org has no checksums for {version} ({args.locale})")
        return
    directory = args.root / ((composer.get("extra") or {}).get("wordpress-install-dir") or "web/wp")
    # A Composer core is the no-content build: wp-content is the site's own.
    compare(report, scope, directory, checksums, "md5", ignore_missing=("wp-content/",))


def verify_plugin(report, args, composer, package, slug):
    version = package["version"].lstrip("v")
    scope = f"plugin {slug} {version}"
    url = f"{args.downloads_base}/plugin-checksums/{urllib.parse.quote(slug)}/{urllib.parse.quote(version)}.json"
    try:
        data = fetch_json(url)
    except (urllib.error.URLError, ValueError) as e:
        report.fail(scope, f"no checksums from wordpress.org ({e})")
        return
    files = data.get("files") or {}
    if not files:
        report.fail(scope, "wordpress.org lists no files")
        return
    expected = {rel: entry.get("sha256") for rel, entry in files.items() if entry.get("sha256")}
    compare(report, scope, args.root / installer_dir(composer, "wordpress-plugin", slug, package), expected, "sha256")


def verify_theme(report, args, composer, package, slug, lock_dir):
    version = package["version"].lstrip("v")
    scope = f"theme {slug} {version}"
    url = (package.get("dist") or {}).get("url")
    if not url:
        report.fail(scope, "the lock has no dist URL")
        return
    if "://" not in url:
        url = str(lock_dir / url)
    try:
        archive = zipfile.ZipFile(io.BytesIO(fetch(url)))
    except (urllib.error.URLError, OSError, zipfile.BadZipFile) as e:
        report.fail(scope, f"could not fetch the locked zip ({e})")
        return
    names = [i.filename for i in archive.infolist() if not i.is_dir()]
    # wordpress.org theme zips hold one <slug>/ directory; Composer strips it.
    tops = {n.split("/", 1)[0] for n in names}
    strip = len(tops) == 1 and all("/" in n for n in names)
    expected = {}
    for n in names:
        rel = n.split("/", 1)[1] if strip else n
        expected[rel] = hashlib.sha256(archive.read(n)).hexdigest()
    compare(report, scope, args.root / installer_dir(composer, "wordpress-theme", slug, package), expected, "sha256")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."), help="the built tree (default: .)")
    parser.add_argument("--lock", type=pathlib.Path, help="composer.lock (default: ROOT/composer.lock)")
    parser.add_argument("--locale", default="en_US")
    parser.add_argument(
        "--api-base", default=os.environ.get("WORDPRESS_NIX_API_BASE", "https://api.wordpress.org")
    )
    parser.add_argument(
        "--downloads-base",
        default=os.environ.get("WORDPRESS_NIX_DOWNLOADS_BASE", "https://downloads.wordpress.org"),
    )
    args = parser.parse_args(argv)
    args.api_base = args.api_base.rstrip("/")
    args.downloads_base = args.downloads_base.rstrip("/")
    lock_path = args.lock or args.root / "composer.lock"
    composer = json.loads((args.root / "composer.json").read_text())
    lock = json.loads(lock_path.read_text())
    packages = lock.get("packages") or []

    report = Report()
    checked = 0
    by_name = {p["name"]: p for p in packages}
    core = next((by_name[n] for n in CORE_PACKAGES if n in by_name), None)
    if core is None:
        report.fail("core", "the lock has no WordPress core package")
    else:
        verify_core(report, args, composer, core)
        checked += 1
    for package in packages:
        name = package["name"]
        if name.startswith(PLUGIN_PREFIXES):
            verify_plugin(report, args, composer, package, name.split("/", 1)[1])
            checked += 1
        elif name.startswith(THEME_PREFIXES):
            verify_theme(report, args, composer, package, name.split("/", 1)[1], lock_path.parent)
            checked += 1

    for line in report.warnings:
        print(f"warning: {line}", file=sys.stderr)
    for line in report.failures:
        print(f"FAIL: {line}")
    print(f"{checked} packages checked, {len(report.failures)} failures, {len(report.warnings)} warnings")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
