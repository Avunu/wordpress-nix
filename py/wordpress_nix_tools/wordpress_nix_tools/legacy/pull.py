"""``wordpress-nix legacy pull``: the code and the full dump, in one SSH session
(§8.3), into a local directory the scaffolder then converts.

- One session: an SSH ControlMaster is opened first, and rsync, the dump and the
  survey all ride on it (they wait for its socket and never open their own
  connection: ``ControlMaster=no``); both timestamps go into provenance.json.
- Code: rsync of the install, without uploads, caches, wflogs, drop-ins, logs and
  backups (uploads move with ``media offload``).
- Dump: ``mysqldump --single-transaction --triggers --routines --events --hex-blob``,
  credentials from wp-config.php through the host's ``php`` and handed to mysqldump
  on a file descriptor (never argv, never a file on the host). The working copy is
  ``.wordpress-nix-backup/dump.sql`` (0600); the full raw dump is also kept as
  evidence, encrypted with age to ``--age-recipient`` (or
  ``WORDPRESS_NIX_AGE_RECIPIENT``).
- ``survey.json`` beside it, so ``wordpress-init`` knows the active set.

Nothing is written on the legacy host.
"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from wordpress_nix_tools.common import records
from wordpress_nix_tools.common.remote import Remote, RemoteError
from wordpress_nix_tools.legacy import survey as surveymod

RSYNC_EXCLUDES = (
    "wp-content/uploads/",
    "wp-content/cache/",
    "wp-content/upgrade/",
    "wp-content/upgrade-temp-backup/",
    "wp-content/wflogs/",
    "wp-content/backup*/",
    "wp-content/ai1wm-backups/",
    "wp-content/updraft/",
    "wp-content/wpvividbackups/",
    "wp-content/object-cache.php",
    "wp-content/advanced-cache.php",
    "wp-content/db.php",
    "*.log",
    "error_log",
    ".git/",
)

MYSQLDUMP_ARGS = (
    "--single-transaction",
    "--triggers",
    "--routines",
    "--events",
    "--hex-blob",
    "--no-tablespaces",
    "--default-character-set=utf8mb4",
)

# On the host: the client options from wp-config.php on fd 3, then mysqldump.
DUMP_SH = r"""
set -euo pipefail
cd "$1"
opts=$(php -r '
  error_reporting(E_ERROR);
  define("ABSPATH", getcwd() . "/");
  $s = file_get_contents("wp-config.php");
  $s = preg_replace("/^\s*(require|include)(_once)?[^;]*wp-settings\.php[^;]*;/m", "", $s);
  eval("?>" . $s);
  $h = DB_HOST; $port = ""; $sock = "";
  if (strpos($h, ":") !== false) { list($h, $r) = explode(":", $h, 2); if (is_numeric($r)) { $port = $r; } else { $sock = $r; } }
  echo "[client]\nuser=" . DB_USER . "\npassword=\"" . addcslashes(DB_PASSWORD, "\\\"") . "\"\nhost=" . $h . "\n";
  if ($port !== "") { echo "port=$port\n"; }
  if ($sock !== "") { echo "socket=$sock\n"; }
  echo "#db=" . DB_NAME . "\n";
')
db=$(printf '%s\n' "$opts" | sed -n 's/^#db=//p')
shift
exec 3<<<"$opts"
# --defaults-file, not -extra-file: the login user's ~/.my.cnf is read after an
# extra file, and its user= (root on xCloud hosts) would replace the site's.
exec mysqldump --defaults-file=/dev/fd/3 "$@" "$db"
"""


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def control_path(tmp: str, host: str) -> str:
    return os.path.join(tmp, "cm-" + hashlib.sha256(host.encode()).hexdigest()[:12])


def ssh_args(cp: str) -> tuple[str, ...]:
    return ("ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", f"ControlPath={cp}")


def master_check(cp: str, host: str) -> bool:
    argv = ["ssh", "-o", f"ControlPath={cp}", "-O", "check", host]
    return subprocess.run(argv, capture_output=True, check=False).returncode == 0


def wait_for_master(
    master,
    cp: str,
    host: str,
    *,
    check: Callable[[str, str], bool] = master_check,
    timeout: float = 60,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Until the ControlMaster's socket answers: a client started before it would
    open a connection of its own (a second authentication)."""
    waited = 0.0
    while True:
        rc = master.poll()
        if rc is not None:
            raise RemoteError(host, "ssh (the ControlMaster)", rc, "the master connection exited")
        if check(cp, host):
            return
        if waited >= timeout:
            raise RemoteError(host, "ssh (the ControlMaster)", 255, f"no control socket after {timeout:.0f}s")
        sleep(0.25)
        waited += 0.25


def rsync_argv(host: str, path: str, into: Path, cp: str) -> list[str]:
    argv = ["rsync", "-a", "--info=stats1", "-e", " ".join(shlex.quote(a) for a in ssh_args(cp))]
    for ex in RSYNC_EXCLUDES:
        argv += ["--exclude", ex]
    return [*argv, f"{host}:{path.rstrip('/')}/", f"{into}/"]


def dump_to(remote: Remote, path: str, out: Path, *, stream: bool = True) -> str:
    """mysqldump to OUT (0600), streamed through a pipe (``stream``) or through the
    remote's runner (the tests' fake); its sha256."""
    cmd = remote.argv("bash -s -- " + " ".join(shlex.quote(a) for a in (path, *MYSQLDUMP_ARGS)))
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    h = hashlib.sha256()
    with os.fdopen(fd, "wb") as f:
        if not stream:
            rc, data, err = remote.runner(cmd, DUMP_SH.encode())
            f.write(data)
            h.update(data)
        else:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert proc.stdin and proc.stdout and proc.stderr
            proc.stdin.write(DUMP_SH.encode())
            proc.stdin.close()
            for block in iter(lambda: proc.stdout.read(1 << 20), b""):
                f.write(block)
                h.update(block)
            err = proc.stderr.read().decode(errors="replace")
            rc = proc.wait()
    if rc != 0:
        raise RemoteError(remote.host, "mysqldump", rc, err)
    return h.hexdigest()


def encrypt(dump: Path, recipient: str, out: Path) -> bool:
    if not recipient:
        return False
    flag = "-R" if os.path.isfile(os.path.expanduser(recipient)) else "-r"
    rc = subprocess.run(["age", flag, os.path.expanduser(recipient), "-o", str(out), str(dump)], check=False).returncode
    return rc == 0


def cmd(args, runner=None) -> int:
    into = Path(args.into)
    into.mkdir(parents=True, exist_ok=True)
    backup = records.backup_dir(into)
    backup.mkdir(exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="wordpress-nix-pull.")
    cp = control_path(tmp, args.host)
    remote = (
        Remote(args.host, ssh=ssh_args(cp)) if runner is None else Remote(args.host, runner=runner, ssh=ssh_args(cp))
    )
    prov: dict = {"host": args.host, "path": args.path, "source": "legacy pull"}
    master = None
    try:
        if runner is None:
            master = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=yes", "-o", f"ControlPath={cp}", "-N", args.host]
            )
            wait_for_master(master, cp, args.host)
        if not args.db_only:
            prov["codePulledAt"] = now()
            argv = rsync_argv(args.host, args.path, into, cp)
            rc = runner(argv, None)[0] if runner else subprocess.run(argv, check=False).returncode
            if rc != 0:
                print(f"legacy pull: rsync exited {rc}", file=sys.stderr)
                return 3
            prov["codeExcludes"] = list(RSYNC_EXCLUDES)
        prov["dumpTakenAt"] = now()
        dump = backup / "dump.sql"
        prov["dumpSha256"] = dump_to(remote, args.path, dump, stream=runner is None)
        prov["mysqldump"] = list(MYSQLDUMP_ARGS)
        recipient = args.age_recipient or os.environ.get("WORDPRESS_NIX_AGE_RECIPIENT", "")
        stamp = prov["dumpTakenAt"].replace(":", "").replace("-", "")
        evidence = backup / f"dump-{stamp}.sql.age"
        if encrypt(dump, recipient, evidence):
            prov["evidence"] = evidence.name
        else:
            print(
                "legacy pull: the raw dump is not kept as age-encrypted evidence: pass --age-recipient "
                "(or set WORDPRESS_NIX_AGE_RECIPIENT)",
                file=sys.stderr,
            )
        s = surveymod.run_survey(remote, args.path)
        surveymod.cloudflare_status(s, surveymod._cf())
        records.write(backup / "survey.json", s)
        records.write(backup / "provenance.json", prov)
    except RemoteError as e:
        print(f"legacy pull: {e}", file=sys.stderr)
        return 3
    finally:
        if master is not None and master.poll() is None:
            master.terminate()
            master.wait()
    print(json.dumps(prov, indent=2))
    print(f"\nNext: wordpress-nix db audit {backup / 'dump.sql'} --survey {backup / 'survey.json'}")
    return 0
