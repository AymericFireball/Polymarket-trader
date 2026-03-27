"""
Import Resolved Markets for Calibration Bootstrapping
======================================================
Two modes:
  1. CSV mode:  python import_resolved.py --csv resolved_markets_400.csv
  2. Fetch mode: python import_resolved.py --fetch --pages 5
                 (fetch mode requires network; use --browser flag if in sandbox)

CSV format (with or without header):
  condition_id,question,final_price,resolution,volume,end_date
"""

import argparse
import csv
import json
import os
import sys
import sqlite3
from datetime import datetime, timezone

# ── Path setup ────────────────────────────────────────────────────────────────
_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _dir)

from db import get_conn, init_db, DB_PATH

# ── Category classification ───────────────────────────────────────────────────
_SPORTS_KEYWORDS = {
    "vs.", "o/u", "spread:", "moneyline", "celtics", "lakers", "warriors",
    "nuggets", "knicks", "cavaliers", "suns", "bucks", "nets", "76ers",
    "pacers", "clippers", "thunder", "heat", "magic", "pistons", "kings",
    "hornets", "pelicans", "raptors", "hawks", "bulls", "jazz", "grizzlies",
    "spurs", "rockets", "mavericks", "blazers", "wolves", "wizards", "cavs",
    # Hockey
    "flyers", "capitals", "bruins", "maple leafs", "jets", "oilers",
    "avalanche", "panthers", "lightning", "kraken", "ducks", "canucks",
    "blue jackets", "islanders", "blackhawks", "senators", "red wings",
    "sharks", "predators", "penguins", "wild", "hurricanes", "canadiens",
    "golden knights", "devils", "blues", "flames", "stars",
    # Tennis / other
    "miami open", "open:", "atp", "wta", "grand slam",
    # esports
    "dota 2", "cs2", "league of legends", "valorant",
    # Baseball / soccer generic
    "mlb", "nfl", "nba", "nhl", "mls",
}

_CRYPTO_KEYWORDS = {
    "bitcoin", "btc", "ethereum", "eth", "sol", "solana", "xrp", "bnb",
    "fdv", "token", "crypto", "defi", "nft", "airdrop", "launch",
    "backpack", "coinbase", "binance", "kraken exchange",
}

_POLITICAL_KEYWORDS = {
    "president", "election", "vote", "congress", "senate", "house", "trump",
    "biden", "harris", "democrat", "republican", "gop", "primary", "ballot",
    "minister", "parliament", "fed", "federal reserve", "rate cut", "tariff",
    "elon musk", "doge", "musk post",
}


def classify_question(question: str) -> str:
    q = question.lower()
    for kw in _SPORTS_KEYWORDS:
        if kw in q:
            return "sports"
    for kw in _CRYPTO_KEYWORDS:
        if kw in q:
            return "crypto"
    for kw in _POLITICAL_KEYWORDS:
        if kw in q:
            return "political"
    return "other"


# ── Brier score helper ────────────────────────────────────────────────────────

def brier(predicted: float, actual: float) -> float:
    return (predicted - actual) ** 2


# ── Core import function ──────────────────────────────────────────────────────

def import_rows(rows: list[dict], conn: sqlite3.Connection, skip_sports: bool = False) -> dict:
    """
    Import a list of resolved market dicts into the DB.
    Each dict must have: condition_id, question, final_price, resolution, volume, end_date
    Returns stats dict.
    """
    stats = {"total": 0, "inserted_markets": 0, "inserted_resolutions": 0,
             "skipped_sports": 0, "skipped_no_resolution": 0, "errors": 0}

    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        stats["total"] += 1
        cid = row.get("condition_id", "").strip()
        question = row.get("question", "").strip()
        resolution = row.get("resolution", "").strip()  # "Yes", "No", "1", "0", ""
        final_price_raw = row.get("final_price", "").strip()
        volume = row.get("volume", 0)
        end_date = row.get("end_date", "").strip()

        if not cid or not cid.startswith("0x"):
            continue

        # Normalise resolution
        if resolution in ("1", "Yes", "YES", "yes"):
            resolution_norm = "Yes"
            actual_outcome = 1.0
        elif resolution in ("0", "No", "NO", "no"):
            resolution_norm = "No"
            actual_outcome = 0.0
        else:
            stats["skipped_no_resolution"] += 1
            continue

        # Parse final_price
        try:
            final_price = float(final_price_raw) if final_price_raw else (1.0 if resolution_norm == "Yes" else 0.0)
        except ValueError:
            final_price = 1.0 if resolution_norm == "Yes" else 0.0

        # Classify
        category = classify_question(question)

        if skip_sports and category == "sports":
            stats["skipped_sports"] += 1
            continue

        try:
            # ── Upsert into markets table ────────────────────────────
            conn.execute("""
                INSERT INTO markets (
                    condition_id, question, slug, category,
                    end_date, yes_price, no_price, total_volume,
                    active, closed, resolved, resolution, resolved_at,
                    last_scraped_at, market_type
                ) VALUES (?,?,?,?,?,?,?,?,0,1,1,?,?,?,?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    resolved = 1,
                    resolution = excluded.resolution,
                    resolved_at = excluded.resolved_at,
                    yes_price = excluded.yes_price,
                    no_price = excluded.no_price,
                    total_volume = COALESCE(excluded.total_volume, total_volume),
                    last_scraped_at = excluded.last_scraped_at
            """, (
                cid,
                question,
                cid[:16],       # slug placeholder
                category,
                end_date or None,
                final_price,
                1.0 - final_price,
                float(str(volume).replace(",", "") or 0),
                resolution_norm,
                end_date or now,
                now,
                category,
            ))
            stats["inserted_markets"] += 1

            # ── Upsert into resolutions table ────────────────────────
            # Check if we have a prediction for this market
            pred_row = conn.execute(
                "SELECT prediction_id, our_estimate FROM predictions WHERE condition_id=? ORDER BY predicted_at DESC LIMIT 1",
                (cid,)
            ).fetchone()

            pred_id = None
            our_est = None
            brier_ours = None
            our_delta = None

            if pred_row:
                pred_id = pred_row["prediction_id"]
                our_est = pred_row["our_estimate"]
                brier_ours = brier(our_est, actual_outcome)
                our_delta = our_est - actual_outcome

            brier_market = brier(final_price, actual_outcome)

            conn.execute("""
                INSERT INTO resolutions (
                    condition_id, question, resolution, resolved_at,
                    final_price, prediction_id, our_estimate, our_delta,
                    brier_score_market, brier_score_ours
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    resolution = excluded.resolution,
                    final_price = excluded.final_price,
                    brier_score_market = excluded.brier_score_market
            """, (
                cid, question, resolution_norm,
                end_date or now,
                final_price,
                pred_id, our_est, our_delta,
                brier_market, brier_ours,
            ))
            stats["inserted_resolutions"] += 1

        except Exception as e:
            stats["errors"] += 1
            if stats["errors"] <= 3:
                print(f"  Error on {cid[:20]}…: {e}")

    conn.commit()
    return stats


# ── CSV import ────────────────────────────────────────────────────────────────

def import_csv(csv_path: str, skip_sports: bool = False) -> dict:
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        first = f.read(1024)
        f.seek(0)
        has_header = "condition_id" in first.lower() or "question" in first.lower()
        reader = csv.DictReader(f) if has_header else csv.reader(f)

        for raw in reader:
            if has_header:
                rows.append({
                    "condition_id": raw.get("condition_id", ""),
                    "question": raw.get("question", ""),
                    "final_price": raw.get("final_price", ""),
                    "resolution": raw.get("resolution", ""),
                    "volume": raw.get("volume", 0),
                    "end_date": raw.get("end_date", ""),
                })
            else:
                # Positional: cid,question,final_price,resolution,volume,end_date
                r = list(raw)
                rows.append({
                    "condition_id": r[0] if len(r) > 0 else "",
                    "question": r[1] if len(r) > 1 else "",
                    "final_price": r[2] if len(r) > 2 else "",
                    "resolution": r[3] if len(r) > 3 else "",
                    "volume": r[4] if len(r) > 4 else 0,
                    "end_date": r[5] if len(r) > 5 else "",
                })
    return rows


# ── Inline data import (pipe-delimited blocks from browser extraction) ────────

def import_inline(text: str, skip_sports: bool = False) -> dict:
    """Parse the compact CSV format output by the browser JS extractor."""
    rows = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line.startswith("0x"):
            continue
        parts = line.split(",", 5)
        if len(parts) < 2:
            continue
        rows.append({
            "condition_id": parts[0],
            "question": parts[1] if len(parts) > 1 else "",
            "final_price": parts[2] if len(parts) > 2 else "",
            "resolution": parts[3] if len(parts) > 3 else "",
            "volume": parts[4] if len(parts) > 4 else 0,
            "end_date": parts[5] if len(parts) > 5 else "",
        })
    return rows


# ── Summary printer ───────────────────────────────────────────────────────────

def print_summary(stats: dict, conn: sqlite3.Connection):
    total_resolved = conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
    by_cat = conn.execute("""
        SELECT m.market_type, COUNT(*) as n
        FROM resolutions r
        JOIN markets m ON r.condition_id = m.condition_id
        GROUP BY m.market_type
    """).fetchall()
    avg_brier = conn.execute(
        "SELECT AVG(brier_score_market) FROM resolutions WHERE brier_score_market IS NOT NULL"
    ).fetchone()[0]

    print("\n── Import Summary ──────────────────────────────")
    print(f"  Processed  : {stats['total']}")
    print(f"  Markets ↑  : {stats['inserted_markets']}")
    print(f"  Resolutions: {stats['inserted_resolutions']}")
    print(f"  Skip sports: {stats['skipped_sports']}")
    print(f"  No resol.  : {stats['skipped_no_resolution']}")
    print(f"  Errors     : {stats['errors']}")
    print(f"\n── DB State ────────────────────────────────────")
    print(f"  Total resolutions in DB : {total_resolved}")
    if avg_brier is not None:
        print(f"  Avg market Brier score  : {avg_brier:.4f}  (lower = better)")
    for row in by_cat:
        print(f"    {row[0] or 'unknown':15s}: {row[1]}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import resolved markets for calibration")
    parser.add_argument("--csv", help="Path to CSV file (with or without header)")
    parser.add_argument("--skip-sports", action="store_true", default=False,
                        help="Skip sports markets (default: import all for base rate diversity)")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't write to DB")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    # ── Resolve CSV path ──
    csv_path = args.csv
    if not csv_path:
        # Look for CSV in project dir
        candidates = [
            os.path.join(_dir, "resolved_markets_400.csv"),
            os.path.join(_dir, "resolved_markets.csv"),
            os.path.join(os.path.expanduser("~"), "Downloads", "resolved_markets_400.csv"),
            os.path.join(os.path.expanduser("~"), "Downloads", "resolved_markets.csv"),
        ]
        for c in candidates:
            if os.path.exists(c):
                csv_path = c
                print(f"Auto-found CSV: {csv_path}")
                break

    if not csv_path:
        print("No CSV file specified and none found in project dir or Downloads.")
        print("Usage: python import_resolved.py --csv /path/to/resolved_markets_400.csv")
        sys.exit(1)

    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        sys.exit(1)

    print(f"Importing from: {csv_path}")
    rows = import_csv(csv_path, skip_sports=args.skip_sports)
    print(f"Parsed {len(rows)} rows from CSV")

    if args.dry_run:
        print("Dry run — not writing to DB")
        for r in rows[:5]:
            print(" ", r)
        return

    stats = import_rows(rows, conn, skip_sports=args.skip_sports)
    print_summary(stats, conn)
    conn.close()


if __name__ == "__main__":
    main()
