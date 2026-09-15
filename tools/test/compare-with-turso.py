#!/usr/bin/env python3
"""Compare a SQLite file with a Turso database over the pipeline, table by table."""

import base64
import json
import sqlite3
import sys
import urllib.request


def pipeline(url, sql):
    body = json.dumps({"requests": [{"type": "execute", "stmt": {"sql": sql}}]}).encode()
    req = urllib.request.Request(url + "/v2/pipeline", data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = json.load(response)
    result = payload["results"][0]
    if result["type"] == "error":
        raise SystemExit(f"pipeline error: {result['error']}")
    rows = []
    for row in result["response"]["result"]["rows"]:
        values = []
        for cell in row:
            kind = cell["type"]
            if kind == "null":
                values.append(None)
            elif kind == "integer":
                values.append(int(cell["value"]))
            elif kind == "float":
                values.append(float(cell["value"]))
            elif kind == "blob":
                values.append(base64.b64decode(cell["base64"]))
            else:
                values.append(cell["value"])
        rows.append(tuple(values))
    return rows


def main(argv):
    source, url = argv[0], argv[1]
    if len(argv) > 3 and argv[2] == "--query":
        for row in pipeline(url, argv[3]):
            print("\t".join("" if v is None else str(v) for v in row))
        return 0
    local = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    tables = [r[0] for r in local.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
    remote_tables = [r[0] for r in pipeline(url, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND name NOT LIKE '\\_\\_turso\\_internal\\_%' ESCAPE '\\' ORDER BY name")]
    if tables != remote_tables:
        print(f"table lists differ:\n  local:  {tables}\n  remote: {remote_tables}", file=sys.stderr)
        return 1
    status = 0
    for table in tables:
        columns = [r[1] for r in local.execute(f'PRAGMA table_info("{table}")')]
        select = f'SELECT {", ".join(chr(34) + c + chr(34) for c in columns)} FROM "{table}" ORDER BY {", ".join(str(i + 1) for i in range(len(columns)))}'
        local_rows = [tuple(r) for r in local.execute(select)]
        remote_rows = pipeline(url, select)
        if local_rows != remote_rows:
            status = 1
            print(f"{table}: {len(local_rows)} local rows vs {len(remote_rows)} remote rows differ", file=sys.stderr)
            for l, r in zip(local_rows, remote_rows):
                if l != r:
                    print(f"  local:  {l!r}\n  remote: {r!r}", file=sys.stderr)
                    break
        else:
            print(f"{table}: {len(local_rows)} rows identical")
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
