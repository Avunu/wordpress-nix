"""The phases the modes share (§8.5 step 4). Check-then-act: every step looks at the
tree and the git index first, so an interrupted run is resumed by running it again.
Nothing is deleted: superseded files move to .wordpress-nix-backup/."""

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from wordpress_nix_tools.common import records, ui
from wordpress_nix_tools.common.proc import attempt, git, probe, run
from wordpress_nix_tools.wordpress_init import composer_json, detect, gitignore, template
from wordpress_nix_tools.wordpress_init.args import LOOSEN_COMMIT_MSG
from wordpress_nix_tools.wordpress_init.secret_files import SECRETS_README, verify_secrets_visible
from wordpress_nix_tools.wordpress_init.state import Item, Run

#: More files than this to stage usually means .gitignore is wrong.
STAGE_LIMIT = 20000

BACKUP_README = """\
# .wordpress-nix-backup/

What `wordpress-init --migrate` moved out of the way, and the conversion's records.
git ignores this directory; nothing in it was deleted.

- `legacy/`: the classic install's core, wp-config.php, SQL files, uploads, caches,
  drop-ins, dropped plugins and themes, and the Composer-replaced copies.
- `plan.json`: the classification the conversion acted on (a rerun resumes from it).
- `survey.json`, `provenance.json`: written by `wordpress-nix legacy pull`.
- `inventory.json`: written by `wordpress-nix media offload`.
- `cutover-<slug>.json`: written by `wordpress-nix cutover`.

Keep it until the site has been cut over and verified. wp-config.php holds the
legacy credentials: rotate them, and never commit this directory.
"""

# Top-level entries of a classic install that stay where they are.
KEEP_AT_ROOT = {
    ".git",
    ".github",
    ".wordpress-nix-backup",
    "wp-content",
    "secrets",
    "README.md",
    "readme.md",
    "LICENSE",
    "LICENSE.md",
    ".envrc",
    ".env",
    ".direnv",
    ".devenv",
}


def backup(st: Run) -> Path:
    return records.backup_dir(st.root)


def move(src: Path, dest: Path) -> bool:
    """src to dest, never over an existing file: a second copy gets a numbered name."""
    if not (src.exists() or src.is_symlink()):
        return False
    target = dest
    n = 1
    while target.exists() or target.is_symlink():
        target = dest.with_name(f"{dest.name}.{n}")
        n += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(target))
    return True


def prepare_backup(st: Run) -> None:
    b = backup(st)
    b.mkdir(parents=True, exist_ok=True)
    readme = b / "README.md"
    if not readme.is_file():
        readme.write_text(BACKUP_README)
    prov = b / "provenance.json"
    if not prov.is_file():
        records.write(
            prov,
            {
                "source": "local",
                "convertedAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": "no legacy pull: the tree was converted as found",
            },
        )
    if detect.repo_state(st.root) == "none":
        run(git("init", "-q", cwd=str(st.root)))
        ui.info("+ git init")


def save_plan(st: Run) -> None:
    records.write(
        backup(st) / "plan.json",
        {
            "name": st.name,
            "home": st.home,
            "php": st.php,
            "wordpress": st.wp_version,
            "dbPrefix": st.db_prefix,
            "locale": st.locale,
            "woocommerce": st.woocommerce,
            "activeTheme": st.active_theme,
            "developmentOnlyPlugins": st.development_only,
            "productionOnlyPlugins": st.production_only,
            "items": [i.to_json() for i in st.items],
        },
    )


def load_plan(st: Run) -> bool:
    path = backup(st) / "plan.json"
    if not path.is_file():
        return False
    raw = json.loads(path.read_text())
    st.name = st.name or raw["name"]
    st.home = st.home or raw.get("home", "")
    st.php = st.args.php or raw.get("php") or st.php
    st.wp_version = st.args.version or raw.get("wordpress", "")
    st.db_prefix = raw.get("dbPrefix", "wp_")
    st.locale = raw.get("locale", "en_US")
    st.woocommerce = raw.get("woocommerce", False)
    st.active_theme = raw.get("activeTheme", "")
    st.development_only = raw.get("developmentOnlyPlugins", [])
    st.production_only = raw.get("productionOnlyPlugins", [])
    st.items = [Item.from_json(i) for i in raw.get("items", [])]
    return True


# What a resumed conversion may still move from the root: the template has landed,
# so composer.*, config/, web/, nix/, wp-cli.yml and flake.* are the site's own now.
RESUME_AT_ROOT = {*detect.CORE_PATHS, "wp-config.php"}


def legacy_at_root(st: Run, name: str) -> bool:
    if name in KEEP_AT_ROOT:
        return False
    if st.resumed:
        return name in RESUME_AT_ROOT or name.endswith((".sql", ".sql.gz"))
    return not (name in ("flake.nix", "flake.lock") and st.shape != "v2")


def relocate_legacy(st: Run) -> None:
    """Step 2: legacy core, wp-config.php, *.sql, uploads/cache/upgrade, dropped items
    and drop-ins into the backup; a v2 flake too. A resumed run sweeps only legacy
    core, wp-config.php and SQL files from the root."""
    root, legacy = st.root, backup(st) / "legacy"
    moved: list[str] = []
    for name in sorted(os.listdir(root)):
        if not legacy_at_root(st, name):
            continue
        if move(root / name, legacy / name):
            moved.append(name)
    for sub in ("uploads", "cache", "upgrade", "upgrade-temp-backup", "backups", "wflogs"):
        if move(root / "wp-content" / sub, legacy / "wp-content" / sub):
            moved.append(f"wp-content/{sub}")
    for item in st.items:
        if item.disp in ("drop", "platform") and item.present:
            if move(root / item.source, legacy / item.source):
                moved.append(item.source)
    for p in sorted((root / "wp-content").glob("*.sql*")) if (root / "wp-content").is_dir() else []:
        if move(p, legacy / "wp-content" / p.name):
            moved.append(f"wp-content/{p.name}")
    if moved:
        ui.info(f"→ .wordpress-nix-backup/legacy/: {', '.join(moved[:12])}{' …' if len(moved) > 12 else ''}")
    known = {"wp-admin", "wp-includes", "wp-config.php", ".gitignore", ".htaccess", "flake.nix", "flake.lock"}
    known |= set(detect.CORE_PATHS)
    other = [m for m in moved if "/" not in m and m not in known and not m.endswith((".sql", ".sql.gz"))]
    if other:
        st.warn(
            f"moved to .wordpress-nix-backup/legacy/: {', '.join(other)} — move into web/ "
            "any the site serves (robots.txt, verification files)"
        )


def relocate_code(st: Run) -> None:
    """Step 4: committed code to web/app/, then what remains of wp-content (the
    Composer-replaced copies) into the backup."""
    root = st.root
    for item in st.committed:
        src, dest = root / item.source, root / item.target
        if dest.exists():
            # Done by an earlier run: what is left in wp-content goes to the backup below.
            continue
        if move(src, dest):
            ui.info(f"→ {item.target}")
    wpc = root / "wp-content"
    if wpc.is_dir():
        move(wpc, backup(st) / "legacy" / "wp-content-rest")


def keep_salts(st: Run) -> None:
    salts = {k: v for k, v in st.legacy_salts.items() if v}
    if not salts:
        st.warn("--keep-salts: no salts found in wp-config.php")
        return
    path = backup(st) / "salts.env"
    path.write_text("".join(f"{k}='{v}'\n" for k, v in salts.items()))
    path.chmod(0o600)
    st.warn(
        f"--keep-salts: the legacy salts are in {path.relative_to(st.root)}; provision them as the "
        "site's salts secret in cluster-ovh-01 (they are not committed)"
    )


def stage(st: Run) -> None:
    ui.step("Staging")
    staged = len((probe(git("add", "-An", cwd=str(st.root))) or "").splitlines())
    if staged > STAGE_LIMIT:
        st.warn(f"{staged} files would be staged — that usually means .gitignore is wrong")
        if not st.args.assume_yes and not ui.confirm(f"Stage {staged} files anyway?"):
            ui.die("aborted; nothing was staged (the working tree changes are already applied)")
    run(git("add", "-A", cwd=str(st.root)))


def _step(st: Run, cmd: list[str]) -> None:
    ui.info(f"$ {' '.join(cmd)}")
    rc = attempt(cmd, cwd=str(st.root))
    if rc != 0:
        ui.die(
            f"`{' '.join(cmd)}` failed (exit {rc}). Fix the cause above and rerun "
            "`wordpress-init --migrate`: the steps before it are done and a rerun resumes. "
            "A nix/vendor-hash of lib.fakeHash would fail the site's CI.",
            1,
        )


def foreign_flake(st: Run) -> bool:
    return detect.has_foreign_flake(st.root)


def lock(st: Run, *, composer_changed: bool) -> None:
    """Step 8: composer update, relock, flake lock. Any failure is exit 1."""
    if st.args.skip_lock:
        ui.info("skipping composer update, relock and flake lock (--skip-lock)")
        return
    if foreign_flake(st):
        st.warn("flake.nix is not wordpress-nix's: merge flake.nix.wordpress-nix, then run `nix run .#relock`")
        return
    hash_now = (st.root / "nix" / "vendor-hash").read_text().strip() if (st.root / "nix/vendor-hash").is_file() else ""
    needed = (
        composer_changed
        or not (st.root / "composer.lock").is_file()
        or hash_now == template.FAKE_HASH
        or not (st.root / "flake.lock").is_file()
    )
    if not needed:
        ui.info(". composer.lock, nix/vendor-hash and flake.lock already current")
        return
    ui.step("Locking (composer update, nix run .#relock, nix flake lock)")
    if composer_changed or not (st.root / "composer.lock").is_file():
        _step(st, ["composer", "update", "--no-install", "--no-scripts", "--no-interaction"])
        _step(st, git("add", "composer.lock", cwd=str(st.root)))
    if composer_changed or hash_now == template.FAKE_HASH or not hash_now:
        _step(st, ["nix", "run", ".#relock"])
    _step(st, ["nix", "flake", "lock"])
    _step(st, git("add", "nix/vendor-hash", "flake.lock", cwd=str(st.root)))


def sync(st: Run) -> None:
    """Step 9: the standards-core managed files (wordpress-nix sync --write)."""
    if st.args.action != "sync":
        return
    ui.step("Managed files (wordpress-nix sync --write)")
    cmd = ["wordpress-nix", "sync", "--write"]
    if st.args.standards:
        cmd += ["--standards", st.args.standards]
    if st.args.skip_lock:
        cmd.append("--skip-lock")
    cmd += st.args.sync_args
    _step(st, cmd)
    run(git("add", "-A", cwd=str(st.root)))


def assert_indexed(st: Run) -> None:
    """Step 10."""
    needed = [p for p in gitignore.NEEDED if p != ".envrc"]
    if st.args.skip_lock or foreign_flake(st):
        needed = [p for p in needed if p != "composer.lock"]
    missing = [
        p for p in needed if attempt(git("ls-files", "--error-unmatch", "--", p, cwd=str(st.root)), quiet_stdout=True)
    ]
    if missing:
        ui.die(f"{', '.join(missing)} did not reach the git index")
    for item in st.committed:
        if not probe(git("ls-files", "--", item.target, cwd=str(st.root))):
            ui.die(f"committed {item.kind} {item.name} has no tracked files under {item.target}")
    vh = (st.root / "nix" / "vendor-hash").read_text().strip()
    if vh == template.FAKE_HASH and not st.args.skip_lock and not foreign_flake(st):
        ui.die("nix/vendor-hash is still lib.fakeHash: run `nix run .#relock`")


def has_staged(st: Run) -> bool:
    return attempt(git("diff", "--cached", "--quiet", cwd=str(st.root))) != 0


def commit(st: Run, message: str) -> None:
    if not has_staged(st):
        ui.info("nothing to commit")
        return
    run(git("commit", "-q", "-m", message, cwd=str(st.root)))
    ui.info(f"committed: {message}")


def loosen(st: Run) -> None:
    """Step 11: the second commit."""
    if not st.args.commit or st.args.no_loosen or st.mode != "migrate":
        return
    if st.args.skip_lock or foreign_flake(st):
        ui.info("not loosening plugin constraints (needs the lock step)")
        return
    if not composer_json.loosen(st.root):
        return
    ui.step("Loosening wp-plugin/* and wp-theme/* to ^x.y")
    _step(st, ["composer", "update", "--lock", "--no-install", "--no-scripts", "--no-interaction"])
    run(git("add", "composer.json", "composer.lock", cwd=str(st.root)))
    _step(st, ["nix", "run", ".#relock"])
    run(git("add", "-A", cwd=str(st.root)))
    commit(st, LOOSEN_COMMIT_MSG)


def print_report(st: Run) -> None:
    ui.step(f"Done — {os.getcwd()}")
    counts = {d: len(st.by_disp(d)) for d in ("composer", "avunu", "commit", "drop")}
    if st.items:
        ui.info(
            f"composer {counts['composer']}, avunu {counts['avunu']}, "
            f"committed {counts['commit']}, dropped {counts['drop']}"
        )
    dropped = sorted({i.name for i in st.dropped if i.kind == "plugin" and i.active})
    if dropped:
        ui.info(f"dropPlugins (deactivated by `site import`): {', '.join(dropped)}")
    writers = sorted({i.name for i in st.items if i.has("writes-to-disk") or i.has("mpdf-tempdir")})
    if writers:
        ui.info(f"runtime writers: {', '.join(writers)}")
    if st.warnings:
        ui.out(f"\n\033[33m{len(st.warnings)} warning(s):\033[0m")
        for w in st.warnings:
            ui.out(f"  ⚠  {w.splitlines()[0]}")
    committed = "" if st.args.commit else "\n  git diff --cached --stat     # review; nothing has been committed"
    ui.out(
        f"""
Next steps:{committed}
  direnv allow                 # or: nix develop --no-pure-eval
  devenv up                    # FrankenPHP, MariaDB, Mailpit
  wp restore                   # (in another shell) the newest dev-restore dump,
                               # or: wp db import <the audited dump>
  wordpress-nix repo adopt {st.name}"""
    )


def run_pipeline(st: Run) -> None:
    root = st.root
    ui.step("Preparing the site tree")
    prepare_backup(st)
    if st.mode == "migrate":
        save_plan(st)
        if (root / ".gitignore").is_file() and gitignore.BEGIN not in (root / ".gitignore").read_text():
            move(root / ".gitignore", backup(st) / "legacy" / ".gitignore")
            ui.info("→ .wordpress-nix-backup/legacy/.gitignore")
        relocate_legacy(st)
        if st.args.keep_salts:
            keep_salts(st)
    template.render_template(st)
    template.install_template(st, keep=True)
    if foreign_flake(st):
        shutil.copy(st.staging / "flake.nix", root / "flake.nix.wordpress-nix")
        st.warn(
            "flake.nix is not a wordpress-nix flake; the site flake was written to flake.nix.wordpress-nix — merge it by hand"
        )
    template.write_site_json(st)
    if not (root / "secrets").is_dir() and st.mode == "init":
        (root / "secrets").mkdir()
        (root / "secrets" / "README.md").write_text(SECRETS_README)

    if st.mode == "migrate":
        ui.step("Code")
        relocate_code(st)
    before = (root / "composer.json").read_text() if (root / "composer.json").is_file() else None
    composer_json.reconcile(st)
    composer_changed = before != (root / "composer.json").read_text()
    gitignore.install_block(st)
    gitignore.verify_not_ignored(st)
    verify_secrets_visible(st)

    stage(st)
    lock(st, composer_changed=composer_changed)
    sync(st)
    assert_indexed(st)
    if st.args.commit:
        commit(st, st.args.commit_msg)
    loosen(st)
    print_report(st)
