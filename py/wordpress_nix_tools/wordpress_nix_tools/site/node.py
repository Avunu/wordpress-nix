"""The cluster node running a site's container, and commands inside it.

The node: ``--node``, else ``WORDPRESS_NIX_NODE``, else Nomad's running allocation
of the job ``app-<slug>`` (``nomad job allocs -json``; the cluster names every app's
job so). Inside: ``nixos-container run <slug> -- runuser -u wordpress -- <cmd>``
over ``ssh root@<node>``. Streams go through stdin; nothing is staged on the node.
"""

import json
import os
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field

from wordpress_nix_tools.common.remote import Remote, Runner, subprocess_runner


class NodeError(Exception):
    pass


def find_node(slug: str, given: str = "", runner: Runner = subprocess_runner) -> str:
    node = given or os.environ.get("WORDPRESS_NIX_NODE", "")
    if node:
        return node
    rc, out, err = runner(["nomad", "job", "allocs", "-json", f"app-{slug}"], None)
    if rc != 0:
        raise NodeError(f"cannot ask Nomad where app-{slug} runs ({err.strip()}); pass --node")
    try:
        allocs = json.loads(out)
    except ValueError as e:
        raise NodeError(f"nomad job allocs: not JSON ({e})") from e
    running = [a for a in allocs if a.get("ClientStatus") == "running"]
    if len(running) != 1:
        raise NodeError(f"app-{slug} has {len(running)} running allocation(s); pass --node")
    return running[0].get("NodeName") or running[0]["NodeID"]


@dataclass
class Container:
    slug: str
    node: str
    runner: Runner = field(default=subprocess_runner)

    @property
    def remote(self) -> Remote:
        return Remote(f"root@{self.node}", runner=self.runner)

    def run(
        self, argv: Sequence[str], *, user: str | None = "wordpress", stdin: bytes | None = None
    ) -> tuple[int, bytes, str]:
        inner = ["runuser", "-u", user, "--", *argv] if user else list(argv)
        cmd = f"nixos-container run {shlex.quote(self.slug)} -- " + " ".join(shlex.quote(a) for a in inner)
        return self.remote.run(cmd, stdin)

    def host(self, command: str, stdin: bytes | None = None) -> tuple[int, bytes, str]:
        """A command on the node itself (cluster-backup)."""
        return self.remote.run(command, stdin)

    def wp(self, argv: Sequence[str]) -> tuple[int, str, str]:
        rc, out, err = self.run(["wp", *argv])
        return rc, out.decode(errors="replace"), err

    def stream(self, argv: Sequence[str], path: str) -> int:
        """ARGV in the container with the local file PATH on its stdin, streamed."""
        if self.runner is not subprocess_runner:
            with open(path, "rb") as f:
                return self.run(argv, stdin=f.read())[0]
        inner = ["runuser", "-u", "wordpress", "--", *argv]
        cmd = self.remote.argv(
            f"nixos-container run {shlex.quote(self.slug)} -- " + " ".join(shlex.quote(a) for a in inner)
        )
        with open(path, "rb") as f:
            return subprocess.run(cmd, stdin=f, check=False).returncode
