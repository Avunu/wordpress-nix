"""The suite runs from the repository (PYTHONPATH=py/wordpress_nix_tools) or the
Nix build (the package installed); the repository's data/ and templates/ are found
through WORDPRESS_NIX_REPO when the package is installed."""

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ.get("WORDPRESS_NIX_REPO", HERE.parents[2]))
sys.path.insert(0, str(HERE))

if (REPO / "data").is_dir():
    os.environ.setdefault("WORDPRESS_NIX_DATA_DIR", str(REPO / "data"))


@pytest.fixture
def repo() -> Path:
    return REPO


@pytest.fixture
def fixtures() -> Path:
    return HERE / "fixtures"
