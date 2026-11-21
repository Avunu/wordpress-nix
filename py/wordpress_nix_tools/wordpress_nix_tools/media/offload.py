"""``wordpress-nix media offload <slug> --from <ssh-host> --path P --dump F [--adopt]``
(plan §8.6).

- The attachment index comes from the audited dump: each ``_wp_attached_file`` and
  the sizes and original image in its ``_wp_attachment_metadata``, minus the
  excluded prefixes (data/runtime-writers.json ``excludePrefixes`` and the legacy
  ``S3_EXCLUDE_PREFIXES``).
- Those files go from the legacy host (sftp, read-only) straight to R2:
  ``rclone copy --files-from`` (resumable), then ``rclone check --one-way``.
- Everything else under uploads (``cwicly/``, ``fonts/``, ``wpcodebox/``, private
  directories, unknowns) is packed into ``.wordpress-nix-backup/private.tar.gz`` for
  the JuiceFS volume (``site import --files``). Executables are never copied, except
  the exact WPCodeBox stub, and are reported.
- ``inventory.json`` records the directory classes; it feeds ``db rewrite-urls
  --exclude-prefix-from``.
- ``--adopt`` (automatic when the legacy config defines ``S3_BUCKET``): attachments
  already in the bucket are skipped; one missing on the source but present in the
  bucket is fine; one in neither place fails the run, with the list.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

from wordpress_nix_tools.common import data, records
from wordpress_nix_tools.common.phpserial import PhpSerialError, loads
from wordpress_nix_tools.db import dump as dumpmod
from wordpress_nix_tools.media.provision import bucket_name

EXECUTABLE = re.compile(r"\.(ph(p\d?|tml|ar|ps|t)|cgi|pl|py|sh|exe)$|(^|/)\.htaccess$|(^|/)\.user\.ini$", re.I)
STUB_PATH = "wpcodebox/index.php"


def attachment_paths(attachments: dict[str, dict[str, str]]) -> set[str]:
    """Every file an attachment owns, relative to uploads: the file, its sizes, the
    original image (WordPress 5.3+ big-image scaling)."""
    out: set[str] = set()
    for att in attachments.values():
        f = (att.get("file") or "").lstrip("/")
        if not f:
            continue
        out.add(f)
        directory = f.rsplit("/", 1)[0] + "/" if "/" in f else ""
        meta = att.get("meta")
        if not meta:
            continue
        try:
            m = loads(meta)
        except PhpSerialError:
            continue
        if not isinstance(m, dict):
            continue
        if isinstance(m.get("original_image"), str):
            out.add(directory + m["original_image"])
        for size in (m.get("sizes") or {}).values():
            if isinstance(size, dict) and isinstance(size.get("file"), str):
                out.add(directory + size["file"])
    return out


def excluded(path: str, prefixes: Sequence[str]) -> bool:
    top = path.split("/", 1)[0]
    for p in prefixes:
        p = p.strip("/")
        if not p:
            continue
        if "/" in p:
            if path.startswith(p + "/"):
                return True
        elif fnmatchcase(top, p):
            return True
    return False


@dataclass
class Plan:
    offload: list[str] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    local: list[str] = field(default_factory=list)
    executables: list[str] = field(default_factory=list)
    excluded_attachments: list[str] = field(default_factory=list)


def plan(
    attachments: set[str],
    source: Iterable[str],
    *,
    prefixes: Sequence[str],
    bucket: Iterable[str] | None = None,
    stub_ok: Callable[[str], bool] = lambda p: True,
) -> Plan:
    source_set = set(source)
    in_bucket = set(bucket or ())
    adopt = bucket is not None
    p = Plan()
    wanted = set()
    for path in sorted(attachments):
        if excluded(path, prefixes):
            p.excluded_attachments.append(path)
            continue
        if EXECUTABLE.search(path):
            p.executables.append(path)
            continue
        wanted.add(path)
        if adopt and path in in_bucket:
            p.adopted.append(path)
        elif path in source_set:
            p.offload.append(path)
        else:
            p.missing.append(path)
    for path in sorted(source_set - wanted):
        if EXECUTABLE.search(path):
            if path == STUB_PATH and stub_ok(path):
                p.local.append(path)
            elif path not in p.executables:
                p.executables.append(path)
            continue
        p.local.append(path)
    return p


def inventory(p: Plan, prefixes: Sequence[str]) -> dict:
    """The directory classes db rewrite-urls reads."""
    local_dirs = sorted({x.split("/", 1)[0] for x in p.local if "/" in x})
    attachment_dirs = sorted({x.split("/", 1)[0] for x in p.offload + p.adopted if "/" in x})
    non_attachment = [d for d in local_dirs if d not in attachment_dirs]
    return {
        "attachments": {"offloaded": len(p.offload), "adopted": len(p.adopted), "missing": len(p.missing)},
        "localDirs": local_dirs,
        "localFiles": len(p.local),
        "executables": p.executables,
        "excludePrefixes": sorted(set(prefixes) | set(non_attachment)),
    }


def s3_env(creds: dict, name: str = "R2") -> dict[str, str]:
    n = name.upper()
    return {
        f"RCLONE_CONFIG_{n}_TYPE": "s3",
        f"RCLONE_CONFIG_{n}_PROVIDER": "Cloudflare",
        f"RCLONE_CONFIG_{n}_ACCESS_KEY_ID": creds["S3_KEY"],
        f"RCLONE_CONFIG_{n}_SECRET_ACCESS_KEY": creds["S3_SECRET"],
        f"RCLONE_CONFIG_{n}_ENDPOINT": creds["S3_ENDPOINT"],
        f"RCLONE_CONFIG_{n}_NO_CHECK_BUCKET": "true",
    }


def sftp_env(host: str, name: str = "SRC") -> dict[str, str]:
    user, _, hostname = host.rpartition("@")
    env = {f"RCLONE_CONFIG_{name}_TYPE": "sftp", f"RCLONE_CONFIG_{name}_HOST": hostname}
    if user:
        env[f"RCLONE_CONFIG_{name}_USER"] = user
    return env


def credentials(args) -> dict:
    if args.credentials:
        raw = json.loads(Path(args.credentials).read_text())
        return raw.get(f"{args.slug}-cloud-files", raw)
    keys = ("S3_KEY", "S3_SECRET", "S3_ENDPOINT")
    found = {k: os.environ.get(f"WORDPRESS_NIX_{k}", "") for k in keys}
    if not all(found.values()):
        raise ValueError(
            "no R2 credentials: --credentials <fragment.json> (media provision's output), or "
            "WORDPRESS_NIX_S3_KEY, WORDPRESS_NIX_S3_SECRET and WORDPRESS_NIX_S3_ENDPOINT"
        )
    return found


Rclone = Callable[[Sequence[str]], tuple[int, str]]


def rclone_runner(env: dict[str, str]) -> Rclone:
    def run(argv: Sequence[str]) -> tuple[int, str]:
        p = subprocess.run(["rclone", *argv], capture_output=True, text=True, env={**os.environ, **env}, check=False)
        if p.returncode != 0:
            sys.stderr.write(p.stderr[-2000:])
        return p.returncode, p.stdout

    return run


def listing(rclone: Rclone, remote: str) -> list[str]:
    rc, out = rclone(["lsf", "-R", "--files-only", remote])
    if rc != 0:
        raise OSError(f"rclone lsf {remote} exited {rc}")
    return [line for line in out.splitlines() if line]


def run_offload(args, rclone: Rclone, site_root: Path) -> int:
    survey = {}
    sp = records.find(site_root, "survey.json")
    if sp:
        survey = records.read(sp)
    cfg = survey.get("config") or {}
    legacy_prefixes = [p for p in re.split(r"[,\s]+", str(cfg.get("S3_EXCLUDE_PREFIXES") or "")) if p]
    prefixes = sorted(set(data.load("runtime-writers").get("excludePrefixes", [])) | set(legacy_prefixes))
    adopt = args.adopt or bool(cfg.get("S3_BUCKET"))
    bucket = args.bucket or (cfg.get("S3_BUCKET") if adopt and cfg.get("S3_BUCKET") else bucket_name(args.slug))

    prefix = args.prefix or dumpmod.detect_prefix(args.dump)
    atts = attachment_paths(dumpmod.attachments(args.dump, prefix))
    src = f"src:{args.path.rstrip('/')}/wp-content/uploads"
    source = listing(rclone, src)
    in_bucket = listing(rclone, f"r2:{bucket}") if adopt else None

    def stub_ok(path: str) -> bool:
        rc, out = rclone(["cat", f"{src}/{path}"])
        return rc == 0 and out.strip() == data.load("runtime-writers").get("wpcodeboxStub", "<?php die();")

    p = plan(atts, source, prefixes=prefixes, bucket=in_bucket, stub_ok=stub_ok)
    print(
        f"attachments: {len(p.offload)} to copy, {len(p.adopted)} already in {bucket}, {len(p.missing)} in neither; "
        f"{len(p.local)} local file(s); {len(p.executables)} executable(s) refused",
        file=sys.stderr,
    )
    for x in p.executables:
        print(f"  refused executable: {x}", file=sys.stderr)
    if p.missing and adopt:
        for x in p.missing[:50]:
            print(f"  in neither place: {x}", file=sys.stderr)
        print(
            f"media offload: {len(p.missing)} attachment(s) are in neither the source nor the bucket", file=sys.stderr
        )
        return 1
    if args.dry_run:
        print(json.dumps(inventory(p, prefixes), indent=2))
        return 0

    with tempfile.TemporaryDirectory(prefix="wordpress-nix-offload.") as tmp:
        files_from = Path(tmp) / "attachments.txt"
        files_from.write_text("".join(f"{x}\n" for x in p.offload))
        if p.offload:
            rc, _ = rclone(
                [
                    "copy",
                    "--files-from-raw",
                    str(files_from),
                    "--transfers",
                    "16",
                    "--checkers",
                    "32",
                    src,
                    f"r2:{bucket}",
                ]
            )
            if rc != 0:
                print("media offload: rclone copy failed (rerun: it resumes)", file=sys.stderr)
                return 3
            rc, _ = rclone(["check", "--one-way", "--files-from-raw", str(files_from), src, f"r2:{bucket}"])
            if rc != 0:
                print("media offload: rclone check found differences", file=sys.stderr)
                return 1
        local_list = Path(tmp) / "local.txt"
        local_list.write_text("".join(f"{x}\n" for x in p.local))
        staging = Path(tmp) / "uploads"
        staging.mkdir()
        if p.local:
            rc, _ = rclone(["copy", "--files-from-raw", str(local_list), src, str(staging)])
            if rc != 0:
                print("media offload: copying the local files failed", file=sys.stderr)
                return 3
        tarball = records.backup_dir(site_root) / "private.tar.gz"
        tarball.parent.mkdir(parents=True, exist_ok=True)
        rc = subprocess.run(["tar", "-czf", str(tarball), "-C", str(staging), "."], check=False).returncode
        if rc != 0:
            return 3
        os.chmod(tarball, 0o600)
    inv = inventory(p, prefixes)
    inv["bucket"] = bucket
    records.write(records.backup_dir(site_root) / "inventory.json", inv)
    print(json.dumps(inv, indent=2))
    print(
        f"\nNext: wordpress-nix site import {args.slug} --dump <import.sql> --files {tarball} --home <URL>",
        file=sys.stderr,
    )
    return 0


def cmd(args, rclone: Rclone | None = None) -> int:
    try:
        env = {**sftp_env(args.from_host)}
        if rclone is None:
            env.update(s3_env(credentials(args)))
            rclone = rclone_runner(env)
        return run_offload(args, rclone, Path(args.site_root))
    except (ValueError, OSError) as e:
        print(f"media offload: {e}", file=sys.stderr)
        return 2 if isinstance(e, ValueError) else 3
