"""The only local records (§8.1): ``.wordpress-nix-backup/{survey.json,
provenance.json, inventory.json, cutover-<slug>.json}``. There is no state file:
every networked step probes remote reality."""

import json
from pathlib import Path
from typing import Any

BACKUP = ".wordpress-nix-backup"


def backup_dir(root: str | Path = ".") -> Path:
    return Path(root) / BACKUP


def read(path: str | Path) -> Any:
    return json.loads(Path(path).read_text())


def write(path: str | Path, data: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n")
    return p


def find(root: str | Path, name: str) -> Path | None:
    """``<root>/.wordpress-nix-backup/<name>`` when it exists."""
    p = backup_dir(root) / name
    return p if p.is_file() else None
