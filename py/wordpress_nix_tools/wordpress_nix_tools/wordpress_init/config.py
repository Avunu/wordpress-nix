"""What lib/init.nix bakes into ``wordpress-init``: store paths, as the JSON
``WORDPRESS_NIX_CLI_CONFIG`` names. Without it (tests, a checkout), the repository's
own template and data are used."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

from wordpress_nix_tools.common import data


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class InitConfig:
    # templates/bedrock-site: stock Bedrock and the site flake.
    template: str
    # data/: presets, drop list, runtime writers, ...
    data: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def load() -> InitConfig:
    path = os.environ.get("WORDPRESS_NIX_CLI_CONFIG")
    if not path:
        root = _repo_root()
        template = root / "templates" / "bedrock-site"
        if not template.is_dir():
            raise ConfigError("no WORDPRESS_NIX_CLI_CONFIG; run wordpress-init as `nix run github:Avunu/wordpress`")
        return InitConfig(template=str(template), data=str(data.data_dir()))
    try:
        raw = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    unknown = sorted(set(raw) - {"template", "data"})
    if unknown or not all(isinstance(raw.get(k), str) for k in ("template", "data")):
        raise ConfigError(f"{path}: expected {{template, data}} (strings), got {sorted(raw)}")
    os.environ.setdefault("WORDPRESS_NIX_DATA_DIR", raw["data"])
    return InitConfig(template=raw["template"], data=raw["data"])
