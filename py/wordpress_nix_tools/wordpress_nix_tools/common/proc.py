"""Running git, composer, nix, ssh and rclone.

- ``run`` is a step whose failure stops the command with the step's own status
  (ui.Exit), after the step has said why on stderr;
- ``attempt`` is a step whose failure the caller handles: its status;
- ``probe`` and ``probe_ok`` ask a question (stdout captured, stderr discarded) and
  never fail;
- ``capture`` runs and returns ``(status, stdout, stderr)``, for callers that report
  the failure themselves.
"""

import signal
import subprocess
import sys
from collections.abc import Sequence

from wordpress_nix_tools.common.ui import Exit


def _wait(proc: subprocess.Popen) -> int:
    try:
        return proc.wait()
    except KeyboardInterrupt:
        try:
            proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGKILL)
            proc.wait()
        raise Exit(130) from None


def attempt(cmd: Sequence[str], *, quiet_stdout: bool = False, cwd: str | None = None) -> int:
    """Run CMD with its output streaming through; its exit status."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        proc = subprocess.Popen(list(cmd), stdout=subprocess.DEVNULL if quiet_stdout else None, cwd=cwd)
    except FileNotFoundError:
        print(f"wordpress-nix: {cmd[0]}: command not found", file=sys.stderr, flush=True)
        return 127
    return _wait(proc)


def run(cmd: Sequence[str], *, quiet_stdout: bool = False, cwd: str | None = None) -> None:
    """Run CMD; stop with its status when it fails."""
    rc = attempt(cmd, quiet_stdout=quiet_stdout, cwd=cwd)
    if rc != 0:
        raise Exit(rc)


def capture(cmd: Sequence[str], *, input: bytes | str | None = None, cwd: str | None = None) -> tuple[int, str, str]:
    """CMD's status, stdout and stderr (text)."""
    data = input.encode() if isinstance(input, str) else input
    try:
        proc = subprocess.run(list(cmd), input=data, capture_output=True, cwd=cwd, check=False)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    return (
        proc.returncode,
        proc.stdout.decode(errors="replace"),
        proc.stderr.decode(errors="replace"),
    )


def probe(cmd: Sequence[str], *, cwd: str | None = None) -> str | None:
    """CMD's stdout without its trailing newlines, or None when it fails."""
    rc, out, _ = capture(cmd, cwd=cwd)
    return out.rstrip("\n") if rc == 0 else None


def probe_ok(cmd: Sequence[str], *, cwd: str | None = None) -> bool:
    return capture(cmd, cwd=cwd)[0] == 0


def git(*args: str, cwd: str | None = None) -> list[str]:
    """A git command line, run in CWD (``git -C``) when given."""
    return ["git", *(["-C", cwd] if cwd else []), *args]
