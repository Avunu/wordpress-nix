"""wordpress-nix's data files (data/*.json in the repository).

Found, in order: ``WORDPRESS_NIX_DATA_DIR``; the package's own ``data/`` (the Nix
build copies the repository's there); the repository checkout this module sits in.
"""

import functools
import json
import os
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

FILES = (
    "wordpress-presets",
    "drop-plugins",
    "host-mu-plugins",
    "runtime-writers",
    "salt-dependent-plugins",
    "premium-slugs",
    "db-code-plugins",
)


class DataError(Exception):
    pass


def data_dir() -> Path:
    env = os.environ.get("WORDPRESS_NIX_DATA_DIR")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    packaged = here.parents[1] / "data"
    if (packaged / "drop-plugins.json").is_file():
        return packaged
    repo = here.parents[4] / "data"
    if (repo / "drop-plugins.json").is_file():
        return repo
    raise DataError("wordpress-nix's data files were not found; set WORDPRESS_NIX_DATA_DIR")


@functools.cache
def load(name: str) -> Any:
    if name not in FILES:
        raise DataError(f"no data file {name!r}")
    path = data_dir() / f"{name}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise DataError(f"cannot read {path}: {e}") from e


def match_table(name: str, exact: dict, patterns: dict) -> Any:
    """``exact[name]``, else the first ``patterns`` glob that matches, else None."""
    if name in exact:
        return exact[name]
    for glob, value in patterns.items():
        if fnmatchcase(name, glob):
            return value
    return None


def drop_reason(slug: str, *, collision: bool = False) -> str | None:
    """Why the drop list drops ``slug``, or None. An entry with ``onlyCollision`` (the
    jwt-auth fork) drops only a copy that is not the wordpress.org plugin."""
    table = load("drop-plugins")
    entry = match_table(slug, table.get("plugins", {}), table.get("patterns", {}))
    if entry is None:
        return None
    if isinstance(entry, dict):
        if entry.get("onlyCollision") and not collision:
            return None
        return entry.get("reason", "on the drop list")
    return str(entry)


def host_mu_reason(filename: str) -> str | None:
    table = load("host-mu-plugins")
    return match_table(filename, table.get("files", {}), table.get("patterns", {}))
