#!/usr/bin/env python3
"""sqlite-to-turso: load a SQLite database into a Turso database.

The companion of mysql-to-sqlite: that converter replays a MySQL dump through
the MySQL-on-SQLite driver into a local SQLite file, and this tool copies the
file -- schema, rows, indexes, triggers, views and AUTOINCREMENT counters --
into a Turso primary over its SQL-over-HTTP pipeline. Nothing is generated as
SQL text for the rows: values travel as typed pipeline arguments, so quoting,
binary content and integers beyond JSON's range are never an issue.

Usage:
  sqlite-to-turso <database.sqlite> <turso url> [--replace] [--rows-per-statement N]
                  [--statements-per-request N] [--pipeline-path /v2/pipeline] [--quiet]

The auth token is read from TURSO_AUTH_TOKEN (or TURSO_AUTH_TOKEN_FILE), never
from the command line. A target that already has tables is refused unless
--replace is given, which drops them first.

Exit codes: 0 success · 1 usage/IO error · 2 load or verification error.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DEFAULT_ROWS_PER_STATEMENT = 200
DEFAULT_STATEMENTS_PER_REQUEST = 8
# The pipeline is JSON over HTTP; keep a request comfortably below the
# server's body limits.
MAX_REQUEST_BYTES = 4 * 1024 * 1024
# Positional parameters per statement stay well under SQLite's limit.
MAX_PARAMS_PER_STATEMENT = 2000


class LoadError(Exception):
    pass


class Pipeline:
    """A minimal client for Turso's /v2/pipeline endpoint."""

    def __init__(self, url: str, token: str | None, path: str) -> None:
        self.endpoint = normalize_url(url) + path
        self.token = token

    def request(self, requests: list[dict]) -> list[dict]:
        body = json.dumps({"requests": requests}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as response:
                    payload = json.load(response)
                break
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")
                raise LoadError(f"HTTP {e.code} from the pipeline: {text[:500]}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == 3:
                    raise LoadError(f"the pipeline request failed: {e}") from None
                time.sleep(2 ** attempt)
        results = payload.get("results")
        if not isinstance(results, list):
            raise LoadError(f"unexpected pipeline response: {json.dumps(payload)[:500]}")
        for result in results:
            if result.get("type") == "error":
                raise LoadError(f"pipeline error: {result.get('error', {}).get('message', result)}")
        return results

    def execute(self, sql: str, args: list | None = None) -> dict:
        stmt = {"sql": sql}
        if args:
            stmt["args"] = args
        results = self.request([{"type": "execute", "stmt": stmt}])
        return results[0]["response"]["result"]

    def batch(self, statements: list[tuple[str, list]]) -> None:
        """Run statements atomically: BEGIN, each chained on the previous, COMMIT."""
        steps = [{"stmt": {"sql": "BEGIN"}}]
        for sql, args in statements:
            stmt = {"sql": sql}
            if args:
                stmt["args"] = args
            steps.append({"stmt": stmt, "condition": {"type": "ok", "step": len(steps) - 1}})
        commit_index = len(steps)
        steps.append({"stmt": {"sql": "COMMIT"}, "condition": {"type": "ok", "step": len(steps) - 1}})
        steps.append(
            {
                "stmt": {"sql": "ROLLBACK"},
                "condition": {"type": "not", "cond": {"type": "ok", "step": commit_index}},
            }
        )
        results = self.request([{"type": "batch", "batch": {"steps": steps}}])
        result = results[0]["response"]["result"]
        for index, error in enumerate(result.get("step_errors") or []):
            if error is not None:
                sql = steps[index]["stmt"]["sql"]
                raise LoadError(f"batch step {index} failed: {error.get('message', error)} -- {sql[:200]}")


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    for scheme in ("libsql://", "turso://", "wss://"):
        if url.startswith(scheme):
            return "https://" + url[len(scheme):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    if not url.startswith(("http://", "https://")):
        return "https://" + url
    return url


def encode_value(value) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode()}
    return {"type": "text", "value": str(value)}


def decode_row(row: dict | list) -> list:
    values = []
    for cell in row:
        if not isinstance(cell, dict):
            values.append(cell)
            continue
        kind = cell.get("type")
        if kind == "null":
            values.append(None)
        elif kind == "integer":
            values.append(int(cell["value"]))
        elif kind == "float":
            values.append(float(cell["value"]))
        elif kind == "blob":
            values.append(base64.b64decode(cell["base64"]))
        else:
            values.append(cell.get("value"))
    return values


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def log(quiet: bool, message: str) -> None:
    if not quiet:
        print(message, flush=True)


def remote_objects(pipeline: Pipeline) -> list[tuple[str, str]]:
    """User objects on the target: SQLite's and Turso's own bookkeeping tables are not ours to drop."""
    result = pipeline.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND name NOT LIKE '\\_\\_turso\\_internal\\_%' ESCAPE '\\' "
        "ORDER BY type, name"
    )
    return [(row[0], row[1]) for row in (decode_row(r) for r in result.get("rows", []))]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="sqlite-to-turso", description=__doc__.split("\n\n")[1])
    parser.add_argument("database", help="the SQLite file to load")
    parser.add_argument("url", help="the Turso database URL (libsql://, turso:// or https://)")
    parser.add_argument("--replace", action="store_true", help="drop the target's existing tables first")
    parser.add_argument("--rows-per-statement", type=int, default=DEFAULT_ROWS_PER_STATEMENT)
    parser.add_argument("--statements-per-request", type=int, default=DEFAULT_STATEMENTS_PER_REQUEST)
    parser.add_argument("--pipeline-path", default="/v2/pipeline")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.database):
        print(f"sqlite-to-turso: {args.database}: no such file", file=sys.stderr)
        return 1
    token = os.environ.get("TURSO_AUTH_TOKEN")
    token_file = os.environ.get("TURSO_AUTH_TOKEN_FILE")
    if not token and token_file:
        with open(token_file, encoding="utf-8") as f:
            token = f.read().strip()

    pipeline = Pipeline(args.url, token or None, args.pipeline_path)
    local = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    local.text_factory = lambda b: b.decode("utf-8", "surrogateescape")

    try:
        return load(pipeline, local, args)
    except LoadError as e:
        print(f"sqlite-to-turso: {e}", file=sys.stderr)
        return 2
    finally:
        local.close()


def load(pipeline: Pipeline, local: sqlite3.Connection, args) -> int:
    quiet = args.quiet
    existing = remote_objects(pipeline)
    if existing:
        if not args.replace:
            names = ", ".join(name for _, name in existing[:8])
            raise LoadError(
                f"the target already has {len(existing)} objects ({names}{', ...' if len(existing) > 8 else ''}); "
                "pass --replace to drop them first"
            )
        log(quiet, f"dropping {len(existing)} existing objects")
        # Views and triggers first, then tables (which take their indexes along).
        order = {"view": 0, "trigger": 1, "index": 2, "table": 3}
        drops = [
            (f"DROP {kind.upper()} IF EXISTS {quote_identifier(name)}", [])
            for kind, name in sorted(existing, key=lambda o: order.get(o[0], 9))
            if kind in order
        ]
        for i in range(0, len(drops), 50):
            pipeline.batch(drops[i : i + 50])

    schema = local.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
        "ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'trigger' THEN 2 WHEN 'view' THEN 3 ELSE 4 END, rowid"
    ).fetchall()
    tables = [name for kind, name, _, _ in schema if kind == "table"]
    if not tables:
        raise LoadError("the SQLite file has no tables")

    log(quiet, f"creating {len(tables)} tables")
    creates = [(sql, []) for kind, _, _, sql in schema if kind == "table"]
    for i in range(0, len(creates), 50):
        pipeline.batch(creates[i : i + 50])

    total_rows = 0
    started = time.monotonic()
    for table in tables:
        total_rows += load_table(pipeline, local, table, args)

    later = [(sql, []) for kind, _, _, sql in schema if kind in ("index", "trigger", "view")]
    if later:
        log(quiet, f"creating {len(later)} indexes, triggers and views")
        for i in range(0, len(later), 50):
            pipeline.batch(later[i : i + 50])

    # AUTOINCREMENT counters: inserting explicit ids already advanced them, but
    # a counter can sit above the highest id when rows were deleted.
    sequences = local.execute("SELECT name, seq FROM sqlite_sequence").fetchall() if has_sequence(local) else []
    if sequences:
        statements = []
        for name, seq in sequences:
            statements.append(("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", [encode_value(seq), encode_value(name)]))
            statements.append(
                (
                    "INSERT INTO sqlite_sequence (name, seq) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = ?)",
                    [encode_value(name), encode_value(seq), encode_value(name)],
                )
            )
        try:
            for i in range(0, len(statements), 50):
                pipeline.batch(statements[i : i + 50])
            log(quiet, f"set {len(sequences)} AUTOINCREMENT counters")
        except LoadError as e:
            print(f"sqlite-to-turso: warning: could not set AUTOINCREMENT counters: {e}", file=sys.stderr)

    elapsed = time.monotonic() - started
    log(quiet, f"loaded {total_rows} rows into {len(tables)} tables in {elapsed:.1f} s; verifying")
    mismatches = verify(pipeline, local, tables)
    if mismatches:
        for table, local_count, remote_count in mismatches:
            print(f"sqlite-to-turso: {table}: {local_count} rows locally, {remote_count} remotely", file=sys.stderr)
        return 2
    log(quiet, "row counts match")
    return 0


def has_sequence(local: sqlite3.Connection) -> bool:
    return local.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone() is not None


def load_table(pipeline: Pipeline, local: sqlite3.Connection, table: str, args) -> int:
    quoted = quote_identifier(table)
    columns = [row[1] for row in local.execute(f"PRAGMA table_info({quoted})").fetchall()]
    if not columns:
        return 0
    rows_per_statement = max(1, min(args.rows_per_statement, MAX_PARAMS_PER_STATEMENT // max(1, len(columns))))
    column_list = ", ".join(quote_identifier(c) for c in columns)
    placeholders = "(" + ", ".join("?" for _ in columns) + ")"

    cursor = local.execute(f"SELECT {column_list} FROM {quoted}")
    count = 0
    pending: list[tuple[str, list]] = []
    pending_bytes = 0
    started = time.monotonic()

    def flush() -> None:
        nonlocal pending, pending_bytes
        if pending:
            pipeline.batch(pending)
            pending, pending_bytes = [], 0

    while True:
        rows = cursor.fetchmany(rows_per_statement)
        if not rows:
            break
        values = []
        for row in rows:
            values.extend(encode_value(v) for v in row)
        sql = f"INSERT INTO {quoted} ({column_list}) VALUES " + ", ".join([placeholders] * len(rows))
        size = len(json.dumps(values)) + len(sql)
        if pending and (len(pending) >= args.statements_per_request or pending_bytes + size > MAX_REQUEST_BYTES):
            flush()
        pending.append((sql, values))
        pending_bytes += size
        count += len(rows)
    flush()
    log(args.quiet, f"  {table}: {count} rows in {time.monotonic() - started:.1f} s")
    return count


def verify(pipeline: Pipeline, local: sqlite3.Connection, tables: list[str]) -> list[tuple[str, int, int]]:
    mismatches = []
    for table in tables:
        quoted = quote_identifier(table)
        local_count = local.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
        remote = decode_row(pipeline.execute(f"SELECT COUNT(*) FROM {quoted}")["rows"][0])[0]
        if int(remote) != local_count:
            mismatches.append((table, local_count, int(remote)))
    return mismatches


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
