"""The suite runs from the repository (PYTHONPATH=py/wordpress_nix_tools) or the
Nix build (the package installed); the repository's data/ and templates/ are found
through WORDPRESS_NIX_REPO when the package is installed.

``site`` is a throwaway git repository (a site checkout) with no global git
configuration; ``git`` and ``commit`` drive it."""

import json
import os
import subprocess
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


def git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.org",
    }
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()


def commit(root: Path, files: dict[str, str], message: str = "c") -> str:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def site(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    return tmp_path


@pytest.fixture
def bedrock_lock() -> dict:
    """The template's lock: stock Bedrock 1.31.9, core 7.1.5, two-factor, twentytwentyfive."""
    return json.loads((REPO / "templates/bedrock-site/composer.lock").read_text())
