"""The catalog the classifier asks: wordpress.org (is the slug theirs, by whom, is it
closed), WP Packages (is the installed version in the registry), the published
files of a version (plugin checksums; a theme's zip), and Packagist (an Avunu
package).

Two implementations behind one interface:

- ``NetworkCatalog``: the public APIs, over HTTPS (stdlib urllib), cached per run;
- ``FileCatalog``: one JSON file, ``WORDPRESS_NIX_CATALOG``, for offline runs and the
  tests::

      {"plugins": {"akismet": {"name": "Akismet", "author": "Automattic",
                               "closed": false, "registry": ["5.3"],
                               "files": {"5.3": {"akismet.php": "<sha256>"}}}},
       "themes":  {...the same shape...},
       "avunu":   {"wp-cloud-files": "avunu/wp-cloud-files"}}

A slug wordpress.org does not know is absent; ``registry`` lists the versions WP
Packages has; ``files`` maps a version to its published files' sha256.
"""

import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

API = "https://api.wordpress.org"
DOWNLOADS = "https://downloads.wordpress.org"
REGISTRY = "https://repo.wp-packages.org"
PACKAGIST = "https://repo.packagist.org"


@dataclass
class Info:
    """What wordpress.org says about a slug."""

    name: str = ""
    author: str = ""
    closed: bool = False
    versions: list[str] = field(default_factory=list)


class Catalog:
    def info(self, kind: str, slug: str) -> Info | None:
        raise NotImplementedError

    def registry(self, kind: str, slug: str) -> set[str] | None:
        """The versions WP Packages has for wp-<kind>/<slug>, or None when unknown."""
        raise NotImplementedError

    def files(self, kind: str, slug: str, version: str) -> dict[str, str | list[str]] | None:
        """{relative path: sha256, or the list of them} of what wordpress.org
        published, or None."""
        raise NotImplementedError

    def avunu(self, slug: str) -> str | None:
        """The Packagist name of an Avunu package for this directory, or None."""
        raise NotImplementedError


class FileCatalog(Catalog):
    def __init__(self, path: str | Path) -> None:
        self.raw = json.loads(Path(path).read_text())

    def _entry(self, kind: str, slug: str) -> dict | None:
        return (self.raw.get(f"{kind}s") or {}).get(slug)

    def info(self, kind, slug):
        e = self._entry(kind, slug)
        if e is None:
            return None
        return Info(
            name=e.get("name", slug),
            author=e.get("author", ""),
            closed=bool(e.get("closed")),
            versions=list(e.get("versions", e.get("registry", []))),
        )

    def registry(self, kind, slug):
        e = self._entry(kind, slug)
        return None if e is None else set(e.get("registry", []))

    def files(self, kind, slug, version):
        e = self._entry(kind, slug)
        if e is None:
            return None
        return (e.get("files") or {}).get(version)

    def avunu(self, slug):
        return (self.raw.get("avunu") or {}).get(slug)


def _strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


class NetworkCatalog(Catalog):
    """The public APIs. Every answer is cached for the run; a network failure is
    ``None`` (unknown), never "not on wordpress.org"."""

    def __init__(self, fetch=None) -> None:
        self._fetch = fetch or self._urlopen
        self._cache: dict[str, object] = {}

    @staticmethod
    def _urlopen(url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": "wordpress-nix/wordpress-init"})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
            return r.read()

    def _get(self, url: str, *, raw: bool = False):
        if url not in self._cache:
            try:
                body = self._fetch(url)
                self._cache[url] = body if raw else json.loads(body)
            except urllib.error.HTTPError as e:
                self._cache[url] = {"__status": e.code}
            except (urllib.error.URLError, OSError, ValueError):
                self._cache[url] = None
        return self._cache[url]

    def info(self, kind, slug):
        q = urllib.parse.urlencode(
            {
                "action": f"{kind}_information",
                "request[slug]": slug,
                "request[fields][versions]": 1,
            }
        )
        data = self._get(f"{API}/{kind}s/info/1.2/?{q}")
        if not isinstance(data, dict) or data.get("__status") == 404:
            return None
        if data.get("error"):
            if "closed" in str(data.get("error")).lower() or data.get("closed"):
                return Info(name=slug, closed=True)
            return None
        author = data.get("author")
        if isinstance(author, dict):
            author = author.get("display_name") or author.get("user_nicename") or ""
        return Info(
            name=_strip_tags(data.get("name", slug)),
            author=_strip_tags(str(author or "")),
            closed=False,
            versions=sorted((data.get("versions") or {}).keys()),
        )

    def registry(self, kind, slug):
        data = self._get(f"{REGISTRY}/p2/wp-{kind}/{slug}.json")
        if not isinstance(data, dict) or "__status" in data:
            return set() if isinstance(data, dict) and data.get("__status") == 404 else None
        versions = (data.get("packages") or {}).get(f"wp-{kind}/{slug}") or []
        # Packagist's p2 is a list of version objects; WP Packages keys them by version.
        if isinstance(versions, dict):
            versions = [
                v if isinstance(v, dict) and v.get("version") else {"version": k} for k, v in versions.items()
            ]
        return {str(v.get("version", "")).lstrip("v") for v in versions if isinstance(v, dict)}

    def files(self, kind, slug, version):
        if kind == "plugin":
            data = self._get(
                f"{DOWNLOADS}/plugin-checksums/{urllib.parse.quote(slug)}/{urllib.parse.quote(version)}.json"
            )
            if not isinstance(data, dict) or "__status" in data:
                return None
            # A file of a tag edited after its release lists every hash it has had.
            return {
                rel: e["sha256"]
                for rel, e in (data.get("files") or {}).items()
                if isinstance(e, dict) and isinstance(e.get("sha256"), (str, list))
            }
        blob = self._get(f"{DOWNLOADS}/theme/{slug}.{version}.zip", raw=True)
        if not isinstance(blob, bytes):
            return None
        try:
            archive = zipfile.ZipFile(io.BytesIO(blob))
        except zipfile.BadZipFile:
            return None
        out = {}
        for i in archive.infolist():
            if i.is_dir():
                continue
            rel = i.filename.split("/", 1)[1] if "/" in i.filename else i.filename
            out[rel] = hashlib.sha256(archive.read(i)).hexdigest()
        return out

    def avunu(self, slug):
        data = self._get(f"{PACKAGIST}/p2/avunu/{slug}.json")
        if isinstance(data, dict) and (data.get("packages") or {}).get(f"avunu/{slug}"):
            return f"avunu/{slug}"
        return None


def load() -> Catalog:
    path = os.environ.get("WORDPRESS_NIX_CATALOG")
    return FileCatalog(path) if path else NetworkCatalog()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def _published(want: str | list[str], path: Path) -> bool:
    """The file is one of the published versions of it (a hash, or a list of them)."""
    return sha256(path) in ([want] if isinstance(want, str) else want)


def published_share(directory: Path, published: dict) -> float:
    """The share of an installed copy's PHP files that are wordpress.org's published
    files of the same path: near 1 for the wordpress.org plugin, near 0 for another
    plugin that took its slug."""
    total = matched = 0
    for p in directory.rglob("*.php"):
        if not p.is_file() or p.is_symlink():
            continue
        total += 1
        want = published.get(p.relative_to(directory).as_posix())
        if want is not None and _published(want, p):
            matched += 1
    return matched / total if total else 0.0


def modified_files(directory: Path, published: dict) -> list[str]:
    """The files of an installed copy that differ from, or are absent from, the
    published set: any PHP file, and any other file that differs. A published file
    the copy lacks is not a modification."""
    out = []
    for p in sorted(directory.rglob("*")):
        if not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(directory).as_posix()
        want = published.get(rel)
        if want is None:
            if rel.endswith(".php"):
                out.append(rel)
            continue
        if not _published(want, p):
            out.append(rel)
    return out
