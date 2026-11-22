"""Reading rows out of a mysqldump without a database: the options the scaffolder
needs (``active_plugins``, ``template``, ``stylesheet``, ``siteurl``, ``home``,
``WPLANG``) and the attachment index ``media offload`` builds (``_wp_attached_file``,
``_wp_attachment_metadata``).

Only INSERTs into the named tables are gathered, statement by statement, so memory
is one statement at a time. The row scanner is audit-mysql-dump's.
"""

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from wordpress_nix_tools.common.phpserial import mysql_unescape

INSERT = re.compile(r"^INSERT INTO `([A-Za-z0-9_$]+)`(?:\s*\([^)]*\))?\s+VALUES\b", re.I)


def split_rows(values: str) -> Iterator[list[str | None]]:
    """Each (...) tuple of a VALUES clause as a list of fields (unescaped text;
    NULL is None)."""
    i, n = 0, len(values)
    while i < n:
        while i < n and values[i] != "(":
            if values[i] == ";":
                return
            i += 1
        if i >= n:
            return
        i += 1
        fields: list[str | None] = []
        buf: list[str] = []
        quoted = was_quoted = False
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
                quoted = was_quoted = True
                i += 1
                continue
            if c in ",)":
                raw = "".join(buf)
                if not was_quoted and raw.strip().upper() == "NULL":
                    fields.append(None)
                else:
                    fields.append(mysql_unescape(raw) if was_quoted else raw.strip())
                buf, was_quoted = [], False
                i += 1
                if c == ")":
                    break
                continue
            buf.append(c)
            i += 1
        yield fields


def rows(lines: Iterable[str], tables: set[str]) -> Iterator[tuple[str, list[str | None]]]:
    """(table, row) for every row INSERTed into one of ``tables``."""
    gathering: list[str] | None = None
    table = ""
    for line in lines:
        if gathering is None:
            m = INSERT.match(line)
            if not m or m.group(1) not in tables:
                continue
            table = m.group(1)
            gathering = [line[m.end() :]]
        else:
            gathering.append(line)
        if line.rstrip().endswith(";"):
            for row in split_rows("".join(gathering)):
                yield table, row
            gathering = None


def open_dump(path: str | Path):
    return open(path, encoding="utf-8", errors="surrogateescape")


def detect_prefix(path: str | Path) -> str:
    """The table prefix: the ``<prefix>options`` table the dump creates."""
    pat = re.compile(r"^CREATE TABLE `([A-Za-z0-9_$]*?)options`", re.I)
    with open_dump(path) as f:
        for line in f:
            m = pat.match(line)
            if m:
                return m.group(1)
    return "wp_"


OPTION_NAMES = ("active_plugins", "template", "stylesheet", "siteurl", "home", "WPLANG", "db_version")


def options(path: str | Path, prefix: str, names: Iterable[str] = OPTION_NAMES) -> dict[str, str]:
    """The named options' values: (option_id, option_name, option_value, autoload)."""
    wanted = set(names)
    out: dict[str, str] = {}
    with open_dump(path) as f:
        for _, row in rows(f, {f"{prefix}options"}):
            if len(row) >= 3 and row[1] in wanted and row[2] is not None:
                out[row[1]] = row[2]
    return out


def attachments(path: str | Path, prefix: str) -> dict[str, dict[str, str]]:
    """post_id → {"file": _wp_attached_file, "meta": _wp_attachment_metadata}."""
    out: dict[str, dict[str, str]] = {}
    with open_dump(path) as f:
        for _, row in rows(f, {f"{prefix}postmeta"}):
            # (meta_id, post_id, meta_key, meta_value)
            if len(row) < 4 or row[3] is None or row[1] is None:
                continue
            if row[2] == "_wp_attached_file":
                out.setdefault(row[1], {})["file"] = row[3]
            elif row[2] == "_wp_attachment_metadata":
                out.setdefault(row[1], {})["meta"] = row[3]
    return out
