"""Audit — and optionally sanitize — a mysqldump before it is restored.

A migration restores a database from a host we do not control and have not
audited. That dump is an input, not a trusted artifact: it can carry executable
objects that run on the NEW server the moment they are restored.

This is not hypothetical. The dump taken to migrate one client site
contained a trigger on wp_comments that created an administrator account
whenever a comment matched a phrase, and the account it had already created was
in the dump too. Restoring that dump faithfully would have migrated the
compromise along with the content.

So every migration runs this first (`wordpress-nix db audit`, plan §8.4).

  Fails on, and --strip removes:
    triggers, stored procedures and functions, events, views (and any DEFINER
    clause left behind), CREATE USER and GRANT statements; the credentials that
    would survive a move: `_application_passwords` and `session_tokens`
    usermeta rows, and MainWP's `mainwp_child_*` options. Those rows are removed
    tuple by tuple: every other tuple of the statement is written back byte for
    byte.

  Reports, each a finding with an ID that `--accept <id>` acknowledges:
    option-obfuscated:<name>     an autoloaded option with eval/base64/gzinflate-style
                                 obfuscation
    snippet:<table>:<id>         a row of a snippet table (WPCodeBox, Code Snippets):
                                 code stored in the database
    option-script:<name>         a widget or header/footer option carrying <script>
                                 or PHP
    admin-sql-written:<id>       an administrator capability written in SQL, not by
                                 WordPress
    admin-unconfirmed:<id>       an administrator the survey does not list (--survey)
    home-mismatch                siteurl and home name different hosts
    survey-mismatch:<kind>       the dump's triggers/routines/events/views differ
                                 from the survey's counts (--survey)

  Exit: 0 clean, or every finding removed (--strip) or accepted; 1 unaccepted
  findings; 2 a bad argument.

Account removal is deliberately NOT done by rewriting the dump: rows are far
easier to delete exactly once the data is in a database. The report prints the
SQL to do it, and `wordpress-nix site import` resets privileged passwords.

Usage:
  audit-mysql-dump dump.sql [--survey survey.json]
  audit-mysql-dump dump.sql --strip -o clean.sql [--accept ID]...
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
RE_INSERT = re.compile(r"^INSERT INTO `([A-Za-z0-9_$]+)`(\s*\([^)]*\))?\s+VALUES\b", re.I)

RE_DEFINER = re.compile(r"DEFINER\s*=\s*`[^`]*`@`[^`]*`\s*", re.I)
RE_TRIGGER = re.compile(r"\bTRIGGER\b", re.I)
RE_EVENT = re.compile(r"\bEVENT\s+`", re.I)
RE_ROUTINE = re.compile(r"\b(PROCEDURE|FUNCTION)\b", re.I)
RE_VIEW_STMT = re.compile(r"/\*!500\d\d\s+VIEW\b", re.I)
# The lines mysqldump writes before a view's VIEW line: CREATE ALGORITHM, DEFINER.
RE_VIEW_HEAD = re.compile(r"^/\*!500\d\d\s+(CREATE\s+ALGORITHM|SQL\s+SECURITY|DEFINER)", re.I)
RE_DELIMITER = re.compile(r"^DELIMITER\s+(\S+)", re.I)
RE_ACCOUNT = re.compile(r"^\s*(?:/\*!\d+\s*)?(CREATE\s+USER|GRANT\s|ALTER\s+USER|SET\s+PASSWORD)", re.I)

# A trigger or routine that touches the users tables is not a quirk of the old
# host's tooling; it is a privilege-escalation mechanism.
RE_TOUCHES_USERS = re.compile(r"\b(INSERT|UPDATE|REPLACE)\b[^;]{0,400}?`?[A-Za-z0-9_]*(users|usermeta)`?", re.I | re.S)

# Obfuscation in an autoloaded option, and code in a widget or header/footer option.
RE_OBFUSCATED = re.compile(
    r"eval\s*\(|assert\s*\(\s*\$|base64_decode\s*\(|gzinflate\s*\(|gzuncompress\s*\(|str_rot13\s*\("
    r"|create_function\s*\(|(?:\\\\?x[0-9a-fA-F]{2}){10,}|<\?php",
    re.I,
)
RE_SCRIPT = re.compile(r"<script\b|<\?php|<\?=", re.I)
RE_HEADER_FOOTER = re.compile(r"(?:^|_)(?:header|footer|head|body)(?:_|$)|^ihaf_|^hfcm_", re.I)

STRIP_USERMETA = ("_application_passwords", "session_tokens")
STRIP_OPTION_PREFIXES = ("mainwp_child_",)
SNIPPET_TABLES = ("wpcb_snippets", "snippets", "wpcodebox_snippets", "wpcode_snippets")


def tuple_spans(values: str) -> Iterator[tuple[int, int, list[str | None]]]:
    """Each (...) tuple of a mysqldump VALUES clause: its start and end offsets in
    `values` (the parentheses included) and its fields.

    Written as a scanner rather than a regex because a value may contain commas,
    parentheses, escaped quotes and backslashes, and a regex that gets that
    wrong fails silently -- which for an audit is worse than failing loudly.
    NULL becomes None; quoted values keep their escapes, which is what the
    callers compare against.
    """
    i, n = 0, len(values)
    while i < n:
        while i < n and values[i] != "(":
            if values[i] == ";":
                return
            i += 1
        if i >= n:
            return
        start = i
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
        yield start, i, [None if f.strip().upper() == "NULL" else f for f in fields]


def split_row_values(values: str) -> Iterator[list[str | None]]:
    for _, _, fields in tuple_spans(values):
        yield fields


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
class Finding:
    id: str
    message: str


@dataclass
class Findings:
    triggers: list[str] = field(default_factory=list)
    routines: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    views: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    definers: int = 0
    privileged: dict[str, Account] = field(default_factory=dict)
    # Every users row seen, keyed by id. Collected unconditionally and joined to
    # `privileged` at the end, because the capability rows and the user rows can
    # appear in either order (mysqldump orders tables alphabetically, which puts
    # usermeta first -- but nothing guarantees that).
    users: dict[str, tuple[str | None, str | None, str | None]] = field(default_factory=dict)
    users_table: str | None = None
    escalation: list[str] = field(default_factory=list)
    # Credentials removed (or removable) row by row.
    app_passwords: int = 0
    session_tokens: int = 0
    mainwp_options: list[str] = field(default_factory=list)
    options: dict[str, str] = field(default_factory=dict)
    report: list[Finding] = field(default_factory=list)

    @property
    def executable(self) -> int:
        return len(self.triggers) + len(self.routines) + len(self.events) + len(self.views)

    @property
    def removable(self) -> int:
        return (
            self.executable
            + len(self.accounts)
            + self.app_passwords
            + self.session_tokens
            + len(self.mainwp_options)
        )


def name_of(block: str, keywords: str) -> str:
    """The identifier following one of `keywords` in a CREATE ... statement."""
    m = re.search(r"\b(?:" + keywords + r")\s+`([^`]+)`", block, re.I)
    return m.group(1) if m else "<unnamed>"


def unescape(text: str | None) -> str:
    if text is None:
        return ""
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "r": "\r", "t": "\t", "0": "\0"}.get(m.group(1), m.group(1)), text)


def _suffix(table: str, prefix_tables: tuple[str, ...]) -> str | None:
    for t in prefix_tables:
        if table.lower().endswith(t):
            return t
    return None


def audit(src: Iterator[str], out, strip: bool) -> Findings:
    f = Findings()
    # Inside a DELIMITER block, mysqldump's trigger, routine and event bodies live.
    pending: list[str] = []
    in_block = False
    gathering: list[str] | None = None
    gather_table = ""
    gather_head = ""
    # A view's leading lines, held until its VIEW line says what they are.
    view_head: list[str] = []

    def flush_block() -> None:
        nonlocal pending
        block = "".join(pending)
        is_trigger = bool(RE_TRIGGER.search(block))
        is_event = not is_trigger and bool(RE_EVENT.search(block))
        is_routine = not is_trigger and not is_event and bool(RE_ROUTINE.search(block))
        if is_trigger:
            f.triggers.append(name_of(block, "TRIGGER"))
        elif is_event:
            f.events.append(name_of(block, "EVENT"))
        elif is_routine:
            f.routines.append(name_of(block, "PROCEDURE|FUNCTION"))
        executable = is_trigger or is_event or is_routine
        if executable and RE_TOUCHES_USERS.search(block):
            keyword = "TRIGGER" if is_trigger else "EVENT" if is_event else "PROCEDURE|FUNCTION"
            f.escalation.append(name_of(block, keyword))
        # Anything executable in a DELIMITER block goes with --strip; the rest is
        # written back verbatim.
        if out and not (strip and executable):
            out.write(block)
        pending = []

    def table_kind(table: str) -> str:
        low = table.lower()
        if low.endswith("usermeta"):
            return "usermeta"
        if low.endswith("users"):
            return "users"
        if low.endswith("options"):
            return "options"
        if _suffix(low, SNIPPET_TABLES):
            return "snippets"
        return ""

    def finish_statement(clause: str) -> str | None:
        """Read the gathered INSERT; return the statement to write (None: drop it)."""
        kind = table_kind(gather_table)
        kept: list[str] = []
        removed = 0
        for start, end, row in tuple_spans(clause):
            drop = False
            if kind == "usermeta":
                # (umeta_id, user_id, meta_key, meta_value)
                key = row[2] if len(row) >= 4 else None
                if key in STRIP_USERMETA:
                    drop = True
                    if key == "_application_passwords":
                        f.app_passwords += 1
                    else:
                        f.session_tokens += 1
                elif key and key.endswith("capabilities") and row[3] and "administrator" in row[3]:
                    uid = row[1] or "?"
                    acct = f.privileged.setdefault(uid, Account(user_id=uid))
                    acct.capabilities = row[3]
                    # WordPress serializes the capability as b:1. The string form
                    # is what something writing SQL directly produces.
                    acct.machine_written = bool(re.search(r'administrator\\?";s:\d+:', row[3]))
            elif kind == "users":
                # (ID, login, pass, nicename, email, url, registered, ...)
                if len(row) >= 7 and row[0] is not None:
                    f.users[row[0]] = (unescape(row[1]), unescape(row[4]), row[6])
            elif kind == "options":
                # (option_id, option_name, option_value, autoload)
                if len(row) >= 3 and row[1] is not None:
                    name, value = unescape(row[1]), unescape(row[2])
                    autoload = (row[3] or "") if len(row) >= 4 else "yes"
                    if name.startswith(STRIP_OPTION_PREFIXES):
                        drop = True
                        f.mainwp_options.append(name)
                    else:
                        if name in ("siteurl", "home"):
                            f.options[name] = value
                        if name.startswith("widget_") or RE_HEADER_FOOTER.search(name):
                            if RE_SCRIPT.search(value):
                                f.report.append(Finding(f"option-script:{name}", f"option {name} carries <script> or PHP"))
                        elif autoload.lower() in ("yes", "on", "auto", "auto-on") and RE_OBFUSCATED.search(value):
                            f.report.append(
                                Finding(f"option-obfuscated:{name}", f"autoloaded option {name} looks obfuscated")
                            )
            elif kind == "snippets":
                rid = row[0] if row else "?"
                code = " ".join(unescape(v) for v in row[1:] if v)
                if code.strip():
                    snippet = re.sub(r"\s+", " ", code)[:80]
                    table = _suffix(gather_table.lower(), SNIPPET_TABLES)
                    f.report.append(Finding(f"snippet:{table}:{rid}", f"{gather_table} row {rid}: {snippet}"))
            if drop:
                removed += 1
            else:
                kept.append(clause[start:end])
        if not strip or not removed:
            return None  # unchanged: the caller writes the lines as they were
        if not kept:
            return ""
        return f"{gather_head} " + ",".join(kept) + ";\n"

    for line in src:
        m = RE_DELIMITER.match(line)
        if m and gathering is None:
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

        if gathering is None and RE_ACCOUNT.match(line):
            f.accounts.append(line.strip()[:120])
            if strip:
                continue

        if gathering is None and RE_VIEW_HEAD.match(line) and not RE_VIEW_STMT.search(line):
            if RE_DEFINER.search(line):
                f.definers += 1
            view_head.append(line)
            continue
        if gathering is None and RE_VIEW_STMT.search(line):
            vm = re.search(r"VIEW\s+`([^`]+)`", line, re.I)
            f.views.append(vm.group(1) if vm else "<unnamed>")
            if strip:
                view_head = []
                continue
        if view_head:
            # Not a view after all: write the held lines back as they were.
            if out:
                out.write("".join(RE_DEFINER.sub("", h) if strip else h for h in view_head))
            view_head = []

        if gathering is None and RE_DEFINER.search(line):
            f.definers += 1
            if strip:
                line = RE_DEFINER.sub("", line)

        # Gather an INSERT into a table this audit reads, however many lines it
        # spans, then parse it. Other statements stream straight through, so only
        # one statement is ever held in memory.
        if gathering is None:
            im = RE_INSERT.match(line)
            if im and table_kind(im.group(1)):
                gather_table = im.group(1)
                if table_kind(gather_table) == "users":
                    f.users_table = gather_table
                gather_head = line[: im.end()]
                gathering = [line]
            else:
                if out:
                    out.write(line)
                continue
        elif gathering[-1] is not line:
            gathering.append(line)
        if line.rstrip().endswith(";"):
            text = "".join(gathering)
            im = RE_INSERT.match(text)
            clause = text[im.end() :] if im else text
            replacement = finish_statement(clause)
            if out:
                out.write(text if replacement is None else replacement)
            gathering = None

    if in_block:  # truncated dump
        flush_block()
    if gathering is not None and out:  # truncated INSERT
        out.write("".join(gathering))

    for uid, acct in f.privileged.items():
        if uid in f.users:
            acct.login, acct.email, acct.registered = f.users[uid]
        if acct.machine_written:
            f.report.append(
                Finding(f"admin-sql-written:{uid}", f"administrator {uid} ({acct.login or '?'}) was written in SQL")
            )
    home, siteurl = f.options.get("home", ""), f.options.get("siteurl", "")
    if home and siteurl and _host(home) != _host(siteurl):
        f.report.append(Finding("home-mismatch", f"siteurl {siteurl} and home {home} name different hosts"))
    return f


def _host(url: str) -> str:
    m = re.match(r"^(?:[a-z]+:)?//([^/:]+)", url.strip(), re.I)
    host = (m.group(1) if m else url).lower()
    return host[4:] if host.startswith("www.") else host


def against_survey(f: Findings, survey: dict) -> None:
    """Cross-check the dump against the legacy survey (wordpress-nix legacy survey)."""
    code = survey.get("dbCode") or {}
    for kind, found in (("triggers", f.triggers), ("routines", f.routines), ("events", f.events), ("views", f.views)):
        if kind in code and len(code.get(kind) or []) != len(found):
            f.report.append(
                Finding(
                    f"survey-mismatch:{kind}",
                    f"the survey counted {len(code.get(kind) or [])} {kind}, the dump has {len(found)}",
                )
            )
    if "administrators" in survey:
        confirmed = {str(a.get("id")) for a in survey.get("administrators") or []}
        for uid, acct in sorted(f.privileged.items()):
            if uid not in confirmed:
                f.report.append(
                    Finding(
                        f"admin-unconfirmed:{uid}",
                        f"administrator {uid} ({acct.login or '?'}) is not in the survey's list",
                    )
                )


def report(f: Findings, path: str, stream, accepted: set[str], stripped: bool) -> None:
    w = stream.write
    w(f"=== dump audit: {path} ===\n\n")

    if f.executable == 0 and f.definers == 0 and not f.accounts:
        w("Executable objects: none. The dump carries data and schema only.\n")
    else:
        w("EXECUTABLE OBJECTS FOUND. These run on the new server once restored.\n")
        for label, names in (
            ("trigger", f.triggers),
            ("routine", f.routines),
            ("event", f.events),
            ("view", f.views),
        ):
            for n in names:
                w(f"  {label:8} {n}\n")
        for stmt in f.accounts:
            w(f"  {'account':8} {stmt}\n")
        if f.definers:
            w(f"  {'definer':8} {f.definers} DEFINER clause(s)\n")
        w("\n" + ("--strip removed them.\n" if stripped else "Re-run with --strip to write a dump without them.\n"))

    if f.escalation:
        w("\n!! PRIVILEGE ESCALATION: these write to the users tables, which is\n")
        w("!! what a persistence backdoor does. Treat the source host as compromised.\n")
        for n in f.escalation:
            w(f"!!   {n}\n")

    creds = []
    if f.app_passwords:
        creds.append(f"{f.app_passwords} application-password row(s)")
    if f.session_tokens:
        creds.append(f"{f.session_tokens} session-token row(s)")
    if f.mainwp_options:
        creds.append(f"{len(f.mainwp_options)} MainWP option(s) ({', '.join(f.mainwp_options[:3])})")
    if creds:
        verb = "removed" if stripped else "found (--strip removes them)"
        w(f"\nCredentials that would survive the move, {verb}: {'; '.join(creds)}\n")

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

    if f.report:
        w("\nFindings (acknowledge each with --accept <id>):\n")
        for item in f.report:
            mark = "accepted" if item.id in accepted else "OPEN    "
            w(f"  {mark} {item.id}: {item.message}\n")

    w("\nReview every account above before cutover. To remove one after restoring:\n")
    t = f.users_table or "wp_users"
    meta = t.replace("users", "usermeta")
    w(f"  DELETE FROM `{meta}` WHERE user_id IN (<ids>);\n")
    w(f"  DELETE FROM `{t}` WHERE ID IN (<ids>);\n")
    w("\nAnd confirm nothing executable survived the restore:\n")
    w("  SELECT COUNT(*) FROM information_schema.triggers WHERE trigger_schema = DATABASE();\n")
    w("  SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema = DATABASE();\n")
    w("  SELECT COUNT(*) FROM information_schema.events   WHERE event_schema   = DATABASE();\n")
    w("  SELECT COUNT(*) FROM information_schema.views    WHERE table_schema   = DATABASE();\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Audit, and optionally sanitize, a mysqldump before restoring it.",
    )
    ap.add_argument("dump", help="the .sql file (use - for stdin)")
    ap.add_argument(
        "--strip",
        action="store_true",
        help="write a sanitized dump (executable objects, accounts, application passwords,"
        " session tokens and MainWP options removed)",
    )
    ap.add_argument("-o", "--out", "--output", dest="output", help="where --strip writes; default stdout")
    ap.add_argument("--survey", help="the legacy survey (survey.json): cross-check counts and administrators")
    ap.add_argument("--accept", action="append", default=[], metavar="ID", help="acknowledge a finding")
    ap.add_argument("--json", action="store_true", help="machine-readable findings on stdout")
    ap.add_argument(
        "--allow-executable",
        action="store_true",
        help="exit 0 even when executable objects were found (do not use in a migration)",
    )
    args = ap.parse_args(argv)

    survey = None
    if args.survey:
        try:
            with open(args.survey, encoding="utf-8") as sf:
                survey = json.load(sf)
        except (OSError, ValueError) as e:
            print(f"audit-mysql-dump: cannot read the survey {args.survey}: {e}", file=sys.stderr)
            return 2
    try:
        src = sys.stdin if args.dump == "-" else open(args.dump, encoding="utf-8", errors="surrogateescape")
    except OSError as e:
        print(f"audit-mysql-dump: {e}", file=sys.stderr)
        return 2
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
    if survey is not None:
        against_survey(f, survey)

    accepted = set(args.accept)
    unknown = sorted(accepted - {item.id for item in f.report})
    open_findings = [item for item in f.report if item.id not in accepted]

    # The report goes to stderr whenever stdout is carrying the dump or JSON, so
    # `--strip -o -` and `| mysql` both stay usable.
    where = sys.stdout if (not args.json and not (args.strip and not args.output)) else sys.stderr
    if args.json:
        json.dump(
            {
                "triggers": f.triggers,
                "routines": f.routines,
                "events": f.events,
                "views": f.views,
                "accounts": f.accounts,
                "definers": f.definers,
                "escalation": f.escalation,
                "applicationPasswords": f.app_passwords,
                "sessionTokens": f.session_tokens,
                "mainwpOptions": f.mainwp_options,
                "findings": [
                    {"id": item.id, "message": item.message, "accepted": item.id in accepted}
                    for item in f.report
                ],
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
    report(f, args.dump, where, accepted, args.strip)
    for item in unknown:
        print(f"audit-mysql-dump: --accept {item}: no such finding", file=sys.stderr)

    failing = bool(open_findings)
    if f.removable and not args.strip and not args.allow_executable:
        failing = True
    return 1 if failing else 0


if __name__ == "__main__":
    sys.exit(main())
