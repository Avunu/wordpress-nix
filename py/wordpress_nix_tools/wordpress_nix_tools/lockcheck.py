"""``wordpress-nix ci verify-relock``: what a relocked site says, before it is pushed.

site-deps.yml's relock-push runs this (standards-core's ``ci verify-relock``, with this
module as the product's ``ci.verify_relock``) on the tree it is about to push, against
the commit the integration branch holds (the pull request's merge-base). It reads git
only: nothing of the pull request runs.

The rule is frappe-nix's lockcheck: **the URL names the same package and version**,
never a vendor host allowlist. For every package ``composer.lock`` adds or changes:

- ``wp-plugin/<s>`` / ``wp-theme/<s>`` (WP Packages): ``dist.url`` is
  ``https://downloads.wordpress.org/{plugin|theme}/<s>.<version>.zip``, and the svn
  ``source`` is wordpress.org's for the same slug;
- ``roots/wordpress-no-content`` (core): ``dist.url`` is
  ``https://downloads.w.org/release/wordpress-<version>-no-content.zip`` (or the same
  path on downloads.wordpress.org) and the lock carries its ``shasum``;
- every other package (Packagist: ``avunu/*``, ``roots/*``, ``composer/*``,
  ``symfony/*``, …): ``dist.url`` is GitHub's zipball (``api.github.com/repos/<o>/<r>/
  zipball/<ref>`` or ``codeload.github.com/<o>/<r>/legacy.zip/<ref>``), ``<o>/<r>`` is
  ``source.url``'s owner and repository, and ``<ref>`` is ``source.reference`` (and
  ``dist.reference``).

A mismatch is a **reject** for a changed package (nothing is pushed): a wordpress.org or
core URL that names another slug, version or host, a core dist without its ``shasum``, or
a Packagist package from GitHub whose dist is not its own source's zipball (another
owner or repository, another reference, another host). A Packagist package that is not
on GitHub at all (its source on GitLab or Bitbucket, a ``path`` or ``artifact`` dist, no
dist) is another host, which plan §4.4 sends to **review**: the relock is pushed, so CI
runs on the real change, and a person reads it. A package the lock **adds** is pushed for
**review** even when its URL passes, and so is one it removes. ``nix/vendor-hash`` must
be one SRI sha256 line, never ``lib.fakeHash``.

The decision table relocks every Composer PR (verdict.py), so the §6.4 rows the version
change decides are **review** here too, and relock-push leaves auto-merge off for them
(:func:`update_review`):

- ``roots/wordpress*`` minor or major (a patch is auto);
- a major of any package;
- ``wp-plugin/woocommerce*`` minor where the head's ``.github/wordpress-nix.json`` says
  ``woocommerce: true``.

The update type is read from the versions as Dependabot reads them: the first number is
major, the second minor, the rest patch.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from standards_core.ci.relock import Verification

LOCK = "composer.lock"
VENDOR_HASH = "nix/vendor-hash"
SITE_JSON = ".github/wordpress-nix.json"
FAKE_HASH = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

_HASH = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")
_SLUG = r"[a-z0-9][a-z0-9._-]*"
_WPORG = re.compile(rf"wp-(?P<kind>plugin|theme)/(?P<slug>{_SLUG})")
_GITHUB_SOURCE = re.compile(
    r"(?:https://github\.com/|git@github\.com:)(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?"
)
_GITHUB_DIST = (
    re.compile(
        r"https://api\.github\.com/repos/(?P<owner>[A-Za-z0-9_.-]+)/"
        r"(?P<repo>[A-Za-z0-9_.-]+)/zipball/(?P<ref>[0-9a-f]{40})"
    ),
    re.compile(
        r"https://codeload\.github\.com/(?P<owner>[A-Za-z0-9_.-]+)/"
        r"(?P<repo>[A-Za-z0-9_.-]+)/legacy\.zip/(?P<ref>[0-9a-f]{40})"
    ),
)
_SHA1 = re.compile(r"[0-9a-f]{40}")
_CORE_HOSTS = ("https://downloads.w.org/release/", "https://downloads.wordpress.org/release/")


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    data: Mapping[str, Any]

    @property
    def dist(self) -> Mapping[str, Any]:
        dist = self.data.get("dist")
        return dist if isinstance(dist, dict) else {}

    @property
    def source(self) -> Mapping[str, Any]:
        source = self.data.get("source")
        return source if isinstance(source, dict) else {}


def packages(text: str, where: str) -> dict[str, Package]:
    """Every package of a ``composer.lock`` text (``packages`` and
    ``packages-dev``), by name. Not a lock is a ``ValueError``."""
    if not text.strip():
        return {}
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{where}: not JSON: {e}") from e
    if not isinstance(doc, dict):
        raise ValueError(f"{where}: not a composer.lock")
    out: dict[str, Package] = {}
    for key in ("packages", "packages-dev"):
        for item in doc.get(key) or []:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ValueError(f"{where}: a {key} entry without a name")
            name = item["name"].lower()
            out[name] = Package(name, str(item.get("version", "")), item)
    return out


def _version(version: str) -> str:
    return version[1:] if version[:1] == "v" and version[1:2].isdigit() else version


def _wporg_or_core(pkg: Package) -> bool:
    return bool(_WPORG.fullmatch(pkg.name)) or pkg.name == "roots/wordpress-no-content"


def foreign(pkg: Package) -> str | None:
    """Why a Packagist package is not on GitHub at all (its source on another host, a
    ``path`` or ``artifact`` dist, no dist), or ``None``: a person's to read, not a
    reject. wordpress.org and core packages are never foreign: their URLs are fixed."""
    if _wporg_or_core(pkg):
        return None
    dist, source = pkg.dist, pkg.source
    url = dist.get("url")
    if not isinstance(url, str) or not url:
        return f"{pkg.name} {pkg.version}: no dist URL, so not from GitHub"
    if dist.get("type") != "zip":
        return f"{pkg.name} {pkg.version}: a {dist.get('type')!r} dist ({url}), not from GitHub"
    if _GITHUB_SOURCE.fullmatch(str(source.get("url") or "")) is None:
        return (
            f"{pkg.name} {pkg.version}: source {source.get('url')!r} and dist {url} are"
            " not on GitHub"
        )
    return None


def problem(pkg: Package) -> str | None:
    """Why ``pkg``'s dist URL does not name ``pkg`` at its version, or ``None``. A
    Packagist package that is not on GitHub at all is :func:`foreign`'s, not this."""
    dist, source = pkg.dist, pkg.source
    url = dist.get("url")
    if not isinstance(url, str) or not url:
        return f"{pkg.name} {pkg.version}: no dist URL"
    if dist.get("type") != "zip":
        return f"{pkg.name} {pkg.version}: dist type {dist.get('type')!r}, not zip"

    m = _WPORG.fullmatch(pkg.name)
    if m:
        kind, slug = m["kind"], m["slug"]
        want = f"https://downloads.wordpress.org/{kind}/{slug}.{pkg.version}.zip"
        if url != want:
            return f"{pkg.name} {pkg.version}: dist {url} is not {want}"
        svn = f"https://{kind}s.svn.wordpress.org/{slug}/"
        if source and source.get("url") != svn:
            return f"{pkg.name} {pkg.version}: source {source.get('url')} is not {svn}"
        return None

    if pkg.name == "roots/wordpress-no-content":
        version = _version(pkg.version)
        want = f"wordpress-{version}-no-content.zip"
        if url not in (host + want for host in _CORE_HOSTS):
            return f"{pkg.name} {pkg.version}: dist {url} is not {_CORE_HOSTS[0]}{want}"
        if not _SHA1.fullmatch(str(dist.get("shasum") or "")):
            return f"{pkg.name} {pkg.version}: the lock carries no shasum for {url}"
        return None

    src = _GITHUB_SOURCE.fullmatch(str(source.get("url") or ""))
    if src is None:
        return (
            f"{pkg.name} {pkg.version}: source {source.get('url')!r} is not a GitHub"
            " repository"
        )
    ref = str(source.get("reference") or "")
    for pattern in _GITHUB_DIST:
        d = pattern.fullmatch(url)
        if d is None:
            continue
        if (d["owner"].lower(), d["repo"].lower()) != (
            src["owner"].lower(),
            src["repo"].lower(),
        ):
            return (
                f"{pkg.name} {pkg.version}: dist {url} is not from"
                f" {src['owner']}/{src['repo']}, its source"
            )
        if d["ref"] != ref or str(dist.get("reference") or ref) != ref:
            return (
                f"{pkg.name} {pkg.version}: dist {url} is not the source reference"
                f" {ref or '(none)'}"
            )
        return None
    return f"{pkg.name} {pkg.version}: dist {url} is not a GitHub zipball of its source"


def _numbers(version: str) -> tuple[int, ...] | None:
    m = re.match(r"v?(\d+(?:\.\d+)*)", str(version).strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def update_type(old: str, new: str) -> str:
    """``major``, ``minor``, ``patch`` or ``other`` (equal, or not numeric, such as
    dev-main)."""
    a, b = _numbers(old), _numbers(new)
    if a is None or b is None:
        return "other" if old == new else "major"
    a, b = a + (0,) * (3 - len(a)), b + (0,) * (3 - len(b))
    if a[0] != b[0]:
        return "major"
    if a[1] != b[1]:
        return "minor"
    return "patch" if a != b else "other"


def update_review(name: str, old: str, new: str, *, woocommerce: bool) -> str | None:
    """Why §6.4 leaves this version change to a person, or ``None``."""
    kind = update_type(old, new)
    if kind == "major":
        return f"{name}: a major update ({old} → {new})"
    if kind == "minor" and name.startswith("roots/wordpress"):
        return f"{name}: a WordPress core minor update ({old} → {new})"
    if kind == "minor" and woocommerce and name.startswith("wp-plugin/woocommerce"):
        return f"{name}: a WooCommerce minor update on a WooCommerce site ({old} → {new})"
    return None


def is_woocommerce(site_json: str | None) -> bool:
    """``woocommerce`` of a site's ``.github/wordpress-nix.json``; none is false."""
    try:
        doc = json.loads(site_json or "{}")
    except ValueError:
        return False
    return isinstance(doc, dict) and doc.get("woocommerce") is True


def compare(
    base: Mapping[str, Package],
    head: Mapping[str, Package],
    *,
    woocommerce: bool = False,
) -> Verification:
    """What changed between two locks' packages: a changed package's URL that names
    another package or version rejects; a changed package not on GitHub (another host),
    an added or removed package, or a version change §6.4 leaves to a person
    (:func:`update_review`) is reviewed."""
    res = Verification()
    for name in sorted(head):
        pkg = head[name]
        before = base.get(name)
        if before is not None and before.data == pkg.data:
            continue
        elsewhere = foreign(pkg)
        why = None if elsewhere else problem(pkg)
        if why:
            res.rejects.append(why)
            continue
        if elsewhere:
            res.reviews.append(elsewhere)
        if before is None:
            res.reviews.append(f"{name} {pkg.version}: a new package")
        elif row := update_review(
            name, before.version, pkg.version, woocommerce=woocommerce
        ):
            res.reviews.append(row)
    for name in sorted(set(base) - set(head)):
        res.reviews.append(f"{name} {base[name].version}: removed")
    return res


def _show(root: Path, rev: str, path: str) -> str | None:
    """``path`` at ``rev`` (a commit or a tree), ``None`` when absent."""
    res = subprocess.run(
        ["git", "-C", str(root), "cat-file", "-p", f"{rev}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return res.stdout if res.returncode == 0 else None


def vendor_hash_problem(text: str | None) -> str | None:
    if text is None:
        return f"{VENDOR_HASH} is missing"
    value = text.strip()
    if text != value + "\n" and text != value:
        return f"{VENDOR_HASH} must be one line"
    if not _HASH.fullmatch(value):
        return f"{VENDOR_HASH} is not one sha256 SRI hash"
    if value == FAKE_HASH:
        return f"{VENDOR_HASH} is lib.fakeHash: the relock did not finish"
    return None


def verify_relock(root: Path, base: str, head: str) -> Verification:
    """``ci.verify_relock``: ``base`` a commit the integration branch holds, ``head``
    the commit or tree to push. ``woocommerce`` is read from the head's
    ``.github/wordpress-nix.json``."""
    res = Verification()
    try:
        before = packages(_show(root, base, LOCK) or "", f"{LOCK} at {base}")
        after_text = _show(root, head, LOCK)
        if after_text is None:
            res.rejects.append(f"{LOCK} is missing at {head}")
            return res
        after = packages(after_text, f"{LOCK} at {head}")
    except ValueError as e:
        res.rejects.append(str(e))
        return res
    found = compare(
        before, after, woocommerce=is_woocommerce(_show(root, head, SITE_JSON))
    )
    res.rejects += found.rejects
    res.reviews += found.reviews
    why = vendor_hash_problem(_show(root, head, VENDOR_HASH))
    if why:
        res.rejects.append(why)
    return res


def check_all(lock_text: str) -> list[str]:
    """Every package of one lock against the rule: what verify-relock would reject or
    review were the whole lock new."""
    return [
        p
        for pkg in packages(lock_text, LOCK).values()
        if (p := foreign(pkg) or problem(pkg))
    ]
