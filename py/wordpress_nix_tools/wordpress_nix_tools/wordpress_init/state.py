"""One run's state: the arguments, what was resolved, and what the steps found.
The git index plus the tree are the conversion's state; this object only carries a
run's answers from one step to the next."""

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from wordpress_nix_tools.common import ui
from wordpress_nix_tools.wordpress_init.args import InitArgs
from wordpress_nix_tools.wordpress_init.config import InitConfig
from wordpress_nix_tools.wordpress_init.presets import Presets

# Flags that stop the conversion until a flag on the command line decides them.
BLOCKING = ("modified", "missing-code", "db-code")


@dataclass
class Item:
    """A directory or file under wp-content, classified (classify.py)."""

    # plugin | theme | mu-plugin | dropin
    kind: str
    # The directory or file name: akismet, hello.php, object-cache.php.
    name: str
    is_dir: bool = True
    version: str = ""
    header: dict[str, str] = field(default_factory=dict)
    # True/False from the survey or dump; None when the active set is unknown.
    active: bool | None = None
    present: bool = True
    # composer | avunu | commit | drop | platform
    disp: str = ""
    # The Composer package for composer and avunu: wp-plugin/akismet.
    package: str = ""
    constraint: str = ""
    reason: str = ""
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # A theme's parent (Template: header).
    parent: str = ""
    # Blocking flags a command-line flag (or the data's default) decided.
    decided: list[str] = field(default_factory=list)

    def has(self, flag: str) -> bool:
        return flag in self.flags

    def flag(self, name: str, note: str = "") -> None:
        if name not in self.flags:
            self.flags.append(name)
        if note:
            self.notes.append(f"{name}: {note}")

    @property
    def blocking(self) -> list[str]:
        return [f for f in self.flags if f in BLOCKING and f not in self.decided]

    @property
    def source(self) -> str:
        """Where it is under wp-content."""
        sub = {"plugin": "plugins", "theme": "themes", "mu-plugin": "mu-plugins", "dropin": ""}[self.kind]
        return f"wp-content/{sub}/{self.name}" if sub else f"wp-content/{self.name}"

    @property
    def target(self) -> str:
        """Where committed code goes in Bedrock."""
        sub = {"plugin": "plugins", "theme": "themes", "mu-plugin": "mu-plugins"}[self.kind]
        return f"web/app/{sub}/{self.name}"

    @property
    def slug(self) -> str:
        return self.name[:-4] if not self.is_dir and self.name.endswith(".php") else self.name

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "Item":
        return cls(**raw)


@dataclass
class Run:
    args: InitArgs
    config: InitConfig
    presets: Presets
    # init | migrate | reconcile
    mode: str = ""
    root: Path = field(default_factory=lambda: Path("."))
    name: str = ""
    home: str = ""
    php: str = ""
    wp_version: str = ""
    db_prefix: str = "wp_"
    locale: str = "en_US"
    woocommerce: bool = False
    # classic | v2 | bedrock | empty
    shape: str = ""
    survey: dict[str, Any] | None = None
    survey_path: str = ""
    dump_path: str = ""
    # Active plugin names (directory or file), the stylesheet and template.
    active_plugins: set[str] | None = None
    active_mu: set[str] | None = None
    stylesheet: str = ""
    template: str = ""
    items: list[Item] = field(default_factory=list)
    production_only: list[str] = field(default_factory=list)
    development_only: list[str] = field(default_factory=list)
    active_theme: str = ""
    staging: Path | None = None
    warnings: list[str] = field(default_factory=list)
    resumed: bool = False
    # wp-config.php's salts, for --keep-salts.
    legacy_salts: dict[str, str] = field(default_factory=dict)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        ui.warn_line(text)

    @property
    def preset(self):
        return self.presets.php[self.php]

    def by_disp(self, disp: str) -> list[Item]:
        return [i for i in self.items if i.disp == disp]

    @property
    def committed(self) -> list[Item]:
        return [i for i in self.items if i.disp == "commit"]

    @property
    def dropped(self) -> list[Item]:
        return [i for i in self.items if i.disp == "drop"]

    @property
    def blocked(self) -> list[Item]:
        return [i for i in self.items if i.blocking]


def normalize_name(name: str) -> str:
    """A site slug: lowercase, digits and dashes (wordpress-nix.site.name)."""
    n = re.sub(r"[^a-z0-9-]", "-", name.lower())
    n = re.sub(r"-+", "-", n).strip("-")
    n = re.sub(r"^site-", "", n)
    return n or "site"
