"""Audit — and optionally sanitize — a mysqldump before it is restored.

A migration restores a database from a host we do not control and have not
audited. That dump is an input, not a trusted artifact: it can carry executable
objects that run on the NEW server the moment they are restored.

This is not hypothetical. The dump taken to migrate one client site
contained a trigger on wp_comments that created an administrator account
whenever a comment matched a phrase, and the account it had already created was
in the dump too. Restoring that dump faithfully would have migrated the
compromise along with the content.

So every migration runs this first. Two things it does:

  audit (default)  Report executable objects and every account that holds an
                   administrator capability, then exit non-zero if anything
                   executable was found. A migration script gates on that.

  --strip          Write a dump with the executable objects removed: triggers,
                   stored procedures and functions, views, and any DEFINER
                   clause left behind. Content is untouched.

Account removal is deliberately NOT done by rewriting the dump. Editing INSERT
statements risks corrupting content, and rows are far easier to delete exactly
once the data is in a database. The report prints the SQL to do it.

Usage:
  audit-mysql-dump dump.sql
  audit-mysql-dump dump.sql --strip -o clean.sql
  audit-mysql-dump dump.sql --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Iterator

# The prefix is configurable in WordPress, so match any of them rather than
# assuming wp_.
# mysqldump does not promise the row tuples are on the same line as VALUES: with
# long rows it emits `INSERT INTO `t` VALUES` and then the tuples below. So these
# only find the START of a statement, which audit() then gathers to its `;`.
RE_USERMETA_INSERT = re.compile(r"^INSERT INTO `([A-Za-z0-9_]*usermeta)`\s+VALUES\b", re.I)
RE_USERS_INSERT = re.compile(r"^INSERT INTO `([A-Za-z0-9_]*users)`\s+VALUES\b", re.I)

RE_DEFINER = re.compile(r"DEFINER\s*=\s*`[^`]*`@`[^`]*`\s*", re.I)
RE_TRIGGER = re.compile(r"\bTRIGGER\b", re.I)
RE_ROUTINE = re.compile(r"\b(PROCEDURE|FUNCTION)\b", re.I)
RE_VIEW_STMT = re.compile(r"/\*!500\d\d\s+VIEW\b", re.I)
RE_DELIMITER = re.compile(r"^DELIMITER\s+(\S+)", re.I)

# A trigger or routine that touches the users tables is not a quirk of the old
# host's tooling; it is a privilege-escalation mechanism.
RE_TOUCHES_USERS = re.compile(r"\b(INSERT|UPDATE|REPLACE)\b[^;]{0,400}?`?[A-Za-z0-9_]*(users|usermeta)`?", re.I | re.S)


def split_row_values(values: str) -> Iterator[list[str | None]]:
    """Yield each (...) tuple of a mysqldump VALUES clause as a list of fields.

    Written as a scanner rather than a regex because a value may contain commas,
    parentheses, escaped quotes and backslashes, and a regex that gets that
    wrong fails silently — which for an audit is worse than failing loudly.
    NULL becomes None; quoted values keep their escapes, which is what the
    callers compare against.
    """
    i, n = 0, len(values)
    while i < n:
        while i < n and values[i] != "(":
            i += 1
        if i >= n:
            return
        i += 1  # past '('
        fields: list[str | None] = []
        buf: list[str] = []
        quoted = False
        while i < n:
            c = values[i]
            if quoted:
                if c == "\\":
                    buf.append(values[i : i + 2])
                    i += 2
                    continue
                if c == "'":
                    quoted = False
                    i += 1
                    continue
                buf.append(c)
                i += 1
                continue
            if c == "'":
                quoted = True
                i += 1
                continue
            if c == ",":
                fields.append("".join(buf))
                buf = []
                i += 1
                continue
            if c == ")":
                fields.append("".join(buf))
                i += 1
                break
            buf.append(c)
            i += 1
        yield [None if f.strip().upper() == "NULL" else f for f in fields]


@dataclass
class Account:
    user_id: str
    capabilities: str = ""
    login: str | None = None
    email: str | None = None
    registered: str | None = None
    # The trigger in the AP dump wrote capabilities as a:1:{s:13:"administrator";s:1:"1";}
    # -- the string form. WordPress itself writes b:1. A string "1" is therefore
    # a fingerprint of something that wrote the row directly, in SQL.
    machine_written: bool = False


@dataclass
class Findings:
    triggers: list[str] = field(default_factory=list)
    routines: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    definers: int = 0
    privileged: dict[str, Account] = field(default_factory=dict)
    # Every users row seen, keyed by id. Collected unconditionally and joined to
    # `privileged` at the end, because the capability rows and the user rows can
    # appear in either order (mysqldump orders tables alphabetically, which puts
    # usermeta first -- but nothing guarantees that).
    users: dict[str, tuple[str | None, str | None, str | None]] = field(default_factory=dict)
    users_table: str | None = None
    escalation: list[str] = field(default_factory=list)

    @property
    def executable(self) -> int:
        return len(self.triggers) + len(self.routines) + len(self.views)


def name_of(block: str, keywords: str) -> str:
    """The identifier following one of `keywords` in a CREATE ... statement."""
    m = re.search(r"\b(?:" + keywords + r")\s+`([^`]+)`", block, re.I)
    return m.group(1) if m else "<unnamed>"


def audit(src: Iterator[str], out, strip: bool) -> Findings:
    f = Findings()
    # Inside a DELIMITER block, mysqldump's trigger and routine bodies live.
    pending: list[str] = []
    in_block = False
    gathering: list[str] | None = None
    gather_kind: str | None = None

    def flush_block() -> None:
        nonlocal pending
        block = "".join(pending)
        is_trigger = bool(RE_TRIGGER.search(block))
        is_routine = bool(RE_ROUTINE.search(block))
        keywords = "TRIGGER" if is_trigger else "PROCEDURE|FUNCTION"
        if is_trigger:
            f.triggers.append(name_of(block, keywords))
        elif is_routine:
            f.routines.append(name_of(block, keywords))
        if (is_trigger or is_routine) and RE_TOUCHES_USERS.search(block):
            f.escalation.append(name_of(block, keywords))
        # Anything in a DELIMITER block is executable, so --strip drops all of
        # it; otherwise it is written back verbatim.
        if not strip or not (is_trigger or is_routine):
            if out and not (strip and (is_trigger or is_routine)):
                out.write(block)
        pending = []

    for line in src:
        m = RE_DELIMITER.match(line)
        if m:
            if m.group(1) != ";":
                in_block = True
                pending = [line]
            else:
                pending.append(line)
                in_block = False
                flush_block()
            continue
        if in_block:
            pending.append(line)
            continue

        if RE_VIEW_STMT.search(line):
            vm = re.search(r"VIEW\s+`([^`]+)`", line, re.I)
            f.views.append(vm.group(1) if vm else "<unnamed>")
            if strip:
                continue

        if RE_DEFINER.search(line):
            f.definers += 1
            if strip:
                line = RE_DEFINER.sub("", line)

        # Gather an INSERT into the users or usermeta tables, however many lines
        # it spans, then parse it. Other statements stream straight through, so
        # only these two tables are ever held in memory.
        if gathering is None:
            um = RE_USERMETA_INSERT.match(line)
            usm = RE_USERS_INSERT.match(line)
            if um:
                gathering, gather_kind = [line[um.end() :]], "usermeta"
            elif usm:
                f.users_table = usm.group(1)
                gathering, gather_kind = [line[usm.end() :]], "users"
        else:
            gathering.append(line)

        if gathering is not None and line.rstrip().endswith(";"):
            clause = "".join(gathering)
            for row in split_row_values(clause):
                if gather_kind == "usermeta":
                    # (umeta_id, user_id, meta_key, meta_value)
                    if len(row) >= 4 and row[2] == "wp_capabilities" and row[3]:
                        if "administrator" in row[3]:
                            uid = row[1] or "?"
                            acct = f.privileged.setdefault(uid, Account(user_id=uid))
                            acct.capabilities = row[3]
                            # WordPress serializes the capability as b:1. The
                            # string form is what something writing SQL directly
                            # produces -- the AP backdoor trigger, for one.
                            acct.machine_written = bool(
                                re.search(r'administrator\\?";s:\d+:', row[3])
                            )
                elif gather_kind == "users":
                    # (ID, login, pass, nicename, email, url, registered, ...)
                    if len(row) >= 7 and row[0] is not None:
                        f.users[row[0]] = (row[1], row[4], row[6])
            gathering, gather_kind = None, None

        if out:
            out.write(line)

    if in_block:  # truncated dump
        flush_block()

    for uid, acct in f.privileged.items():
        if uid in f.users:
            acct.login, acct.email, acct.registered = f.users[uid]
    return f


def report(f: Findings, path: str, stream) -> None:
    w = stream.write
    w(f"=== dump audit: {path} ===\n\n")

    if f.executable == 0 and f.definers == 0:
        w("Executable objects: none. The dump carries data and schema only.\n")
    else:
        w("EXECUTABLE OBJECTS FOUND. These run on the new server once restored.\n")
        for label, names in (
            ("trigger", f.triggers),
            ("routine", f.routines),
            ("view", f.views),
        ):
            for n in names:
                w(f"  {label:8} {n}\n")
        if f.definers:
            w(f"  {'definer':8} {f.definers} DEFINER clause(s)\n")
        w("\nRe-run with --strip to write a dump without them.\n")

    if f.escalation:
        w("\n!! PRIVILEGE ESCALATION: these write to the users tables, which is\n")
        w("!! what a persistence backdoor does. Treat the source host as compromised.\n")
        for n in f.escalation:
            w(f"!!   {n}\n")

    w(f"\nAccounts holding `administrator` ({len(f.privileged)}):\n")
    if not f.privileged:
        w("  none found — check the table prefix if that is unexpected.\n")
    for uid in sorted(f.privileged, key=lambda x: int(x) if x.isdigit() else 0):
        a = f.privileged[uid]
        flag = "  <-- written directly in SQL, not by WordPress" if a.machine_written else ""
        w(f"  id={uid:<6} login={a.login or '?':<28} email={a.email or '?':<34}"
          f" registered={a.registered or '?'}{flag}\n")

    machine = [a for a in f.privileged.values() if a.machine_written]
    if machine:
        w("\n!! An administrator capability serialized as s:1:\"1\" rather than b:1 was\n")
        w("!! written by something issuing SQL directly, not by WordPress.\n")

    w("\nReview every account above before cutover. To remove one after restoring:\n")
    t = f.users_table or "wp_users"
    meta = t.replace("users", "usermeta")
    w(f"  DELETE FROM `{meta}` WHERE user_id IN (<ids>);\n")
    w(f"  DELETE FROM `{t}` WHERE ID IN (<ids>);\n")
    w("\nAnd confirm nothing executable survived the restore:\n")
    w("  SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema = DATABASE();\n")
    w("  SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema = DATABASE();\n")
    w("  SELECT COUNT(*) FROM information_schema.views   WHERE table_schema   = DATABASE();\n")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit, and optionally sanitize, a mysqldump before restoring it.",
    )
    ap.add_argument("dump", help="the .sql file (use - for stdin)")
    ap.add_argument(
        "--strip",
        action="store_true",
        help="write a sanitized dump (triggers, routines, views and DEFINER clauses removed)",
    )
    ap.add_argument("-o", "--output", help="where --strip writes; default stdout")
    ap.add_argument("--json", action="store_true", help="machine-readable findings on stdout")
    ap.add_argument(
        "--allow-executable",
        action="store_true",
        help="exit 0 even when executable objects were found (do not use in a migration)",
    )
    args = ap.parse_args()

    src = sys.stdin if args.dump == "-" else open(args.dump, encoding="utf-8", errors="surrogateescape")
    out = None
    if args.strip:
        out = (
            open(args.output, "w", encoding="utf-8", errors="surrogateescape")
            if args.output
            else sys.stdout
        )

    try:
        f = audit(src, out, args.strip)
    finally:
        if src is not sys.stdin:
            src.close()
        if out and out is not sys.stdout:
            out.close()

    # The report goes to stderr whenever stdout is carrying the dump or JSON, so
    # `--strip -o -` and `| mysql` both stay usable.
    where = sys.stdout if (not args.json and not (args.strip and not args.output)) else sys.stderr
    if args.json:
        json.dump(
            {
                "triggers": f.triggers,
                "routines": f.routines,
                "views": f.views,
                "definers": f.definers,
                "escalation": f.escalation,
                "administrators": {
                    uid: {
                        "login": a.login,
                        "email": a.email,
                        "registered": a.registered,
                        "written_directly_in_sql": a.machine_written,
                    }
                    for uid, a in f.privileged.items()
                },
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
    report(f, args.dump, where)

    if f.executable and not args.strip and not args.allow_executable:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
