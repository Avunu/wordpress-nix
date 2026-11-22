"""The managed block of the site's ``.gitignore``.

Stock Bedrock ignores ``web/app/plugins/*`` and ``web/app/mu-plugins/*/``, and
tracks themes. The block whitelists each committed plugin and mu-plugin directory,
ignores each Composer-installed theme (a dev shell's ``composer install`` puts it
in the tree), and ignores the backup directory. It is derived from the tree and
composer.json, so a reconcile rewrites the same block.
"""

import json
from pathlib import Path

from wordpress_nix_tools.common import ui
from wordpress_nix_tools.common.proc import git, probe_ok
from wordpress_nix_tools.common.records import BACKUP
from wordpress_nix_tools.wordpress_init.state import Run

BEGIN = "# BEGIN wordpress-nix (managed)"
BEGIN_LINE = "# BEGIN wordpress-nix (managed): committed premium and custom code, one line each"
END = "# END wordpress-nix"


def composer_names(root: Path) -> set[str]:
    try:
        doc = json.loads((root / "composer.json").read_text())
    except (OSError, ValueError):
        return set()
    return set((doc.get("require") or {})) | set((doc.get("require-dev") or {}))


def block_lines(root: Path) -> list[str]:
    names = composer_names(root)
    installed = {n.split("/", 1)[1] for n in names if "/" in n}
    lines = [f"/{BACKUP}/"]
    plugins = root / "web/app/plugins"
    if plugins.is_dir():
        for p in sorted(plugins.iterdir(), key=lambda p: p.name):
            if p.name.startswith(".") or p.name in installed:
                continue
            if p.is_dir() or p.suffix == ".php":
                lines.append(f"!web/app/plugins/{p.name}" + ("/" if p.is_dir() else ""))
    mu = root / "web/app/mu-plugins"
    if mu.is_dir():
        for p in sorted(mu.iterdir(), key=lambda p: p.name):
            if p.is_dir() and p.name not in installed:
                lines.append(f"!web/app/mu-plugins/{p.name}/")
    for n in sorted(names):
        if n.startswith("wp-theme/"):
            lines.append(f"web/app/themes/{n.split('/', 1)[1]}/")
    return lines


def with_block(current: str | None, lines: list[str]) -> str:
    body = "\n".join([BEGIN_LINE, *lines, END]) + "\n"
    if not current:
        return body
    out, inside, placed = [], False, False
    for line in current.splitlines(keepends=True):
        if line.startswith(BEGIN):
            inside = True
            continue
        if inside:
            if line.startswith(END):
                inside = False
                out.append(body)
                placed = True
            continue
        out.append(line)
    if inside:
        raise ValueError(".gitignore has a wordpress-nix block with no end marker")
    text = "".join(out)
    if not placed:
        text = text.rstrip("\n") + "\n\n" + body
    return text


def install_block(st: Run) -> None:
    path = st.root / ".gitignore"
    current = path.read_text() if path.is_file() else None
    if current is None and st.staging is not None and (st.staging / ".gitignore").is_file():
        current = (st.staging / ".gitignore").read_text()
    try:
        new = with_block(current, block_lines(st.root))
    except ValueError as e:
        ui.die(str(e))
    if path.is_file() and path.read_text() == new:
        ui.info(". .gitignore already current")
        return
    path.write_text(new)
    ui.info("+ .gitignore (wordpress-nix managed block)")


NEEDED = (
    "flake.nix",
    "composer.json",
    "composer.lock",
    "nix/vendor-hash",
    "config/application.php",
    "web/wp-config.php",
    "web/index.php",
    ".github/wordpress-nix.json",
    ".envrc",
)


def verify_not_ignored(st: Run) -> None:
    """A flake's source is its tracked files: anything the build needs that
    .gitignore excludes is a much later, silent build failure."""
    paths = list(NEEDED)
    for item in st.committed:
        paths.append(item.target)
    bad = False
    for p in paths:
        if (st.root / p).exists() and probe_ok(git("check-ignore", "-q", "--", p, cwd=str(st.root))):
            ui.err(f"{p} is excluded by .gitignore")
            bad = True
    if bad:
        ui.die("the site's .gitignore excludes files the Nix build needs (a flake's source is only its tracked files)")
