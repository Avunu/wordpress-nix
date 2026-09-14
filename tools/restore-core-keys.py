#!/usr/bin/env python3
"""restore-core-keys: give WordPress's core tables their standard keys back in a MySQL dump.

Plugins such as index-wp-mysql-for-speed rewrite the core tables' keys -- wp_options
gets PRIMARY KEY (option_name) with a UNIQUE KEY on option_id, the meta tables get
composite primary keys. The MySQL-on-SQLite driver maps the AUTO_INCREMENT column
to SQLite's rowid primary key and has no place for a second primary key, so the
uniqueness of option_name is lost on the way, and with it the safety of
INSERT ... ON DUPLICATE KEY UPDATE. Run this on the dump first: for every core
table whose keys differ from wp_get_db_schema(), the PRIMARY KEY / UNIQUE KEY / KEY
lines are replaced by the standard ones. Column definitions are kept as dumped.

Usage: restore-core-keys <dump.sql> <standard-schema.sql> <out.sql>

The Nix package supplies standard-schema.sql from the platform's pinned core:
    nix run github:Avunu/wordpress#restore-core-keys -- dump.sql fixed.sql
"""
import re
import sys

if len(sys.argv) != 4:
    print(__doc__.split("\n\n")[3], file=sys.stderr)
    sys.exit(1)
dump_path, schema_path, out_path = sys.argv[1:4]
schema = open(schema_path, encoding="utf-8").read()
standard = {}
for m in re.finditer(r"CREATE TABLE (\w+) \((.*?)\n\) ", schema, re.S):
    table, body = m.group(1), m.group(2)
    keys = [line.strip().rstrip(",") for line in body.split("\n") if re.match(r"\s*(PRIMARY KEY|UNIQUE KEY|KEY)\b", line)]
    standard[table] = keys

def normalize(key):
    key = re.sub(r"\s+", " ", key.replace("`", "")).strip()
    return re.sub(r"PRIMARY KEY \(", "PRIMARY KEY (", key)

dump = open(dump_path, encoding="utf-8", errors="surrogateescape").read()
changed = []
skipped = []
def rewrite(m):
    table, body, tail = m.group(1), m.group(2), m.group(3)
    if table not in standard:
        return m.group(0)
    lines = body.split("\n")
    columns = [l for l in lines if not re.match(r"\s*(PRIMARY KEY|UNIQUE KEY|KEY|FULLTEXT KEY)\b", l)]
    old_keys = [l.strip().rstrip(",") for l in lines if re.match(r"\s*(PRIMARY KEY|UNIQUE KEY|KEY|FULLTEXT KEY)\b", l)]
    if sorted(map(normalize, old_keys)) == sorted(map(normalize, standard[table])):
        return m.group(0)
    columns = [c.rstrip(",") for c in columns if c.strip()]
    # The standard keys must only name columns the dumped table has; a table
    # from another core version (or a trimmed one) is left as it is.
    have = {re.match(r"\s*`?(\w+)`?", c).group(1) for c in columns}
    need = {col for k in standard[table] for col in re.findall(r"\(([^)]*)\)", k) for col in re.findall(r"\b(\w+)\b(?!\()", col)}
    need = {col for col in need if not col.isdigit()}
    missing = sorted(need - have)
    if missing:
        skipped.append((table, missing))
        return m.group(0)
    changed.append((table, old_keys, standard[table]))
    new_body = ",\n".join(columns + ["  " + k for k in standard[table]])
    return f"CREATE TABLE `{table}` (\n{new_body}\n){tail}"

out = re.sub(r"CREATE TABLE `(\w+)` \(\n(.*?)\n\)( ENGINE=[^\n]*;)", rewrite, dump, flags=re.S)
open(out_path, "w", encoding="utf-8", errors="surrogateescape").write(out)
for table, old, new in changed:
    print(f"{table}:")
    for k in old: print(f"  - {k}")
    for k in new: print(f"  + {k}")
for table, missing in skipped:
    print(f"warning: {table}: keys differ from the standard but the table lacks {', '.join(missing)}; left as dumped", file=sys.stderr)
print(f"{len(changed)} tables rewritten")
