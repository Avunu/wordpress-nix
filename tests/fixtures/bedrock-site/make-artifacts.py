#!/usr/bin/env python3
"""Regenerate artifacts/, the fixture site's local Composer `artifact` repository.

    python3 tests/fixtures/bedrock-site/make-artifacts.py

Run it only to change the fixture: it needs the network for the four real
packages, and every zip's sha1 is in composer.lock, so a regenerated zip means
a re-locked fixture (`composer update` here, then `nix run .#relock`-style:
build tests/bedrock-build.nix with lib.fakeHash and write nix/vendor-hash).

What goes in:
  - real upstream packages at pinned commits, as Packagist serves them
    (composer/installers and roots/wordpress-core-installer, the two Composer
    plugins stock Bedrock needs; the roots/wordpress metapackage; and
    roots/bedrock-disallow-indexing, a real wordpress-muplugin);
  - packages/*, small stand-ins for what WP Packages and wordpress.org serve
    (a core with wp-includes/version.php, a plugin, a theme, a PSR-4 library).

An artifact's composer.json must carry `version`, which a GitHub zipball's does
not, so each real package is repacked with it added. Zips are written with
fixed timestamps and sorted entries, so a rerun reproduces the same bytes.
"""

import io
import json
import pathlib
import sys
import urllib.request
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "artifacts"

# name, version, GitHub owner/repo, commit (Packagist's dist.reference).
UPSTREAM = [
    ("composer/installers", "2.3.0", "composer/installers", "12fb2dfe5e16183de69e784a7b84046c43d97e8e"),
    (
        "roots/wordpress-core-installer",
        "4.0.0",
        "roots/wordpress-core-installer",
        "85d3589252eb93eee836facf33b54ce30c565bea",
    ),
    ("roots/wordpress", "7.1.5", "roots/wordpress", "eb3d108f216618a858dcaed691e00e68f0ccff9f"),
    (
        "roots/bedrock-disallow-indexing",
        "2.1.0",
        "roots/bedrock-disallow-indexing",
        "e9f20bf1c2baf96daf80ebab3f4cfbb756b5c88c",
    ),
]

EPOCH = (1980, 1, 1, 0, 0, 0)


def write_zip(path: pathlib.Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, files[name])


def with_version(raw: bytes, version: str) -> bytes:
    manifest = json.loads(raw)
    manifest["version"] = version
    return (json.dumps(manifest, indent=4) + "\n").encode()


def upstream(name: str, version: str, repo: str, ref: str) -> dict[str, bytes]:
    url = f"https://codeload.github.com/{repo}/legacy.zip/{ref}"
    with urllib.request.urlopen(url) as response:  # noqa: S310 (a pinned https URL)
        data = response.read()
    files = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            # GitHub's zipball has one top-level directory, <owner>-<repo>-<sha>/.
            rel = info.filename.split("/", 1)[1]
            files[rel] = z.read(info)
    files["composer.json"] = with_version(files["composer.json"], version)
    return files


def local(directory: pathlib.Path) -> tuple[str, str, dict[str, bytes]]:
    files = {
        str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob("*") if p.is_file()
    }
    manifest = json.loads(files["composer.json"])
    return manifest["name"], manifest["version"], files


def main() -> int:
    OUT.mkdir(exist_ok=True)
    for old in OUT.glob("*.zip"):
        old.unlink()
    for name, version, repo, ref in UPSTREAM:
        write_zip(OUT / f"{name.replace('/', '--')}-{version}.zip", upstream(name, version, repo, ref))
    for directory in sorted((HERE / "packages").iterdir()):
        name, version, files = local(directory)
        write_zip(OUT / f"{name.replace('/', '--')}-{version}.zip", files)
    for z in sorted(OUT.glob("*.zip")):
        print(f"{z.stat().st_size:>8}  {z.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
