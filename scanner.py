"""
Polymarket Market Scanner
==========================
Scans active markets, analyzes liquidity and pricing,
identifies potential edges, and outputs structured trade signals.

Usage:
    python scanner.py              # Full scan
    python scanner.py --top 10     # Show top 10 opportunities
    python scanner.py --category crypto  # Filter by category
"""

import sys
import json
import argparse
from datetime import datetime, timezone
from typing import List, Dict, Optional

from api_client import PolymarketClient
from risk_manager import RiskManager, PortfolioState
from config import (
    MIN_LIQUIDITY_USD, MAX_DAYS_TO_RESOLUTION, MIN_VOLUME_24H,
    MIN_EDGE_CENTS, FOCUS_CATEGORIES, BANKROLL,
)


def parse_date(date_str: Optional[str]) -> Optional[datetime]:
    """Parse ISO date string, handling various formats."""
    if not date_str:
        return None
    try:
        # Strip trailing 'Z' and try parsing
        cleaned = date_str.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None


def days_until(date_str: Optional[str]) -> Optional[int]:
    """Days from now until a given ISO date."""
    dt = parse_date(date_str)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    delta = dt - now
    return max(delta.days, 0)


class MarketScanner:
    """Scans Polymarket for trading opportunities."""

    def __init__(self):
        self.client = PolymarketClient()
        self.risk_mgr = RiskManager(PortfolioState(bankroll=BANKROLL))

    def scan(self, max_markets: int = 200, category_filter: str = "") -> List[Dict]:
        """
        Full market scan pipeline:
        1. Fetch active markets from Gamma API (rich metadata)
        2. Filter by liquidity, volume, time horizon
        3. Analyze order books for interesting markets
        4. Score and rank opportunities
        """
        print(f"\n{'='*60}")
        print(f"  POLYMARKET SCANNER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"  Bankroll: ${BANKROLL:,.0f} | Min edge: {MIN_EDGE_CENTS}c")
        print(f"{'='*60}\n")

        # Step 1: Fetch markets
        print("[1/4] Fetching active markets...")
        raw_markets = self.client.get_gamma_markets(limit=max_markets, active=True)
        print(f"       Found {len(raw_markets)} active markets")

        if not raw_markets:
            print("       No markets returned. API may be down or rate-limited.")
            return []

        # Step 2: Filter
        print("[2/4] Filtering markets...")
        filtered = self._filter_markets(raw_markets, category_filter)
        print(f"       {len(filtered)} markets pass filters")

        if not filtered:
            print("       No markets pass filters. Try relaxing constraints.")
            return []

        # Step 3: Analyze order books for top candidates
        print(f"[3/4] Analyzing order books for top {min(len(filtered), 30)} markets...")
        analyzed = self._analyze_markets(filtered[:30])
        print(f"       Analyzed {len(analyzed)} markets")

        # Step 4: Score and rank
        print("[4/4] Scoring opportunities...\n")
        scored = self._score_markets(analyzed)
        scored.sort(key=lambda x: x.get("score", 0), reverse=True)

        # Output
        self._print_results(scored)
        return scored

    def _filter_markets(self, markets: list, category_filter: str) -> list:
        """Apply basic filters: volume, liquidity, time horizon, category."""
        filtered = []
        for m in markets:
            # Must be active and accepting orders
            if not m.get("active", False):
                continue

            # Volume filter
            vol_24h = float(m.get("volume24hr", 0) or 0)
            if vol_24h < MIN_VOLUME_24H:
                continue

            # Time horizon filter
            end_date = m.get("endDate") or m.get("end_date_iso")
            dtl = days_until(end_date)
            if dtl is not None and dtl > MAX_DAYS_TO_RESOLUTION:
                continue

            # Category filter
            if category_filter:
                tags = (m.get("tags") or []) + [m.get("category", "")]
                tag_str = " ".join(str(t) for t in tags).lower()
                if category_filter.lower() not in tag_str:
                    # Also check question text
                    question = (m.get("question") or "").lower()
                    if category_filter.lower() not in question:
                        continue

            # Liquidity filter (use volume as proxy if liquidity not available)
            liquidity = float(m.get("liquidityNum", 0) or m.get("liquidity", 0) or 0)
            if liquidity > 0 and liquidity < MIN_LIQUIDITY_USD:
                continue

            filtered.append(m)

        # Sort by 24h volume descending
        filtered.sort(key=lambda x: float(x.get("volume24hr", 0) or 0), reverse=True)
        return filtered

    def _analyze_markets(self, markets: list) -> list:
        """Fetch order book data and compute metrics for each market."""
        analyzed = []
        for m in markets:
            # Get token IDs
            tokens = m.get("tokens", [])
            if not tokens and m.get("clobTokenIds"):
                # Gamma API sometimes provides IDs differently
                clob_ids = m.get("clobTokenIds", "")
                if isinstance(clob_ids, str):
                    try:
                        token_list = json.loads(clob_ids)
                    except (json.JSONDecodeError, TypeError):
                        token_list = []
                else:
                    token_list = clob_ids
                outcomes = m.get("outcomes", '["Yes","No"]')
                if isinstance(outcomes, str):
                    try:
                        outcomes = json.loads(outcomes)
                    except (json.JSONDecodeError, TypeError):
                        outcomes = ["Yes", "No"]
                tokens = [
                    {"token_id": tid, "outcome": outcomes[i] if i < len(outcomes) else f"Outcome_{i}"}
                    for i, tid in enumerate(token_list)
                ]

            if not tokens:
                continue

            # Get YES token price (first token is typically YES)
            yes_token = tokens[0] if tokens else {}
            yes_token_id = yes_token.get("token_id", "")

            if not yes_token_id:
                continue

            # Fetch order book
            book_analysis = self.client.analyze_order_book(yes_token_id)
            if book_analysis.get("error"):
                continue

            # Get current prices
            yes_price = float(m.get("outcomePrices", "0.5,0.5").split(",")[0]) if m.get("outcomePrices") else None
            if yes_price is None:
                yes_price = book_analysis.get("midpoint", 0.5)

            no_price = 1.0 - yes_price if yes_price else 0.5

            entry = {
                "condition_id": m.get("conditionId") or m.get("condition_id", ""),
                "question": m.get("question", "Unknown"),
                "description": (m.get("description") or "")[:200],
                "category": m.get("category", ""),
                "tags": m.get("tags", []),
                "yes_price": round(yes_price, 4),
                "no_price": round(no_price, 4),
                "volume_24h": float(m.get("volume24hr", 0) or 0),
                "total_volume": float(m.get("volumeNum", 0) or m.get("volume", 0) or 0),
                "liquidity": float(m.get("liquidityNum", 0) or m.get("liquidity", 0) or 0),
                "end_date": m.get("endDate") or m.get("end_date_iso"),
                "days_to_resolution": days_until(m.get("endDate") or m.get("end_date_iso")),
                "spread": book_analysis.get("spread", 0),
                "bid_depth_usd": book_analysis.get("bid_depth_usd", 0),
                "ask_depth_usd": book_analysis.get("ask_depth_usd", 0),
                "total_depth_usd": book_analysis.get("total_depth_usd", 0),
                "tokens": tokens,
            }
            analyzed.append(entry)

        return analyzed

    def _score_markets(self, markets: list) -> list:
        """
        Score markets for trading interest.
        Higher score = more interesting opportunity.

        Scoring factors:
        - Liquidity (higher = better execution)
        - Spread (tighter = lower cost)
        - Volume (higher = more active)
        - Price range (markets near 50/50 have more uncertainty = more edge potential)
        - Days to resolution (sooner = less capital tie-up)
        """
        scored = []
        for m in markets:
            score = 0.0

            # Liquidity score (0-25 points)
            depth = m.get("total_depth_usd", 0)
            if depth > 5000:
                score += 25
            elif depth > 1000:
                score += 20
            elif depth > 500:
                score += 15
            elif depth > 100:
                score += 10
            else:
                score += 5

            # Spread score (0-25 points, tighter = better)
            spread = m.get("spread", 1)
            if spread <= 0.02:
                score += 25
            elif spread <= 0.05:
                score += 20
            elif spread <= 0.10:
                score += 15
            elif spread <= 0.15:
                score += 10
            else:
                score += 5

            # Volume score (0-20 points)
            vol = m.get("volume_24h", 0)
            if vol > 50000:
                score += 20
            elif vol > 10000:
                score += 16
            elif vol > 1000:
                score += 12
            elif vol > 100:
                score += 8
            else:
                score += 4

            # Price uncertainty score (0-20 points)
            # Markets near 50% have max uncertainty — more room for edge
            yes_price = m.get("yes_price", 0.5)
            distance_from_50 = abs(yes_price - 0.5)
            if distance_from_50 <= 0.10:
                score += 20  # Very uncertain, good for edge-finding
            elif distance_from_50 <= 0.20:
                score += 15
            elif distance_from_50 <= 0.30:
                score += 10
            else:
                score += 5  # Near-certain markets have less opportunity

            # Time horizon score (0-10 points, sooner = better for small bankroll)
            dtl = m.get("days_to_resolution")
            if dtl is not None:
                if dtl <= 7:
                    score += 10
                elif dtl <= 30:
                    score += 8
                elif dtl <= 60:
                    score += 6
                else:
                    score += 3

            m["score"] = round(score, 1)

            # Add Kelly sizing example (assuming 5% edge in our favor)
            # This is illustrative — real edge requires your probability estimate
            hypothetical_edge = 0.05
            if yes_price < 0.5:
                my_estimate = yes_price + hypothetical_edge
            else:
                my_estimate = yes_price - hypothetical_edge

            sizing = self.risk_mgr.kelly_size(
                my_prob=my_estimate,
                market_price=yes_price,
                confidence="Medium"
            )
            m["example_sizing"] = sizing

            scored.append(m)

        return scored

    def _print_results(self, markets: list):
        """Print formatted scanner results."""
        if not markets:
            print("No opportunities found matching your criteria.\n")
            return

        print(f"{'='*60}")
        print(f"  TOP OPPORTUNITIES")
        print(f"{'='*60}\n")

        for i, m in enumerate(markets[:15], 1):
            score = m.get("score", 0)
            q = m.get("question", "?")
            yes_p = m.get("yes_price", 0)
            spread = m.get("spread", 0)
            vol = m.get("volume_24h", 0)
            depth = m.get("total_depth_usd", 0)
            dtl = m.get("days_to_resolution")
            dtl_str = f"{dtl}d" if dtl is not None else "?"

            # Score indicator
            if score >= 80:
                grade = "A"
            elif score >= 65:
                grade = "B"
            elif score >= 50:
                grade = "C"
            else:
                grade = "D"

            sizing = m.get("example_sizing", {})
            size_usd = sizing.get("position_size_usd", 0)
            edge_c = sizing.get("edge_cents", 0)

            print(f"  #{i} [{grade}] {q}")
            print(f"      YES: ${yes_p:.2f} | Spread: {spread:.3f} | "
                  f"Vol 24h: ${vol:,.0f} | Depth: ${depth:,.0f} | Res: {dtl_str}")
            if size_usd > 0:
                print(f"      Example (5c edge): ${size_usd:.0f} position | "
                      f"Side: {sizing.get('side', '?')}")
            print()

        print(f"{'─'*60}")
        summary = self.risk_mgr.portfolio_summary()
        print(f"  Portfolio: ${summary['bankroll']:,.0f} bankroll | "
              f"${summary['cash_available']:,.0f} available | "
              f"{summary['num_positions']} positions")
        print(f"  Mode: {'DEFENSIVE' if summary['defensive_mode'] else 'NORMAL'} | "
              f"Drawdown: {summary['drawdown_from_peak']:.1f}%")
        print(f"{'─'*60}\n")

        print("Next steps:")
        print("  1. Review markets above and form probability estimates")
        print("  2. Run: python trade_signal.py <condition_id> <your_probability>")
        print("  3. Review the structured trade signal before executing")
        print()


def main():
    parser = argparse.ArgumentParser(description="Polymarket Market Scanner")
    parser.add_argument("--top", type=int, default=15, help="Show top N opportunities")
    parser.add_argument("--category", type=str, default="", help="Filter by category")
    parser.add_argument("--max-markets", type=int, default=200, help="Max markets to fetch")
    args = parser.parse_args()

    scanner = MarketScanner()
    results = scanner.scan(max_markets=args.max_markets, category_filter=args.category)

    # Save results to JSON for further analysis
    if results:
        output_path = "scan_results.json"
        with open(output_path, "w") as f:
            # Remove non-serializable parts
            clean = []
            for r in results[:30]:
                entry = {k: v for k, v in r.items() if k != "example_sizing"}
                entry["example_sizing_summary"] = {
                    "side": r.get("example_sizing", {}).get("side", ""),
                    "position_size_usd": r.get("example_sizing", {}).get("position_size_usd", 0),
                    "edge_cents": r.get("example_sizing", {}).get("edge_cents", 0),
                }
                clean.append(entry)
            json.dump(clean, f, indent=2, default=str)
        print(f"Results saved to {output_path}")


if __name__ == "__main__":
    main()
