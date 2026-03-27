"""
Trade Signal Generator
=======================
Takes a market condition_id and your probability estimate,
then outputs a full structured trade signal per the trading system.

Usage:
    python trade_signal.py <condition_id> <your_probability> [confidence]

Example:
    python trade_signal.py 0x123abc 0.72 High
"""

import sys
import json
from datetime import datetime

from api_client import PolymarketClient
from risk_manager import RiskManager, PortfolioState
from config import BANKROLL


def generate_signal(condition_id: str, my_prob: float, confidence: str = "Medium"):
    """Generate a structured trade signal for a market."""

    client = PolymarketClient()
    risk_mgr = RiskManager(PortfolioState(bankroll=BANKROLL))

    # Fetch market data
    print(f"\nFetching market data for {condition_id[:20]}...")

    # Try Gamma API first for rich data
    gamma_markets = client.get_gamma_markets(limit=200)
    market = None
    for m in gamma_markets:
        cid = m.get("conditionId") or m.get("condition_id", "")
        if cid == condition_id:
            market = m
            break

    if not market:
        # Try CLOB API
        market = client.get_market(condition_id)

    if not market:
        print(f"Market not found: {condition_id}")
        return None

    question = market.get("question", "Unknown")
    description = (market.get("description") or "")[:300]

    # Get prices
    yes_price = None
    outcome_prices = market.get("outcomePrices", "")
    if outcome_prices:
        try:
            prices = outcome_prices.split(",") if isinstance(outcome_prices, str) else outcome_prices
            yes_price = float(prices[0])
        except (IndexError, ValueError):
            pass

    # Get token IDs
    tokens = market.get("tokens", [])
    if not tokens and market.get("clobTokenIds"):
        clob_ids = market.get("clobTokenIds", "")
        if isinstance(clob_ids, str):
            try:
                token_list = json.loads(clob_ids)
            except (json.JSONDecodeError, TypeError):
                token_list = []
        else:
            token_list = clob_ids
        tokens = [{"token_id": tid, "outcome": ["Yes", "No"][i]} for i, tid in enumerate(token_list)]

    # Analyze order book if we have token IDs
    book_data = {}
    if tokens:
        yes_token_id = tokens[0].get("token_id", "")
        if yes_token_id:
            book_data = client.analyze_order_book(yes_token_id)
            if not yes_price and book_data.get("midpoint"):
                yes_price = book_data["midpoint"]

    if yes_price is None:
        yes_price = 0.5
        print("  Warning: Could not determine current price, using 0.50")

    no_price = 1.0 - yes_price

    # Calculate sizing
    sizing = risk_mgr.kelly_size(my_prob, yes_price, confidence)

    # Edge calculation
    edge_cents = round((my_prob - yes_price) * 100, 1)

    # Determine entry target (slightly inside spread for maker status)
    spread = book_data.get("spread", 0.02)
    if sizing["side"] == "BUY YES":
        entry_target = round(yes_price - (spread * 0.3), 4)  # Bid inside spread
    else:
        entry_target = round(no_price - (spread * 0.3), 4)

    # ─── Output ─────────────────────────────────────────────────────

    print(f"\n{'='*60}")
    print(f"  TRADE SIGNAL — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*60}\n")

    # Market assessment
    print("MARKET ASSESSMENT:")
    print(f"  Market:       {question}")
    print(f"  Current:      YES ${yes_price:.2f} / NO ${no_price:.2f}")
    print(f"  My estimate:  YES {my_prob*100:.0f}% / NO {(1-my_prob)*100:.0f}%")
    print(f"  Confidence:   {confidence}")
    print(f"  Edge:         {edge_cents:+.1f} cents")
    print(f"  Description:  {description[:150]}...")
    print()

    # Order book
    if book_data and not book_data.get("error"):
        print("ORDER BOOK:")
        print(f"  Best bid:     ${book_data.get('best_bid', 0):.4f}")
        print(f"  Best ask:     ${book_data.get('best_ask', 0):.4f}")
        print(f"  Spread:       ${book_data.get('spread', 0):.4f}")
        print(f"  Bid depth:    ${book_data.get('bid_depth_usd', 0):,.0f}")
        print(f"  Ask depth:    ${book_data.get('ask_depth_usd', 0):,.0f}")
        print()

    # Trade signal
    passes = sizing.get("passes_risk_checks", False)
    signal_icon = ">>>" if passes else "XXX"

    print(f"{signal_icon} TRADE SIGNAL:")
    print(f"  Side:         {sizing['side']}")
    print(f"  Entry target: ${entry_target:.4f} (limit order)")
    print(f"  Order type:   GTC")
    print(f"  Size:         ${sizing.get('position_size_usd', 0):.2f} "
          f"({sizing.get('adjusted_pct', 0):.1f}% of bankroll)")
    print(f"  Quantity:     {sizing.get('quantity', 0):.1f} shares")
    print(f"  Kelly:        {sizing.get('kelly_pct', 0):.1f}% full / "
          f"{sizing.get('half_kelly_pct', 0):.1f}% half")
    print()

    # Risk checks
    print("RISK CHECKS:")
    checks = sizing.get("risk_checks", [])
    if checks:
        for c in checks:
            icon = "X" if "BLOCKED" in c else "!"
            print(f"  [{icon}] {c}")
    else:
        print("  [OK] All checks passed")
    print()

    # Exit plan
    stop = sizing.get("stop_loss", 0)
    tp = sizing.get("take_profit", 0.93)
    print("EXIT PLAN:")
    print(f"  Stop-loss:    ${stop:.4f} (15c against entry)")
    print(f"  Take-profit:  ${tp} (trim when token > 93c)")
    print(f"  Time review:  Weekly thesis revalidation")
    print(f"  Invalidation: [ADD YOUR INVALIDATION CRITERIA]")
    print()

    # Action summary
    if passes:
        print(f"{'─'*60}")
        print(f"  STATUS: ACTIONABLE — Edge sufficient, risk checks passed")
        print(f"  Place a GTC limit order at ${entry_target:.4f} for "
              f"{sizing.get('quantity', 0):.0f} shares")
        print(f"{'─'*60}")
    else:
        print(f"{'─'*60}")
        print(f"  STATUS: DO NOT TRADE — Risk checks failed")
        for c in checks:
            if "BLOCKED" in c:
                print(f"  Reason: {c}")
        print(f"{'─'*60}")

    print()
    return {
        "market": question,
        "side": sizing["side"],
        "entry_target": entry_target,
        "size_usd": sizing.get("position_size_usd", 0),
        "edge_cents": edge_cents,
        "passes": passes,
    }


def main():
    if len(sys.argv) < 3:
        print("Usage: python trade_signal.py <condition_id> <your_probability> [confidence]")
        print("Example: python trade_signal.py 0x123abc 0.72 High")
        sys.exit(1)

    condition_id = sys.argv[1]
    my_prob = float(sys.argv[2])
    confidence = sys.argv[3] if len(sys.argv) > 3 else "Medium"

    if not 0 < my_prob < 1:
        print("Probability must be between 0 and 1 (e.g., 0.72 for 72%)")
        sys.exit(1)

    generate_signal(condition_id, my_prob, confidence)


if __name__ == "__main__":
    main()
