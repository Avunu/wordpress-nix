"""``wordpress-nix db rewrite-urls``: the URL rewrite sequence (plan §4.9), one
command, against whichever ``wp`` it is given: the dev shell's on PATH, or the
container's through ``site import``.

1. Attachments → ``https://media.<domain>/``, while the database still names the
   legacy domain: ``wp search-replace --regex --all-tables-with-prefix --precise``
   with ``(https?:)?//(www.)?<domain>/wp-content/uploads/(?!(?:<excluded>)/)``,
   plain and JSON-escaped. The excluded list is the non-attachment directories
   (``--exclude-prefix-from inventory.json``) and ``S3_EXCLUDE_PREFIXES``.
2. The remaining legacy content paths of that host, ``…/wp-content/`` →
   ``https://<domain>/app/``, plain and escaped. Other hosts are never touched.
3. Only when ``--to-home`` differs (staging): the host itself → ``--to-home``.
4. ``wp core update-db``, ``wp rewrite flush``; the Cwicly CSS and ``wp_font_face``
   URLs are a checklist item.

Step 1 is idempotent on URLs already on ``media.<domain>``: it is also the adopt
mode for a partially offloaded site.
"""

import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from wordpress_nix_tools.common import data

# (argv after `wp`) → (status, stdout, stderr)
Wp = Callable[[Sequence[str]], tuple[int, str, str]]


@dataclass(frozen=True)
class Step:
    pattern: str
    replacement: str
    regex: bool = True
    label: str = ""


def pcre_escape(text: str) -> str:
    return re.sub(r"([.\\+*?\[^\]$(){}=!<>|:#/-])", r"\\\1", text)


def prefix_alternation(prefixes: Sequence[str]) -> str:
    """``wpo_wcpdf*`` → ``wpo_wcpdf[^/]*``; the rest escaped; sorted and unique."""
    out = []
    for p in sorted({p.strip().strip("/") for p in prefixes if p.strip()}):
        parts = p.split("*")
        out.append("[^/]*".join(pcre_escape(x) for x in parts))
    return "|".join(out)


def host_of(url: str) -> str:
    host = urlparse(url if "//" in url else f"https://{url}").hostname or url
    return host[4:] if host.startswith("www.") else host


def build_steps(from_home: str, to_home: str, media_url: str, exclude: Sequence[str]) -> list[Step]:
    domain = host_of(from_home)
    d = r"(?:www\.)?" + pcre_escape(domain)
    media = media_url.rstrip("/")
    alt = prefix_alternation(exclude)
    look_plain = f"(?!(?:{alt})/)" if alt else ""
    # In JSON-escaped text a "/" is "\/": the lookahead's own slashes too.
    alt_json = alt.replace(r"\/", r"\\\/").replace("[^/]", r"[^/\\]")
    look_json = f"(?!(?:{alt_json})\\\\/)" if alt else ""
    base = f"https://{domain}"
    steps = [
        Step(f"(https?:)?//{d}/wp-content/uploads/{look_plain}", f"{media}/", label="attachments → media"),
        Step(
            rf"(https?:)?\\/\\/{d}\\/wp-content\\/uploads\\/{look_json}",
            media.replace("/", r"\/") + r"\/",
            label="attachments → media (JSON-escaped)",
        ),
        Step(f"(https?:)?//{d}/wp-content/", f"{base}/app/", label="content paths → /app/"),
        Step(
            rf"(https?:)?\\/\\/{d}\\/wp-content\\/",
            base.replace("/", r"\/") + r"\/app\/",
            label="content paths → /app/ (JSON-escaped)",
        ),
    ]
    if to_home and to_home.rstrip("/") != base:
        target = to_home.rstrip("/")
        end = r"(?![\w.-])"
        steps += [
            Step(f"(https?:)?//{d}{end}", target, label="host → --to-home"),
            Step(rf"(https?:)?\\/\\/{d}{end}", target.replace("/", r"\/"), label="host → --to-home (JSON-escaped)"),
        ]
    return steps


def exclude_prefixes(inventory: str | None, extra: Sequence[str] = ()) -> list[str]:
    """The lookahead list: inventory.json's directory classes, the data file's
    excluded prefixes, and S3_EXCLUDE_PREFIXES."""
    out = list(data.load("runtime-writers").get("excludePrefixes", []))
    if inventory and Path(inventory).is_file():
        inv = json.loads(Path(inventory).read_text())
        out += inv.get("excludePrefixes", [])
    out += list(extra)
    return sorted(set(out))


def search_replace_argv(step: Step, *, dry_run: bool, report: bool) -> list[str]:
    argv = ["search-replace", step.pattern, step.replacement, "--all-tables-with-prefix", "--precise"]
    if step.regex:
        argv += ["--regex", "--regex-delimiter=~"]
    if dry_run:
        argv.append("--dry-run")
    if report:
        argv.append("--report-changes-only")
    else:
        argv.append("--no-report")
    return argv


def local_wp(argv: Sequence[str]) -> tuple[int, str, str]:
    exe = shutil.which("wp")
    if not exe:
        return 127, "", "wp: command not found (run inside the dev shell, or through site import)"
    p = subprocess.run([exe, *argv], capture_output=True, text=True, check=False)
    return p.returncode, p.stdout, p.stderr


def run(
    wp: Wp,
    steps: Sequence[Step],
    *,
    dry_run: bool = False,
    report: bool = False,
    after: bool = True,
    log=print,
) -> int:
    for step in steps:
        rc, out, err = wp(search_replace_argv(step, dry_run=dry_run, report=report))
        log(f"{step.label}: {'ok' if rc == 0 else f'exit {rc}'}")
        if report and out.strip():
            log(out.rstrip())
        if rc != 0:
            log(err.rstrip())
            return 1
    if after and not dry_run:
        for argv in (["core", "update-db"], ["rewrite", "flush"]):
            rc, _, err = wp(argv)
            log(f"wp {' '.join(argv)}: {'ok' if rc == 0 else f'exit {rc}'}")
            if rc != 0:
                log(err.rstrip())
                return 1
        rc, out, _ = wp(["post", "list", "--post_type=wp_font_face", "--format=count"])
        if rc == 0 and out.strip() not in ("", "0"):
            log(f"check: {out.strip()} wp_font_face post(s): their URLs should be on /app/uploads/fonts/")
        log("check: regenerate the Cwicly CSS (save once in the Cwicly editor) if the site uses Cwicly")
    return 0


def cmd(args, wp: Wp = local_wp) -> int:
    media = args.media_url or f"https://media.{host_of(args.from_home)}"
    extra = [p for p in (args.exclude_prefix or [])]
    steps = build_steps(args.from_home, args.to_home or "", media, exclude_prefixes(args.exclude_prefix_from, extra))
    if args.print:
        for s in steps:
            print(json.dumps({"label": s.label, "pattern": s.pattern, "replacement": s.replacement}))
        return 0
    try:
        return run(wp, steps, dry_run=args.dry_run, report=args.report_changes)
    except OSError as e:
        print(f"db rewrite-urls: {e}", file=sys.stderr)
        return 3
