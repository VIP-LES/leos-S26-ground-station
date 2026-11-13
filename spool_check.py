#!/usr/bin/env python3
"""Read-only look at the Pi's SQLite spool: what is queued, and what has been sent?

Run on the ground-station Pi, from the repo root, venv active:
    python3 spool_check.py
It changes nothing. Paste the output back.
"""

import os
import sqlite3
import sys


def load_env(path):
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip().removeprefix("export ").strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


load_env(".env")
path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SPOOL_DB_PATH")
if not path:
    sys.exit("SPOOL_DB_PATH not set; pass the path: python3 spool_check.py /path/to/spool.db")
print("spool file:", path, "| exists:", os.path.exists(path))
db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)

tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1")]
print("tables:", tables)
for t in tables:
    cols = [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')]
    total = db.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
    print(f"\n--- {t}: {total} rows | columns: {cols}")
    # Group by any column that looks like a type or a delivery status.
    for c in cols:
        if any(w in c.lower() for w in ("type", "kind", "sent", "status", "state", "attempt", "error")):
            rows = db.execute(f'SELECT "{c}", count(*) FROM "{t}" GROUP BY 1 ORDER BY 2 DESC LIMIT 10').fetchall()
            print(f"  by {c}: {rows}")
    order = "id" if "id" in cols else "rowid"
    for row in db.execute(f'SELECT * FROM "{t}" ORDER BY {order} DESC LIMIT 4'):
        print("  ", [(str(v)[:90] + "...") if isinstance(v, (str, bytes)) and len(str(v)) > 90 else v for v in row])
