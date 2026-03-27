"""
Import markets from browser-fetched JSON chunks into the database.
Run this after saving chunk files from the browser.
"""
import json
import sys
import os
from db import init_db, get_conn, upsert_market, db_stats

def import_chunk(filepath, conn):
    with open(filepath) as f:
        data = json.load(f)
    markets = data if isinstance(data, list) else data.get("markets", [])
    count = 0
    for m in markets:
        cid = m.get("conditionId") or m.get("condition_id", "")
        if not cid:
            continue
        upsert_market(conn, m)
        count += 1
    conn.commit()
    return count

if __name__ == "__main__":
    init_db()
    conn = get_conn()
    total = 0
    for f in sys.argv[1:]:
        if os.path.exists(f):
            n = import_chunk(f, conn)
            total += n
            print(f"Imported {n} from {f}")
    print(f"\nTotal imported: {total}")
    print(json.dumps(db_stats(conn), indent=2))
    conn.close()
