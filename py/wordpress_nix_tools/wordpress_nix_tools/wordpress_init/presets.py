"""data/wordpress-presets.json, read and checked.

    {"minimumWordPress": "6.6", "defaultPhp": "php84",
     "php": {"php84": {"label": "PHP 8.4", "platform": "8.4", "requires": ">=8.3"}}}

``platform`` is composer.json's ``config.platform.php``; the key is the nixpkgs
attribute ``wordpress-nix.site.php`` takes.
"""

from dataclasses import dataclass

from wordpress_nix_tools.common import data


class PresetError(Exception):
    pass


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    platform: str
    requires: str


@dataclass(frozen=True)
class Presets:
    minimum_wordpress: str
    default_php: str
    php: dict[str, Preset]


def parse(raw) -> Presets:
    if not isinstance(raw, dict):
        raise PresetError("the presets file must be an object")
    try:
        php = {
            k: Preset(key=k, label=v["label"], platform=v["platform"], requires=v["requires"])
            for k, v in raw["php"].items()
        }
        presets = Presets(minimum_wordpress=raw["minimumWordPress"], default_php=raw["defaultPhp"], php=php)
    except (KeyError, TypeError, AttributeError) as e:
        raise PresetError(f"the presets file is malformed: {e}") from e
    if presets.default_php not in php:
        raise PresetError(f"defaultPhp {presets.default_php!r} is not a preset")
    return presets


def load() -> Presets:
    try:
        return parse(data.load("wordpress-presets"))
    except data.DataError as e:
        raise PresetError(str(e)) from e
