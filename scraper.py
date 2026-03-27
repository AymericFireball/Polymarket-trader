"""
Polymarket Market Scraper
==========================
Pulls all open + resolved markets from the Gamma API and stores them in the database.
Designed to be run on a schedule (cron) to build the calibration dataset.

Usage:
    python scraper.py                    # Scrape open markets
    python scraper.py --resolved         # Scrape resolved markets too
    python scraper.py --from-json FILE   # Import from a JSON file (browser-fetched)
    python scraper.py --stats            # Show database stats
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import List, Dict

from db import init_db, get_conn, upsert_market, db_stats, classify_market


# ─── API scraping (works when not in sandbox) ──────────────────

def scrape_gamma_api(active: bool = True, closed: bool = False,
                     limit: int = 100, max_pages: int = 10) -> List[Dict]:
    """Fetch markets from the Gamma API with pagination."""
    import requests

    base_url = "https://gamma-api.polymarket.com/markets"
    all_markets = []

    for page in range(max_pages):
        offset = page * limit
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": "volume24hr",
            "ascending": "false",
        }

        try:
            resp = requests.get(base_url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if isinstance(data, list):
                markets = data
            else:
                markets = data.get("data", data.get("markets", []))

            if not markets:
                break

            all_markets.extend(markets)
            print(f"  Page {page + 1}: fetched {len(markets)} markets (total: {len(all_markets)})")

            if len(markets) < limit:
                break

        except Exception as e:
            print(f"  API error on page {page + 1}: {e}")
            break

    return all_markets


def normalize_gamma_market(raw: Dict) -> Dict:
    """Convert raw Gamma API market to our DB format."""
    # Parse outcome prices
    yes_price = None
    no_price = None
    outcome_prices = raw.get("outcomePrices", "")
    if outcome_prices:
        try:
            if isinstance(outcome_prices, str):
                prices = json.loads(outcome_prices)
            else:
                prices = outcome_prices
            yes_price = float(prices[0])
            no_price = float(prices[1]) if len(prices) > 1 else 1 - yes_price
        except (json.JSONDecodeError, IndexError, ValueError, TypeError):
            pass

    # Parse outcomes
    outcomes = ["Yes", "No"]
    raw_outcomes = raw.get("outcomes", "")
    if raw_outcomes:
        try:
            outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else raw_outcomes
        except (json.JSONDecodeError, TypeError):
            pass

    # Parse token IDs
    token_ids = []
    raw_tokens = raw.get("clobTokenIds", "")
    if raw_tokens:
        try:
            token_ids = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        except (json.JSONDecodeError, TypeError):
            pass

    # Determine resolution for closed markets
    resolution = None
    resolved = False
    if raw.get("closed") and raw.get("umaResolutionStatus") == "resolved":
        resolved = True
        # If yes_price >= 0.99, resolved YES; if <= 0.01, resolved NO
        if yes_price is not None:
            if yes_price >= 0.99:
                resolution = "Yes"
            elif yes_price <= 0.01:
                resolution = "No"

    # Category from series
    category = ""
    events = raw.get("events", [])
    if events and events[0].get("series"):
        series = events[0]["series"]
        if series:
            category = series[0].get("title", "")

    return {
        "conditionId": raw.get("conditionId") or raw.get("condition_id", ""),
        "question": raw.get("question", ""),
        "slug": raw.get("slug", ""),
        "description": raw.get("description", ""),
        "category": category,
        "outcomes": outcomes,
        "token_ids": token_ids,
        "resolutionSource": raw.get("resolutionSource", ""),
        "end_date": raw.get("endDateIso") or raw.get("end_date", ""),
        "createdAt": raw.get("createdAt", ""),
        "yes_price": yes_price,
        "no_price": no_price,
        "spread": raw.get("spread"),
        "bestBid": raw.get("bestBid") or raw.get("best_bid"),
        "bestAsk": raw.get("bestAsk") or raw.get("best_ask"),
        "volume24hr": raw.get("volume24hr") or raw.get("volume_24h"),
        "volumeNum": raw.get("volumeNum") or raw.get("total_volume"),
        "liquidityNum": raw.get("liquidityNum") or raw.get("liquidity"),
        "oneDayPriceChange": raw.get("oneDayPriceChange") or raw.get("price_change_1d"),
        "oneWeekPriceChange": raw.get("oneWeekPriceChange") or raw.get("price_change_1w"),
        "active": raw.get("active", True),
        "closed": raw.get("closed", False),
        "acceptingOrders": raw.get("acceptingOrders") or raw.get("accepting_orders", True),
        "resolved": resolved,
        "resolution": resolution,
        "closedTime": raw.get("closedTime") or raw.get("resolved_at"),
        "negRisk": raw.get("negRisk") or raw.get("neg_risk", False),
        "tags": raw.get("tags", []),
    }


# ─── Import from JSON ──────────────────────────────────────────

def import_from_json(filepath: str, conn) -> int:
    """Import markets from a JSON file into the database."""
    with open(filepath) as f:
        data = json.load(f)

    markets = data if isinstance(data, list) else data.get("markets", [])
    count = 0

    for raw in markets:
        # Check if it's already normalized or raw from Gamma
        if "conditionId" in raw or "condition_id" in raw:
            if "conditionId" not in raw and "condition_id" in raw:
                raw["conditionId"] = raw["condition_id"]
            normalized = normalize_gamma_market(raw)
        else:
            normalized = raw

        cid = normalized.get("conditionId") or normalized.get("condition_id", "")
        if not cid:
            continue

        upsert_market(conn, normalized)
        count += 1

    conn.commit()
    return count


# ─── Full scrape pipeline ──────────────────────────────────────

def full_scrape(include_resolved: bool = False, conn=None):
    """Run a full scrape of open (and optionally resolved) markets."""
    own_conn = conn is None
    if own_conn:
        init_db()
        conn = get_conn()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n{'='*55}")
    print(f"  POLYMARKET SCRAPER — {now}")
    print(f"{'='*55}\n")

    # Scrape open markets
    print("[1] Fetching active open markets...")
    try:
        open_markets = scrape_gamma_api(active=True, closed=False, limit=100, max_pages=10)
        print(f"    Got {len(open_markets)} open markets")

        for raw in open_markets:
            normalized = normalize_gamma_market(raw)
            if normalized.get("conditionId"):
                upsert_market(conn, normalized)
        conn.commit()
        print(f"    Stored in database")
    except Exception as e:
        print(f"    Error: {e}")
        print(f"    (If in sandbox, use --from-json to import browser-fetched data)")

    # Scrape resolved markets
    if include_resolved:
        print("\n[2] Fetching resolved markets...")
        try:
            resolved_markets = scrape_gamma_api(active=False, closed=True, limit=100, max_pages=20)
            print(f"    Got {len(resolved_markets)} resolved markets")

            for raw in resolved_markets:
                normalized = normalize_gamma_market(raw)
                if normalized.get("conditionId"):
                    upsert_market(conn, normalized)
            conn.commit()
            print(f"    Stored in database")
        except Exception as e:
            print(f"    Error: {e}")

    # Print stats
    stats = db_stats(conn)
    print(f"\n{'─'*55}")
    print(f"  DATABASE STATS")
    print(f"  Markets: {stats['markets']} total, {stats['active_markets']} active, {stats['resolved_markets']} resolved")
    print(f"  Predictions: {stats['predictions']}")
    print(f"  Trades: {stats.get('open_trades', 0)} open")
    if stats.get("by_type"):
        print(f"  By type: {json.dumps(stats['by_type'])}")
    print(f"{'─'*55}\n")

    if own_conn:
        conn.close()


# ─── Browser-fetch helper ──────────────────────────────────────

BROWSER_FETCH_SCRIPT = """
// Paste this in the browser console at gamma-api.polymarket.com
// or use the dashboard's Refresh button

async function fetchAndSave(type) {
  const active = type === 'open';
  const closed = type === 'resolved';
  let all = [];
  for (let offset = 0; offset < 2000; offset += 100) {
    const url = `https://gamma-api.polymarket.com/markets?limit=100&offset=${offset}&active=${active}&closed=${closed}&order=volume24hr&ascending=false`;
    const resp = await fetch(url);
    const data = await resp.json();
    if (!data.length) break;
    all.push(...data);
    console.log(`Fetched ${all.length} ${type} markets...`);
  }
  const blob = new Blob([JSON.stringify({fetched_at: new Date().toISOString(), total: all.length, markets: all})], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `polymarket_${type}_${new Date().toISOString().split('T')[0]}.json`;
  a.click();
  console.log(`Downloaded ${all.length} ${type} markets`);
}

// Run: fetchAndSave('open') or fetchAndSave('resolved')
"""


def main():
    parser = argparse.ArgumentParser(description="Polymarket Market Scraper")
    parser.add_argument("--resolved", action="store_true", help="Also scrape resolved markets")
    parser.add_argument("--from-json", type=str, help="Import markets from a JSON file")
    parser.add_argument("--stats", action="store_true", help="Show database stats")
    parser.add_argument("--browser-script", action="store_true",
                        help="Print browser console script for manual data fetch")
    args = parser.parse_args()

    init_db()

    if args.browser_script:
        print(BROWSER_FETCH_SCRIPT)
        return

    if args.stats:
        conn = get_conn()
        stats = db_stats(conn)
        print(json.dumps(stats, indent=2))
        conn.close()
        return

    if args.from_json:
        conn = get_conn()
        count = import_from_json(args.from_json, conn)
        print(f"Imported {count} markets from {args.from_json}")
        stats = db_stats(conn)
        print(f"Database now has {stats['markets']} total markets")
        conn.close()
        return

    full_scrape(include_resolved=args.resolved)


if __name__ == "__main__":
    main()
