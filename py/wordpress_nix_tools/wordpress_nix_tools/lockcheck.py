"""What a relock's files may say before a job holding the App's key pushes them
(``wordpress-nix ci verify-relock``, standards-core's ``ci.verify_relock`` hook).

A Composer relock touches ``composer.lock`` (Dependabot's) and ``nix/vendor-hash``
(the relock's). It compares the tree to push (``--head``) with a commit the
integration branch holds (``--base``), reading git only, with frappe-nix's rule:
the URL names the package and version, never a host allowlist alone.

- ``composer.lock`` may not be deleted, and must parse.
- ``nix/vendor-hash`` is one sha256 SRI line, never ``lib.fakeHash``.
- Each package entry that is not the base's (by name, version, source and dist):
  - ``wp-plugin/<slug>`` / ``wp-theme/<slug>``: the dist is the wordpress.org zip
    of that slug and version (``https://downloads.wordpress.org/{plugin,theme}/<slug>.<version>.zip``);
  - ``roots/wordpress-no-content``: the wordpress.org no-content release of that
    version (downloads.wordpress.org or downloads.w.org);
  - a GitHub source (Packagist): the dist is the GitHub zipball of that repository
    at the source's reference (``https://api.github.com/repos/<owner>/<repo>/zipball/<ref>``);
  - a metapackage has no dist;
  - anything else is ``reject``.
- A package name the base lock did not have is ``needs-review``.

The §6.4 rows the version change decides are ``needs-review`` too, so relock-push
leaves auto-merge off for them (``update_review``):
- ``roots/wordpress*`` minor or major (a patch is auto);
- a major of any package;
- ``wp-plugin/woocommerce*`` minor where the head's ``.github/wordpress-nix.json``
  says ``woocommerce: true``.
The update type is read from the versions as Dependabot reads them: the first
number is major, the second minor, the rest patch.
"""

import json
import re
import subprocess
from pathlib import Path

from standards_core.ci.relock import Verification

FAKE_HASH = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
SRI = re.compile(r"sha256-[A-Za-z0-9+/]{43}=")
GITHUB_SOURCE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?$")
CORE_HOSTS = ("https://downloads.wordpress.org/release/", "https://downloads.w.org/release/")


def _show(root: Path, rev: str, path: str) -> str | None:
    p = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{path}"], capture_output=True, text=True, check=False)
    return p.stdout if p.returncode == 0 else None


def _packages(text: str) -> dict[str, dict]:
    doc = json.loads(text)
    out = {}
    for key in ("packages", "packages-dev"):
        for p in doc.get(key) or []:
            if isinstance(p, dict) and "name" in p:
                out[p["name"].lower()] = p
    return out


def _same(a: dict, b: dict) -> bool:
    keys = ("version", "source", "dist", "type")
    return all(a.get(k) == b.get(k) for k in keys)


def package_problem(p: dict) -> str | None:
    """Why this lock entry's URLs do not name its package and version, or None."""
    name = p.get("name", "").lower()
    version = str(p.get("version", "")).lstrip("v")
    dist = p.get("dist") or {}
    url = dist.get("url", "")
    ptype = p.get("type", "")
    if ptype == "metapackage" and not dist:
        return None
    if not url:
        return "no dist URL"
    if not url.startswith("https://"):
        return f"dist {url!r} is not https"
    for prefix, kind in (("wp-plugin/", "plugin"), ("wp-theme/", "theme")):
        if name.startswith(prefix):
            slug = name[len(prefix) :]
            want = f"https://downloads.wordpress.org/{kind}/{slug}.{version}.zip"
            if url != want:
                return f"dist {url!r} is not {want}"
            return None
    if name == "roots/wordpress-no-content":
        if not any(url == f"{h}wordpress-{version}-no-content.zip" for h in CORE_HOSTS):
            return f"dist {url!r} is not wordpress.org's {version} no-content release"
        return None
    source = p.get("source") or {}
    m = GITHUB_SOURCE.match(source.get("url", ""))
    if m:
        owner, repo = m.group(1), m.group(2)
        ref = source.get("reference", "")
        want = f"https://api.github.com/repos/{owner}/{repo}/zipball/{ref}"
        if url != want or dist.get("reference", ref) != ref:
            return f"dist {url!r} is not the zipball of {owner}/{repo} at {ref}"
        return None
    return f"dist {url!r} is from no source this check knows"


def _numbers(version: str) -> tuple[int, ...] | None:
    m = re.match(r"v?(\d+(?:\.\d+)*)", str(version).strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def update_type(old: str, new: str) -> str:
    """``major``, ``minor``, ``patch`` or ``other`` (equal, or not numeric, such as dev-main)."""
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
    """Why §6.4 leaves this version change to a person, or None."""
    kind = update_type(old, new)
    if kind == "major":
        return f"a major update ({old} → {new})"
    if kind == "minor" and name.startswith("roots/wordpress"):
        return f"a WordPress core minor update ({old} → {new})"
    if kind == "minor" and woocommerce and name.startswith("wp-plugin/woocommerce"):
        return f"a WooCommerce minor update on a WooCommerce site ({old} → {new})"
    return None


def is_woocommerce(site_json: str | None) -> bool:
    try:
        return bool(json.loads(site_json or "{}").get("woocommerce"))
    except (ValueError, AttributeError):
        return False


def check(
    base_lock: str | None, head_lock: str | None, head_hash: str | None, *, woocommerce: bool = False
) -> Verification:
    v = Verification()
    if head_lock is None:
        if base_lock is not None:
            v.rejects.append("composer.lock was deleted")
        return v
    try:
        head = _packages(head_lock)
    except (ValueError, AttributeError) as e:
        v.rejects.append(f"composer.lock does not parse: {e}")
        return v
    try:
        base = _packages(base_lock) if base_lock else {}
    except (ValueError, AttributeError):
        base = {}
    for name, p in sorted(head.items()):
        if name in base and _same(base[name], p):
            continue
        problem = package_problem(p)
        if problem:
            v.rejects.append(f"{name} {p.get('version', '?')}: {problem}")
        elif name not in base and base_lock is not None:
            v.reviews.append(f"{name} {p.get('version', '?')}: a package the base lock does not have")
        elif name in base:
            why = update_review(name, str(base[name].get("version", "")), str(p.get("version", "")), woocommerce=woocommerce)
            if why:
                v.reviews.append(f"{name}: {why}")
    if head_hash is not None:
        line = head_hash.strip()
        if line == FAKE_HASH:
            v.rejects.append("nix/vendor-hash is lib.fakeHash")
        elif not SRI.fullmatch(line):
            v.rejects.append("nix/vendor-hash is not one sha256 SRI hash")
    return v


def verify_relock(root: Path, base: str, head: str) -> Verification:
    """standards-core's ``ci.verify_relock``: (root, base, head) → Verification."""
    return check(
        _show(root, base, "composer.lock"),
        _show(root, head, "composer.lock"),
        _show(root, head, "nix/vendor-hash"),
        woocommerce=is_woocommerce(_show(root, head, ".github/wordpress-nix.json")),
    )
