"""
Browser-Assisted Market Data Fetcher
======================================
Since the sandbox blocks direct API calls, this script provides
instructions for fetching data via the browser, or loads cached data.

In production (on your own machine), the api_client.py will work directly.
"""

import json
import os
from datetime import datetime, timezone


CACHE_FILE = "market_data.json"


def days_until(date_str):
    """Days from now until a given ISO date."""
    if not date_str:
        return None
    try:
        if len(date_str) == 10:  # YYYY-MM-DD
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    now = datetime.now(timezone.utc)
    return max((dt - now).days, 0)


def load_cached_markets():
    """Load markets from the cached JSON file."""
    if not os.path.exists(CACHE_FILE):
        print(f"No cache file found at {CACHE_FILE}")
        print("Paste market data JSON into this file, or use the browser to fetch.")
        return []

    with open(CACHE_FILE) as f:
        data = json.load(f)

    markets = data.get("markets", [])
    fetched = data.get("fetched_at", "unknown")
    print(f"Loaded {len(markets)} markets (fetched: {fetched})")
    return markets


def score_market(m):
    """Score a market for trading interest (0-100)."""
    score = 0
    yp = m.get("yes_price", 0.5)

    # Uncertainty score (markets near 50/50 = more opportunity)
    score += (1 - 2 * abs(yp - 0.5)) * 40

    # Volume score
    v24 = m.get("volume_24h", 0)
    score += min(v24 / 100000, 30)

    # Liquidity score
    liq = m.get("liquidity", 0)
    score += min(liq / 50000, 20)

    # Tight spread bonus
    if m.get("spread", 1) <= 0.03:
        score += 10

    return round(score, 1)


def analyze_markets(markets):
    """Filter and score markets for trading opportunities."""
    tradeable = []

    for m in markets:
        yp = m.get("yes_price", 0)

        # Skip near-certain outcomes (no edge to find)
        if yp < 0.05 or yp > 0.95:
            continue

        # Skip very low volume
        if m.get("volume_24h", 0) < 1000:
            continue

        # Add computed fields
        m["score"] = score_market(m)
        m["days_to_resolution"] = days_until(m.get("end_date"))

        tradeable.append(m)

    tradeable.sort(key=lambda x: x["score"], reverse=True)
    return tradeable


def print_scan_report(markets):
    """Print a formatted scan report."""
    print(f"\n{'='*65}")
    print(f"  POLYMARKET SCAN — {datetime.now().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  {len(markets)} tradeable markets (price between 5c-95c)")
    print(f"{'='*65}\n")

    for i, m in enumerate(markets[:20], 1):
        score = m.get("score", 0)
        grade = 'A' if score >= 70 else 'B' if score >= 55 else 'C' if score >= 40 else 'D'
        q = m["question"]
        yp = m["yes_price"]
        sp = m.get("spread", 0)
        v24 = m.get("volume_24h", 0)
        liq = m.get("liquidity", 0)
        dtl = m.get("days_to_resolution")
        d1 = m.get("price_change_1d", 0)

        print(f"  #{i:2d} [{grade}] {q}")
        print(f"       YES: ${yp:.3f} | Spread: {sp:.3f} | "
              f"Vol24h: ${v24:,.0f} | Liq: ${liq:,.0f}")

        extras = []
        if dtl is not None:
            extras.append(f"Res: {dtl}d")
        if d1:
            extras.append(f"1d: {d1*100:+.1f}%")
        cat = m.get("category", "")
        if cat:
            extras.append(f"Cat: {cat}")
        if extras:
            print(f"       {' | '.join(extras)}")
        print()

    print(f"{'─'*65}")
    print(f"  Use trade_signal.py to generate full signals for specific markets")
    print(f"{'─'*65}\n")


if __name__ == "__main__":
    markets = load_cached_markets()
    if markets:
        tradeable = analyze_markets(markets)
        print_scan_report(tradeable)

        # Save analyzed results
        with open("scan_results.json", "w") as f:
            json.dump(tradeable[:30], f, indent=2, default=str)
        print(f"Top 30 saved to scan_results.json")
