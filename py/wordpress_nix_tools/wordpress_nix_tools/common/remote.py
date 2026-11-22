"""Running commands on another machine: the legacy host (read-only) and the
cluster node that runs a site's container.

``Remote`` is a host and a runner; the runner is ``ssh -o BatchMode=yes`` in
production and a fake in the unit tests. Nothing here writes to a legacy host:
the commands that touch one (survey, pull, verify) pass only read-only scripts.
"""

import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

# (argv, stdin) → (status, stdout bytes, stderr text)
Runner = Callable[[Sequence[str], bytes | None], tuple[int, bytes, str]]


def subprocess_runner(argv: Sequence[str], stdin: bytes | None = None) -> tuple[int, bytes, str]:
    try:
        p = subprocess.run(list(argv), input=stdin, capture_output=True, check=False)
    except FileNotFoundError:
        return 127, b"", f"{argv[0]}: command not found"
    return p.returncode, p.stdout, p.stderr.decode(errors="replace")


@dataclass
class Remote:
    host: str
    runner: Runner = field(default=subprocess_runner)
    ssh: tuple[str, ...] = ("ssh", "-o", "BatchMode=yes")

    def argv(self, command: str) -> list[str]:
        return [*self.ssh, self.host, command]

    def run(self, command: str, stdin: bytes | None = None) -> tuple[int, bytes, str]:
        return self.runner(self.argv(command), stdin)

    def script(self, body: str, *args: str) -> tuple[int, bytes, str]:
        """``bash -s -- args`` with BODY on stdin."""
        cmd = "bash -s -- " + " ".join(shlex.quote(a) for a in args)
        return self.run(cmd, body.encode())

    def text(self, command: str) -> str:
        rc, out, err = self.run(command)
        if rc != 0:
            raise RemoteError(self.host, command, rc, err)
        return out.decode(errors="replace")


class RemoteError(Exception):
    def __init__(self, host: str, command: str, rc: int, err: str) -> None:
        super().__init__(f"{host}: `{command[:80]}` exited {rc}: {err.strip()[-400:]}")
        self.rc = rc


def local(runner: Runner = subprocess_runner) -> Callable[[Sequence[str], bytes | None], tuple[int, bytes, str]]:
    return runner
