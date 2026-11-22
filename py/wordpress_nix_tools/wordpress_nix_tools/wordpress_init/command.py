"""``wordpress-init``'s entry point: the arguments, the mode, and dispatch.

Exit codes (docs/reference/scaffolder.md): 0 done or a finished --dry-run; 1 any
error without a more specific code, --strict warnings, a blocked plan, a failed
lock step; 2 --init into a directory that is not empty; 3 --sync or --check on a
target that cannot be entered; 4 a flake.nix that is not wordpress-nix's; 5 the
WordPress version (undetectable or below the minimum); 6 the wrong shape
(multisite, not WordPress), a declined confirmation, or no survey or dump to tell
the active set; 7 nothing to migrate; 8 a directory inside another repository.
--sync and --check exit with wordpress-nix sync's codes.
"""

import os
import sys
from pathlib import Path

from wordpress_nix_tools.common import ui
from wordpress_nix_tools.common.proc import attempt
from wordpress_nix_tools.wordpress_init import detect
from wordpress_nix_tools.wordpress_init.args import USAGE, UsageError, parse_args
from wordpress_nix_tools.wordpress_init.config import ConfigError
from wordpress_nix_tools.wordpress_init.config import load as load_config
from wordpress_nix_tools.wordpress_init.presets import PresetError
from wordpress_nix_tools.wordpress_init.presets import load as load_presets
from wordpress_nix_tools.wordpress_init.state import Run

GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")


def decide_mode(st: Run) -> str:
    """The mode for the (non-empty) current directory."""
    a, root, here = st.args, st.root, os.getcwd()
    shape = detect.shape(root)
    st.shape = shape
    if a.mode == "init":
        if not (detect.dir_is_empty(".") or a.force):
            ui.die(
                f"'{here}' is not empty. Use --migrate to convert an existing site, or --init --force to scaffold into it anyway.",
                2,
            )
        return "init"
    if shape == "bedrock":
        if (root / "wp-content").is_dir() and (root / ".wordpress-nix-backup" / "plan.json").is_file():
            return "resume"
        return "reconcile"
    if shape in ("classic", "v2"):
        return "migrate"
    if a.mode == "migrate":
        if a.force:
            return "migrate"
        ui.die(f"'{here}' does not look like a WordPress install (no wp-content/, no wp-includes/).", 6)
    if detect.has_foreign_flake(root):
        ui.die(f"'{here}' has a flake.nix that is not wordpress-nix's and no WordPress install.", 4)
    if (root / "wp-config.php").is_file() or (root / "wp-content").exists():
        ui.die(f"'{here}' has a WordPress skeleton but no plugins, themes or core: nothing to migrate.", 7)
    ui.die(
        f"'{here}' is not empty and is not a WordPress install. Use --init --force to scaffold into it.",
        6,
    )


def cmd_sync(st: Run) -> None:
    """--sync / --check without --migrate: wordpress-nix sync, in the target."""
    a = st.args
    cmd = ["wordpress-nix", "sync", "--write" if a.action == "sync" else "--check"]
    if a.standards:
        cmd += ["--standards", a.standards]
    if a.force and a.action == "sync":
        cmd.append("--force")
    if a.dry_run and a.action == "sync":
        cmd.append("--dry-run")
    if a.skip_lock and a.action == "sync":
        cmd.append("--skip-lock")
    cmd += a.sync_args
    raise ui.Exit(attempt(cmd))


def dispatch(st: Run) -> None:
    from wordpress_nix_tools.wordpress_init.migrate import cmd_migrate, cmd_reconcile, cmd_resume
    from wordpress_nix_tools.wordpress_init.scaffold import cmd_init

    a = st.args
    target = a.target
    if a.action and a.mode != "migrate":
        if target:
            try:
                os.chdir(target)
            except OSError as e:
                ui.die(f"cannot enter {target}: {e.strerror}", 3)
        cmd_sync(st)

    if not target:
        here = Path(".")
        if a.mode in ("migrate", "init") or detect.shape(here) in ("bedrock", "classic", "v2"):
            target = "."
    if target and os.path.isdir(target) and not detect.dir_is_empty(target):
        os.chdir(target)
        mode = decide_mode(st)
    else:
        if a.mode == "migrate":
            ui.die(f"'{target or '.'}' is empty or does not exist — there is nothing to migrate.")
        if target:
            os.makedirs(target, exist_ok=True)
            os.chdir(target)
        mode = "init"
    st.root = Path(".")
    if mode == "init":
        cmd_init(st)
    elif mode == "resume":
        cmd_resume(st)
    elif mode == "reconcile":
        cmd_reconcile(st)
    else:
        cmd_migrate(st)


def main(argv: list[str] | None = None) -> int:
    for var in GIT_ENV:
        os.environ.pop(var, None)
    try:
        try:
            args = parse_args(list(sys.argv[1:] if argv is None else argv))
        except UsageError as e:
            if e.show_usage:
                print(USAGE, file=sys.stderr, end="")
            raise ui.Die(str(e), e.code) from None
        if args.help:
            print(USAGE, end="")
            return 0
        try:
            config = load_config()
            presets = load_presets()
        except (ConfigError, PresetError) as e:
            raise ui.Die(str(e)) from None
        os.environ.pop("WORDPRESS_NIX_CLI_CONFIG", None)
        st = Run(args=args, config=config, presets=presets)
        dispatch(st)
    except ui.Die as e:
        ui.error_line(str(e))
        return e.code
    except ui.Exit as e:
        return e.code
    except KeyboardInterrupt:
        return 130
    finally:
        sys.stdout.flush()
    return 0


def entry() -> None:
    sys.exit(main())
