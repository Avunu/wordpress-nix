"""``wordpress-nix audit``: the site's code against Wordfence Intelligence V3 (§4.14).

It matches composer.lock (``wp-plugin/*``, ``wp-theme/*``, ``roots/wordpress-no-content``
as core) and the versions in committed plugin and theme headers against each
vulnerability's ``affected_versions`` ranges. With ``--base-rev`` (the PR's base
commit), only findings the change *introduced* fail the run (exit 1); findings the
base already had, in its composer.lock or its committed headers (read from git),
are reported. ``--base-lock`` gives the base lock as a file instead; without
``--base-rev`` the base has no committed code, so every committed finding fails.
Without either every finding fails.

The feed is ``--feed FILE`` or, with ``WORDPRESS_NIX_WORDFENCE_KEY``, the production
feed (``https://www.wordfence.com/api/intelligence/v3/vulnerabilities/production``).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wordpress_nix_tools.common.versions import compare, normalize

FEED_URL = "https://www.wordfence.com/api/intelligence/v3/vulnerabilities/production"
KEY_ENV = "WORDPRESS_NIX_WORDFENCE_KEY"
HEADER = re.compile(r"^[ \t/*#@]*Version:[ \t]*(.*?)[ \t]*(?:\*/)?$", re.MULTILINE | re.I)


@dataclass(frozen=True)
class Software:
    kind: str  # plugin | theme | core
    slug: str
    version: str
    where: str  # composer.lock | web/app/plugins/<x>

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.slug, self.version)


@dataclass(frozen=True)
class Finding:
    software: Software
    id: str
    title: str
    cvss: str
    patched: tuple[str, ...]


def in_range(version: str, r: dict) -> bool:
    """One affected_versions range: from/to with inclusivity; ``*`` is unbounded."""
    lo, hi = str(r.get("from_version", "*")), str(r.get("to_version", "*"))
    if lo != "*":
        c = compare(version, lo)
        if c < 0 or (c == 0 and not r.get("from_inclusive", True)):
            return False
    if hi != "*":
        c = compare(version, hi)
        if c > 0 or (c == 0 and not r.get("to_inclusive", True)):
            return False
    return True


def affected(version: str, ranges: dict) -> bool:
    return any(in_range(version, r) for r in (ranges or {}).values() if isinstance(r, dict))


def from_lock(text: str, where: str = "composer.lock") -> list[Software]:
    doc = json.loads(text)
    out = []
    for p in (doc.get("packages") or []) + (doc.get("packages-dev") or []):
        name, version = p.get("name", ""), normalize(str(p.get("version", "")))
        if name.startswith(("wp-plugin/", "wpackagist-plugin/")):
            out.append(Software("plugin", name.split("/", 1)[1], version, where))
        elif name.startswith(("wp-theme/", "wpackagist-theme/")):
            out.append(Software("theme", name.split("/", 1)[1], version, where))
        elif name in ("roots/wordpress-no-content", "roots/wordpress"):
            out.append(Software("core", "wordpress", version, where))
    # roots/wordpress and -no-content name the same core.
    seen, unique = set(), []
    for s in out:
        if s.key not in seen:
            seen.add(s.key)
            unique.append(s)
    return unique


def _committed(
    dirs: Callable[[str], list[str]], php: Callable[[str], list[str]], read: Callable[[str], str | None]
) -> list[Software]:
    """Committed plugins and themes, by their header's Version. ``dirs(sub)`` lists
    the directories under web/app/<sub>, ``php(dir)`` a directory's top-level .php
    files, ``read(path)`` a file's text (None when absent)."""
    out = []
    for kind, sub in (("plugin", "plugins"), ("theme", "themes")):
        for name in sorted(dirs(f"web/app/{sub}")):
            where = f"web/app/{sub}/{name}"
            if kind == "theme":
                files = [f"{where}/style.css"]
            else:
                files = [f"{where}/{name}.php", *(f"{where}/{f}" for f in sorted(php(where)))]
            for f in files:
                text = read(f)
                if text is None:
                    continue
                head = text[:8192]
                if kind == "plugin" and "Plugin Name" not in head:
                    continue
                m = HEADER.search(head)
                if m:
                    out.append(Software(kind, name, normalize(m.group(1)), where))
                    break
    return out


def from_tree(root: Path) -> list[Software]:
    """Committed plugins and themes in the working tree."""

    def dirs(sub: str) -> list[str]:
        d = root / sub
        return [e.name for e in d.iterdir() if e.is_dir()] if d.is_dir() else []

    def php(sub: str) -> list[str]:
        return [e.name for e in (root / sub).glob("*.php")]

    def read(path: str) -> str | None:
        try:
            return (root / path).read_text(errors="replace")
        except OSError:
            return None

    return _committed(dirs, php, read)


def from_git(root: Path, rev: str) -> list[Software]:
    """Committed plugins and themes at ``rev`` (the PR's base), read from git."""

    def git(*args: str) -> str | None:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
        return p.stdout if p.returncode == 0 else None

    def entries(sub: str, kind: str) -> list[str]:
        out = git("ls-tree", f"{rev}:{sub}") or ""
        names = []
        for line in out.splitlines():
            meta, _, name = line.partition("\t")
            if meta.split()[1:2] == [kind]:
                names.append(name)
        return names

    return _committed(
        lambda sub: entries(sub, "tree"),
        lambda sub: [n for n in entries(sub, "blob") if n.endswith(".php")],
        lambda path: git("show", f"{rev}:{path}"),
    )


def match(feed: dict, software: list[Software]) -> list[Finding]:
    index: dict[tuple[str, str], list[Software]] = {}
    for s in software:
        index.setdefault((s.kind, s.slug), []).append(s)
    found = []
    for vid, v in feed.items():
        if not isinstance(v, dict):
            continue
        for sw in v.get("software") or []:
            kind = sw.get("type")
            slug = sw.get("slug")
            for s in index.get((kind, slug), []):
                if s.version and affected(s.version, sw.get("affected_versions")):
                    cvss = v.get("cvss") or {}
                    found.append(
                        Finding(
                            software=s,
                            id=str(v.get("id", vid)),
                            title=str(v.get("title", "")),
                            cvss=f"{cvss.get('score', '?')} {cvss.get('rating', '')}".strip(),
                            patched=tuple(sw.get("patched_versions") or ()),
                        )
                    )
    return sorted(found, key=lambda f: (f.software.kind, f.software.slug, f.id))


def load_feed(path: str | None, fetch=None) -> dict:
    if path:
        return json.loads(Path(path).read_text())
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise SystemExit(f"wordpress-nix audit: no --feed and no {KEY_ENV}")
    if fetch:
        return fetch(FEED_URL, key)
    req = urllib.request.Request(FEED_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
        return json.loads(r.read())


def audit(
    root: Path, lock_text: str, feed: dict, base_lock_text: str | None, base_rev: str | None = None
) -> tuple[list[Finding], list[Finding]]:
    """(introduced, pre-existing). The base is ``base_lock_text`` plus the committed
    code at ``base_rev``; the head's own committed code never counts as the base."""
    software = from_lock(lock_text) + from_tree(root)
    findings = match(feed, software)
    if base_lock_text is None and base_rev is None:
        return findings, []
    base_software = from_lock(base_lock_text) if base_lock_text else []
    if base_rev:
        base_software += from_git(root, base_rev)
    base = {(f.software.kind, f.software.slug, f.id) for f in match(feed, base_software)}
    introduced = [f for f in findings if (f.software.kind, f.software.slug, f.id) not in base]
    existing = [f for f in findings if (f.software.kind, f.software.slug, f.id) in base]
    return introduced, existing


def _line(f: Finding) -> str:
    fix = f" (fixed in {', '.join(f.patched)})" if f.patched else " (no fix)"
    s = f.software
    return f"{s.kind} {s.slug} {s.version} [{s.where}]: {f.title} — CVSS {f.cvss}{fix} ({f.id})"


def run(args: argparse.Namespace) -> int:
    root = Path(args.root)
    lock = Path(args.lock) if args.lock else root / "composer.lock"
    try:
        lock_text = lock.read_text()
        base_text = Path(args.base_lock).read_text() if args.base_lock else None
        if args.base_rev:
            if subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--verify", "-q", f"{args.base_rev}^{{commit}}"],
                capture_output=True,
                check=False,
            ).returncode:
                raise ValueError(f"--base-rev {args.base_rev} is not a commit in {root}")
            if base_text is None:
                shown = subprocess.run(
                    ["git", "-C", str(root), "show", f"{args.base_rev}:composer.lock"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                base_text = shown.stdout if shown.returncode == 0 else ""
        feed = load_feed(args.feed)
    except (OSError, ValueError) as e:
        print(f"wordpress-nix audit: {e}", file=sys.stderr)
        return 2
    introduced, existing = audit(root, lock_text, feed, base_text, args.base_rev)
    if args.format == "json":
        json.dump(
            {
                "introduced": [_line(f) for f in introduced],
                "existing": [_line(f) for f in existing],
            },
            sys.stdout,
            indent=2,
        )
        print()
    else:
        gh = args.format == "github"
        for f in introduced:
            print(f"::error::{_line(f)}" if gh else f"NEW       {_line(f)}")
        for f in existing:
            print(f"::warning::{_line(f)}" if gh else f"existing  {_line(f)}")
        print(f"{len(introduced)} introduced, {len(existing)} pre-existing")
    return 1 if introduced else 0


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--root", default=".", help="the site repository (default: .)")
    p.add_argument("--lock", help="composer.lock (default: ROOT/composer.lock)")
    p.add_argument(
        "--base-rev", help="the PR's base commit: only findings it did not have fail (its lock and committed code)"
    )
    p.add_argument(
        "--base-lock", help="the lock at the PR's base, as a file (default with --base-rev: REV:composer.lock)"
    )
    p.add_argument("--feed", help=f"a Wordfence V3 feed file (default: download with {KEY_ENV})")
    p.add_argument("--format", choices=("text", "json", "github"), default="text")
