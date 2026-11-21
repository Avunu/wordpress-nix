"""``wordpress-nix repo adopt <slug> [--check]``: where the converted site's history
goes on GitHub (D6). Asks GitHub, never local state, what exists (703742d).

- No ``<org>/site-<slug>``: create it (private) and push.
- One whose history holds ``*.sql``, a ``wp-config.php`` or ``.env`` (customer PII
  and password hashes): rename it ``site-<slug>-legacy``, archive it, create a fresh
  ``site-<slug>`` and push. Reversible; no force-push, no history rewrite.
- Otherwise: push the conversion as a branch and open a pull request.

``--check`` prints the decision and changes nothing. Every push is verified on the
remote (moonshot's push_repo): the branch's tip there must be the local HEAD.
Then ``wordpress-nix repo apply`` sets the repository's policy.
"""

import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from wordpress_nix_tools.common.remote import subprocess_runner

Runner = Callable[[Sequence[str], bytes | None], tuple[int, bytes, str]]
CONVERT_BRANCH = "wordpress-nix/v3"


@dataclass
class Decision:
    action: str  # create | replace | pr
    repo: str
    reasons: list[str] = field(default_factory=list)


def disqualifying(paths: list[str]) -> list[str]:
    out = set()
    for p in paths:
        base = p.rsplit("/", 1)[-1]
        if base.endswith((".sql", ".sql.gz")) or base in ("wp-config.php", ".env", "auth.json"):
            out.add(p)
    return sorted(out)


class Adopter:
    def __init__(self, run: Runner = subprocess_runner, cwd: str = ".") -> None:
        self._run = run
        self.cwd = cwd

    def run(self, *argv: str) -> tuple[int, str, str]:
        rc, out, err = self._run(list(argv), None)
        return rc, out.decode(errors="replace") if isinstance(out, bytes) else out, err

    def git(self, *argv: str) -> tuple[int, str, str]:
        return self.run("git", "-C", self.cwd, *argv)

    def exists(self, repo: str) -> bool:
        return self.run("gh", "repo", "view", repo, "--json", "name")[0] == 0

    def history(self, repo: str) -> list[str]:
        tmp = tempfile.mkdtemp(prefix="wordpress-nix-adopt.")
        try:
            rc, _, err = self.run(
                "git",
                "clone",
                "--quiet",
                "--bare",
                "--filter=blob:none",
                f"https://github.com/{repo}.git",
                f"{tmp}/r.git",
            )
            if rc != 0:
                raise OSError(f"cloning {repo}: {err.strip()}")
            rc, out, err = self.run(
                "git", "-C", f"{tmp}/r.git", "log", "--all", "--pretty=format:", "--name-only", "--diff-filter=A"
            )
            if rc != 0:
                raise OSError(f"reading {repo}'s history: {err.strip()}")
            return [line.strip() for line in out.splitlines() if line.strip()]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def decide(self, org: str, slug: str) -> Decision:
        repo = f"{org}/site-{slug}"
        if not self.exists(repo):
            return Decision("create", repo, [f"{repo} does not exist"])
        bad = disqualifying(self.history(repo))
        if bad:
            return Decision("replace", repo, [f"its history holds {', '.join(bad[:8])}{' …' if len(bad) > 8 else ''}"])
        return Decision("pr", repo, [f"{repo} exists and its history holds no dump, wp-config.php or .env"])

    def push_verified(self, remote: str, branch: str) -> None:
        rc, head, _ = self.git("rev-parse", "HEAD")
        if rc != 0:
            raise OSError("no commit to push: commit the conversion first (wordpress-init --commit)")
        rc, _, err = self.git("push", remote, f"HEAD:refs/heads/{branch}")
        if rc != 0:
            raise OSError(f"push to {remote} {branch} failed: {err.strip()}")
        rc, out, err = self.git("ls-remote", remote, f"refs/heads/{branch}")
        tip = out.split("\t", 1)[0].strip()
        if rc != 0 or tip != head.strip():
            raise OSError(
                f"the remote's {branch} is {tip or '?'} but this tree is at {head.strip()}: refusing to call it pushed"
            )

    def set_origin(self, repo: str) -> None:
        url = f"https://github.com/{repo}.git"
        rc, current, _ = self.git("remote", "get-url", "origin")
        if rc == 0 and current.strip() != url:
            self.git("remote", "rename", "origin", "legacy")
        if rc != 0 or current.strip() != url:
            self.git("remote", "add", "origin", url)

    def apply(self, d: Decision, slug: str, branch: str = "main", log=print) -> None:
        org = d.repo.split("/", 1)[0]
        if d.action == "replace":
            legacy = f"site-{slug}-legacy"
            for argv in (
                ("gh", "repo", "rename", legacy, "--repo", d.repo, "--yes"),
                ("gh", "repo", "archive", f"{org}/{legacy}", "--yes"),
            ):
                rc, _, err = self.run(*argv)
                if rc != 0:
                    raise OSError(f"{' '.join(argv)}: {err.strip()}")
            log(f"{d.repo} → {org}/{legacy} (archived)")
        if d.action in ("create", "replace"):
            rc, _, err = self.run(
                "gh", "repo", "create", d.repo, "--private", "--description", f"WordPress site: {slug} (wordpress-nix)"
            )
            if rc != 0:
                raise OSError(f"gh repo create {d.repo}: {err.strip()}")
            self.set_origin(d.repo)
            self.push_verified("origin", branch)
            log(f"pushed {branch} to {d.repo}")
            return
        self.set_origin(d.repo)
        self.push_verified("origin", CONVERT_BRANCH)
        rc, out, err = self.run(
            "gh",
            "pr",
            "create",
            "--repo",
            d.repo,
            "--head",
            CONVERT_BRANCH,
            "--base",
            branch,
            "--title",
            "feat: convert to Bedrock (wordpress-nix)",
            "--body",
            "The wordpress-nix v3 conversion (`wordpress-init --migrate`). Review the plan in the first commit.",
        )
        if rc != 0:
            raise OSError(f"gh pr create: {err.strip()}")
        log(out.strip())


def cmd(args, runner: Runner | None = None) -> int:
    a = Adopter(runner or subprocess_runner, args.dir)
    try:
        d = a.decide(args.org, args.slug)
        print(f"{d.repo}: {d.action} — {'; '.join(d.reasons)}")
        if args.check:
            return 0
        a.apply(d, args.slug, args.branch, log=lambda m: print(m, file=sys.stderr))
    except OSError as e:
        print(f"repo adopt: {e}", file=sys.stderr)
        return 3
    print(f"Next: wordpress-nix repo apply {d.repo}", file=sys.stderr)
    return 0
