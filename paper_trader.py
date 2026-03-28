"""
Paper Trader
============
Simulates the full trading pipeline without placing real orders.
Trades are saved to the trades table with is_paper=1 so they can be
tracked and reported separately from live trades.

Usage:
    python paper_trader.py --top 20
    python run.py paper-trade --top 20
    python run.py paper-report
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timezone
from typing import List, Dict, Optional

from db import init_db, get_conn
from config import BANKROLL
from pipeline import Pipeline, format_trade_signal


# ─── Core session ─────────────────────────────────────────────────────────────

def run_paper_session(top_n: int = 20, verbose: bool = False) -> Dict:
    """
    Scan top_n markets through the full pipeline and paper-record every
    trade that would pass the DecisionGate.

    Returns a summary dict with keys: scanned, signals, recorded.
    """
    init_db()
    conn = get_conn()

    markets = conn.execute("""
        SELECT * FROM markets
        WHERE resolved=0 AND yes_price IS NOT NULL
          AND yes_price > 0.03 AND yes_price < 0.97
        ORDER BY volume_24h DESC
        LIMIT ?
    """, (top_n,)).fetchall()

    pipeline = Pipeline()
    scanned = 0
    signals = 0
    recorded = 0

    for row in markets:
        market = dict(row)
        scanned += 1
        try:
            result = pipeline.analyze_market(market)
        except Exception as e:
            if verbose:
                print(f"  ERROR analyzing {market['question'][:50]}: {e}")
            continue

        decision = result.get("decision", {})
        if not decision.get("pass"):
            if verbose:
                edge = decision.get("edge_cents", 0)
                print(f"  SKIP  | {edge:3d}c | {market['question'][:55]}")
            continue

        signals += 1
        if verbose:
            print(format_trade_signal(result))

        # Record the paper trade
        try:
            _record_paper_trade(conn, market, result)
            recorded += 1
        except Exception as e:
            if verbose:
                print(f"  WARN: could not record paper trade: {e}")

    conn.commit()
    conn.close()

    return {"scanned": scanned, "signals": signals, "recorded": recorded}


def _record_paper_trade(conn, market: Dict, result: Dict) -> None:
    """Insert a paper trade row into the trades table."""
    decision = result.get("decision", {})
    side = decision.get("side", "YES")
    price = float(market.get("yes_price" if side == "YES" else "no_price") or 0.5)
    edge_cents = decision.get("edge_cents", 0)

    # Half-Kelly sizing capped at 5% of bankroll for paper trades
    max_size = BANKROLL * 0.05
    size = min(max_size, max(1.0, edge_cents * 0.5))

    now = datetime.now(timezone.utc).isoformat()
    trade_id = f"PT-{now[:10].replace('-','')}-{market['condition_id'][:8]}"

    conn.execute("""
        INSERT OR IGNORE INTO trades
            (trade_id, condition_id, question, side, entry_price, quantity, cost_basis,
             status, thesis, invalidation, stop_loss, take_profit,
             opened_at, is_paper)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, 1)
    """, (
        trade_id,
        market["condition_id"],
        market.get("question", ""),
        f"BUY {side}",
        price,
        round(size / price, 4) if price > 0 else 0,
        round(size, 4),
        result.get("decision", {}).get("thesis", "Paper trade via pipeline"),
        "Paper trade — no real invalidation criteria",
        round(max(0.01, price - 0.10), 4),
        round(min(0.99, price + 0.15), 4),
        now,
    ))



# ─── Reporting ────────────────────────────────────────────────────────────────

def get_paper_trades(conn, open_only: bool = False) -> List[Dict]:
    """Fetch paper trades from the DB."""
    where = "is_paper=1"
    if open_only:
        where += " AND status='open'"
    rows = conn.execute(f"""
        SELECT t.*, m.question, m.yes_price AS current_price_yes,
               m.no_price AS current_price_no, m.resolved, m.resolution
        FROM trades t
        LEFT JOIN markets m ON t.condition_id = m.condition_id
        WHERE {where}
        ORDER BY t.opened_at DESC
    """).fetchall()
    return [dict(r) for r in rows]


def paper_report(verbose: bool = False) -> str:
    """Generate a paper-trading performance report."""
    init_db()
    conn = get_conn()
    trades = get_paper_trades(conn)
    conn.close()

    if not trades:
        return "No paper trades recorded yet.\n  Run: python run.py paper-trade"

    lines = []
    lines.append("=" * 65)
    lines.append("PAPER TRADING REPORT")
    lines.append("=" * 65)

    open_trades = [t for t in trades if t["status"] == "open"]
    closed_trades = [t for t in trades if t["status"] != "open"]

    lines.append(f"  Total paper trades : {len(trades)}")
    lines.append(f"  Open               : {len(open_trades)}")
    lines.append(f"  Closed             : {len(closed_trades)}")
    lines.append("")

    # Compute unrealized P&L on open positions
    total_cost = 0.0
    total_value = 0.0
    for t in open_trades:
        cost = float(t.get("cost_basis") or 0)
        side = str(t.get("side") or "BUY YES")
        if "YES" in side:
            cur = float(t.get("current_price_yes") or t.get("entry_price") or 0)
        else:
            cur = float(t.get("current_price_no") or t.get("entry_price") or 0)
        qty = float(t.get("quantity") or 0)
        total_cost += cost
        total_value += cur * qty

    unrealized_pnl = total_value - total_cost
    lines.append(f"  Deployed capital   : ${total_cost:.2f}")
    lines.append(f"  Unrealized P&L     : ${unrealized_pnl:+.2f}")

    # Closed trade P&L
    if closed_trades:
        closed_pnl = sum(
            float(t.get("realized_pnl") or 0) for t in closed_trades
        )
        wins = sum(1 for t in closed_trades if float(t.get("realized_pnl") or 0) > 0)
        lines.append(f"  Realized P&L       : ${closed_pnl:+.2f}")
        lines.append(f"  Win rate           : {wins}/{len(closed_trades)} "
                     f"({wins/len(closed_trades):.0%})")

    if verbose and open_trades:
        lines.append("")
        lines.append(f"  {'ID':>18s} | {'Side':8s} | {'Entry':>6s} | {'Current':>7s} | {'PnL':>8s} | Question")
        lines.append("  " + "-" * 63)
        for t in open_trades[:20]:
            side = str(t.get("side") or "?")
            entry = float(t.get("entry_price") or 0)
            if "YES" in side:
                cur = float(t.get("current_price_yes") or entry)
            else:
                cur = float(t.get("current_price_no") or entry)
            qty = float(t.get("quantity") or 0)
            pnl = (cur - entry) * qty
            q = str(t.get("question") or t.get("condition_id") or "")[:35]
            lines.append(f"  {t['trade_id']:>18s} | {side:8s} | ${entry:5.3f} | ${cur:6.4f} | ${pnl:+7.2f} | {q}")

    lines.append("")
    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Paper trading session")
    p.add_argument("--top",     type=int, default=20, help="Markets to scan (default: 20)")
    p.add_argument("--verbose", action="store_true",  help="Print per-market details")
    p.add_argument("--report",  action="store_true",  help="Show report instead of running session")
    args = p.parse_args()

    if args.report:
        print(paper_report(verbose=args.verbose))
    else:
        print(f"Running paper session (top {args.top} markets)...")
        stats = run_paper_session(top_n=args.top, verbose=args.verbose)
        print(f"\nScanned: {stats['scanned']}  |  Signals: {stats['signals']}  |  Recorded: {stats['recorded']}")
        print()
        print(paper_report(verbose=args.verbose))
