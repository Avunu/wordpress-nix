"""``wordpress-init``'s command line, parsed by hand as frappe-init's is.

- a usage error is exit 1, except on a line with ``--sync`` or ``--check``, where it
  is 2 (1 is drift there);
- ``--commit`` takes its message only as ``--commit=<msg>``, so ``--commit <dir>``
  is the flag and a target;
- repeatable flags (``--keep-plugin``, ``--drop-plugin``, ``--accept-modified``)
  collect every value.
"""

from dataclasses import dataclass, field

USAGE = """\
Usage: wordpress-init [options] [target-dir]

Scaffold a new wordpress-nix site (stock Roots Bedrock), convert a pulled classic
WordPress install in place, or reconcile a site that already is one. The mode is
detected from the target directory (default: the current directory when it holds
a WordPress install or a site, otherwise a new site).

Mode:
  --sync                     Write this site's managed files (wordpress-nix sync --write)
  --check                    Report drift in them and change nothing (wordpress-nix sync --check)
  --init                     Force scaffold mode
  --migrate                  Force migration mode (also reconciles a wordpress-nix site)
  --force                    Relax the mode guard; never overwrites an existing file
  --dry-run                  Print the plan and exit without changing anything
  -y, --yes                  Assume yes (required to migrate in a non-TTY)

Common:
  --version <x.y.z>          The WordPress core version to pin (overrides detection)
  --name <slug>              The site's slug (default: the directory's name)
  --home-url <url>           The site's home URL (default: the survey's home)
  --php <php83|php84|php85>  The PHP the site builds with (default: php84)
  --skip-lock                Do not run composer update, nix run .#relock or nix flake lock
  --strict                   Treat warnings as errors (exit 1)
  --commit[=<msg>]           Commit the result instead of only staging it
  -h, --help                 Show this help

Migration only:
  --survey <file>            The legacy survey (default: .wordpress-nix-backup/survey.json)
  --dump <file>              The legacy dump, read for the active set when there is no survey
  --keep-plugin <slug>       Keep a plugin the plan would drop, or resolve its db-code flag
  --drop-plugin <slug>       Drop a plugin the plan would keep
  --keep-inactive            Keep inactive plugins and themes
  --keep-salts               Keep the legacy salts (only for salt-dependent sites on a trusted host)
  --accept-modified <slug>:<replace|vendor>
                             A modified wordpress.org copy: replace it with the
                             Composer package, or commit (vendor) the modified copy
  --no-loosen                With --commit, skip the second commit that loosens plugin constraints

With a terminal and no flags you are prompted for what is missing.
"""

DEFAULT_COMMIT_MSG = "feat: convert to Bedrock (wordpress-nix)"
LOOSEN_COMMIT_MSG = "chore(deps): loosen plugin constraints"

VALUED = {
    "--version": "version",
    "--name": "name",
    "--home-url": "home_url",
    "--php": "php",
    "--survey": "survey",
    "--dump": "dump",
    "--standards": "standards",
}
REPEATED = {
    "--keep-plugin": "keep_plugins",
    "--drop-plugin": "drop_plugins",
    "--accept-modified": "accept_modified",
}
SYNC_VALUED = ("--only", "--format", "--expect-rev", "--profile-path")
CHECK_ONLY = ("--format", "--expect-rev")

SWITCHES = {
    "--force": "force",
    "--dry-run": "dry_run",
    "-y": "assume_yes",
    "--yes": "assume_yes",
    "--skip-lock": "skip_lock",
    "--strict": "strict",
    "--keep-inactive": "keep_inactive",
    "--keep-salts": "keep_salts",
    "--no-loosen": "no_loosen",
}


class UsageError(Exception):
    def __init__(self, message: str, code: int, *, show_usage: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.show_usage = show_usage


@dataclass
class InitArgs:
    # auto | init | migrate
    mode: str = "auto"
    # sync | check | ""
    action: str = ""
    force: bool = False
    dry_run: bool = False
    assume_yes: bool = False
    skip_lock: bool = False
    strict: bool = False
    commit: bool = False
    commit_msg: str = DEFAULT_COMMIT_MSG
    keep_inactive: bool = False
    keep_salts: bool = False
    no_loosen: bool = False
    version: str = ""
    name: str = ""
    home_url: str = ""
    php: str = ""
    survey: str = ""
    dump: str = ""
    standards: str = ""
    keep_plugins: list[str] = field(default_factory=list)
    drop_plugins: list[str] = field(default_factory=list)
    accept_modified: list[str] = field(default_factory=list)
    target: str = ""
    sync_args: list[str] = field(default_factory=list)
    help: bool = False

    def accepted(self) -> dict[str, str]:
        """``--accept-modified`` as slug → replace | vendor."""
        out = {}
        for spec in self.accept_modified:
            slug, _, how = spec.partition(":")
            out[slug] = how
        return out


def parse_args(argv: list[str]) -> InitArgs:
    args = InitArgs()
    has_action = any(w in ("--sync", "--check") for w in argv)
    usage_code = 2 if has_action else 1
    unknown = ""
    words = list(argv)
    while words:
        word = words.pop(0)
        flag, eq, inline = word.partition("=")
        if not word.startswith("-"):
            args.target = word
            continue
        if not eq and (word in VALUED or word in REPEATED or word in SYNC_VALUED) and not words:
            raise UsageError(f"{word} needs a value", usage_code, show_usage=False)
        if flag in CHECK_ONLY and not has_action:
            raise UsageError(f"unknown flag: {word}", 1)
        if flag in VALUED:
            setattr(args, VALUED[flag], inline if eq else words.pop(0))
        elif flag in REPEATED:
            getattr(args, REPEATED[flag]).append(inline if eq else words.pop(0))
        elif flag in SYNC_VALUED:
            args.sync_args.extend([word] if eq else [word, words.pop(0)])
        elif word == "--commit":
            args.commit = True
        elif flag == "--commit" and eq:
            args.commit = True
            args.commit_msg = inline
        elif word in ("--sync", "--check"):
            args.action = word[2:]
        elif word in ("--init", "--migrate"):
            args.mode = word[2:]
        elif word in SWITCHES:
            setattr(args, SWITCHES[word], True)
        elif word in ("-h", "--help"):
            args.help = True
            return args
        elif not has_action:
            raise UsageError(f"unknown flag: {word}", 1)
        elif not unknown:
            unknown = word
    if unknown:
        raise UsageError(f"unknown flag: {unknown}", 2)
    for spec in args.accept_modified:
        slug, sep, how = spec.partition(":")
        if not slug or not sep or how not in ("replace", "vendor"):
            raise UsageError(
                f"--accept-modified {spec!r}: expected <slug>:replace or <slug>:vendor",
                usage_code,
                show_usage=False,
            )
    if args.php and args.php not in ("php83", "php84", "php85"):
        raise UsageError(f"--php {args.php!r}: expected php83, php84 or php85", usage_code, show_usage=False)
    return args
