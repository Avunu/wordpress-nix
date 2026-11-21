"""``wordpress-nix db audit``: tools/audit-mysql-dump.py (plan §8.4), the same
program ``nix run .#audit-mysql-dump`` runs, with its flags and exit codes:
0 clean or all accepted, 1 unaccepted findings, 2 a bad argument."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def tool() -> list[str]:
    """The audit program: on PATH (the wordpress-nix package puts it there), else the
    repository checkout's tools/ script."""
    exe = shutil.which("audit-mysql-dump")
    if exe:
        return [exe]
    script = Path(__file__).resolve().parents[4] / "tools" / "audit-mysql-dump.py"
    if script.is_file():
        return [sys.executable, str(script)]
    raise FileNotFoundError("audit-mysql-dump is not on PATH")


def argv(args) -> list[str]:
    out = [args.dump]
    if args.survey:
        out += ["--survey", args.survey]
    if args.strip:
        out.append("--strip")
    if args.out:
        out += ["--out", args.out]
    for a in args.accept or []:
        out += ["--accept", a]
    if args.json:
        out.append("--json")
    return out


def cmd(args) -> int:
    if args.out and not args.strip:
        print("db audit: --out needs --strip", file=sys.stderr)
        return 2
    try:
        exe = tool()
    except FileNotFoundError as e:
        print(f"db audit: {e}", file=sys.stderr)
        return 3
    return subprocess.run([*exe, *argv(args)], env=os.environ, check=False).returncode
