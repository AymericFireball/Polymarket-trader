"""
Trade Journal & Position Tracker
==================================
Tracks all positions, generates P&L reports, and handles post-mortems.

From the brief:
  - Maintain real-time position ledger
  - Daily P&L reporting
  - Post-mortem on every closed trade
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import get_conn, init_db
from config import BANKROLL


# ─── Position Ledger ──────────────────────────────────────────────

def get_open_positions() -> List[Dict]:
    """Get all open (non-closed) positions."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT t.*, m.yes_price as live_price, m.volume_24h, m.end_date
        FROM trades t
        LEFT JOIN markets m ON t.condition_id = m.condition_id
        WHERE t.status NOT IN ('closed', 'cancelled')
        ORDER BY t.opened_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_closed_positions() -> List[Dict]:
    """Get all closed positions."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT * FROM trades
        WHERE status = 'closed'
        ORDER BY closed_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def close_position(trade_id: str, exit_price: float,
                   reason: str = "manual") -> Dict:
    """
    Close a position and record the result.

    Args:
        trade_id: The trade ID to close
        exit_price: Price at which we're exiting
        reason: Why we're closing (stop_loss, take_profit, thesis_invalidated, manual, resolved)
    """
    conn = get_conn()
    trade = conn.execute(
        "SELECT * FROM trades WHERE trade_id=?", (trade_id,)
    ).fetchone()

    if not trade:
        conn.close()
        return {"error": f"Trade {trade_id} not found"}

    trade = dict(trade)
    entry = trade.get("entry_price", 0)
    qty = trade.get("quantity", 0)
    side = trade.get("side", "")
    cost = trade.get("cost_basis", 0)

    # Calculate realized P&L
    if "YES" in side.upper():
        pnl = (exit_price - entry) * qty
    else:
        pnl = (entry - exit_price) * qty

    pnl = round(pnl, 2)

    conn.execute("""
        UPDATE trades SET
            status = 'closed',
            exit_price = ?,
            exit_quantity = ?,
            realized_pnl = ?,
            closed_at = ?,
            close_reason = ?
        WHERE trade_id = ?
    """, (
        exit_price, qty, pnl,
        datetime.now(timezone.utc).isoformat(),
        reason, trade_id,
    ))
    conn.commit()
    conn.close()

    return {
        "trade_id": trade_id,
        "entry": entry,
        "exit": exit_price,
        "quantity": qty,
        "realized_pnl": pnl,
        "reason": reason,
    }


def update_position_prices() -> int:
    """Update unrealized P&L for all open positions using current market prices."""
    conn = get_conn()
    positions = conn.execute("""
        SELECT t.trade_id, t.entry_price, t.quantity, t.side, m.yes_price
        FROM trades t
        JOIN markets m ON t.condition_id = m.condition_id
        WHERE t.status NOT IN ('closed', 'cancelled')
        AND m.yes_price IS NOT NULL
    """).fetchall()

    updated = 0
    for pos in positions:
        entry = pos["entry_price"] or 0
        current = pos["yes_price"] or 0
        qty = pos["quantity"] or 0
        side = pos["side"] or ""

        if "YES" in side.upper():
            pnl = (current - entry) * qty
        else:
            pnl = (entry - current) * qty

        conn.execute("""
            UPDATE trades SET current_price=?, unrealized_pnl=?
            WHERE trade_id=?
        """, (current, round(pnl, 2), pos["trade_id"]))
        updated += 1

    conn.commit()
    conn.close()
    return updated


# ─── P&L Reporting ────────────────────────────────────────────────

def daily_pnl_report() -> str:
    """Generate the daily P&L report per the brief's format."""
    init_db()
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # Update prices first
    update_position_prices()

    # Get positions
    open_pos = get_open_positions()
    closed_today = get_closed_positions()
    closed_today = [p for p in closed_today
                    if p.get("closed_at", "").startswith(today_str)]

    # Portfolio calculations
    total_deployed = sum(p.get("cost_basis", 0) or 0 for p in open_pos)
    total_unrealized = sum(p.get("unrealized_pnl", 0) or 0 for p in open_pos)
    total_realized_today = sum(p.get("realized_pnl", 0) or 0 for p in closed_today)
    cash = BANKROLL - total_deployed
    cash_pct = cash / BANKROLL if BANKROLL > 0 else 0

    lines = [
        f"DAILY REPORT — {today_str}",
        f"{'='*60}",
        "",
        "PORTFOLIO SUMMARY:",
        f"  Total bankroll: ${BANKROLL:,.2f}",
        f"  Deployed capital: ${total_deployed:,.2f} ({total_deployed/BANKROLL:.0%})" if BANKROLL else "",
        f"  Cash reserve: ${cash:,.2f} ({cash_pct:.0%})",
        f"  Unrealized P&L: ${total_unrealized:+,.2f}",
        f"  Realized today: ${total_realized_today:+,.2f}",
        "",
    ]

    # Position details
    if open_pos:
        lines.append(f"OPEN POSITIONS ({len(open_pos)}):")
        lines.append(f"  {'Market':<40s} | {'Side':>8s} | {'Entry':>6s} | {'Now':>6s} | {'P&L':>8s} | {'Cost':>7s}")
        lines.append(f"  {'-'*40}-+-{'-'*8}-+-{'-'*6}-+-{'-'*6}-+-{'-'*8}-+-{'-'*7}")
        for p in open_pos:
            market = (p.get("question") or p.get("thesis", "?"))[:40]
            side = p.get("side", "?")
            entry = p.get("entry_price", 0) or 0
            current = p.get("live_price") or p.get("current_price", 0) or 0
            pnl = p.get("unrealized_pnl", 0) or 0
            cost = p.get("cost_basis", 0) or 0
            lines.append(
                f"  {market:<40s} | {side:>8s} | {entry:6.3f} | {current:6.3f} | "
                f"${pnl:>+7.2f} | ${cost:>6.2f}"
            )
        lines.append("")

    # Closed trades today
    if closed_today:
        lines.append(f"CLOSED TODAY ({len(closed_today)}):")
        for p in closed_today:
            market = (p.get("question") or "?")[:50]
            pnl = p.get("realized_pnl", 0) or 0
            reason = p.get("close_reason", "?")
            lines.append(f"  {market}: ${pnl:+.2f} ({reason})")
        lines.append("")

    # Risk flags
    flags = []
    if cash_pct < 0.30:
        flags.append(f"Cash reserve {cash_pct:.0%} < 30% minimum")
    if total_deployed / BANKROLL > 0.70:
        flags.append(f"Deployed {total_deployed/BANKROLL:.0%} > 70% threshold")
    # Check for correlated positions (same market_type)
    types = {}
    conn = get_conn()
    for p in open_pos:
        mkt = conn.execute(
            "SELECT market_type FROM markets WHERE condition_id=?",
            (p.get("condition_id", ""),)
        ).fetchone()
        mtype = dict(mkt)["market_type"] if mkt else "other"
        types[mtype] = types.get(mtype, 0) + (p.get("cost_basis", 0) or 0)
    conn.close()
    for mtype, exposure in types.items():
        if exposure / BANKROLL > 0.25:
            flags.append(f"Concentration: {mtype} exposure ${exposure:.0f} ({exposure/BANKROLL:.0%})")

    if flags:
        lines.append("RISK FLAGS:")
        for f in flags:
            lines.append(f"  !! {f}")
    else:
        lines.append("RISK FLAGS: None")

    return "\n".join(lines)


# ─── Post-Mortem ──────────────────────────────────────────────────

def post_mortem(trade_id: str) -> str:
    """
    Generate a post-mortem analysis for a closed trade.
    Per the brief's format.
    """
    conn = get_conn()
    trade = conn.execute("SELECT * FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
    conn.close()

    if not trade:
        return f"Trade {trade_id} not found"

    t = dict(trade)
    entry = t.get("entry_price", 0) or 0
    exit_p = t.get("exit_price", 0) or 0
    pnl = t.get("realized_pnl", 0) or 0
    cost = t.get("cost_basis", 0) or 0
    pnl_pct = (pnl / cost * 100) if cost > 0 else 0

    win = pnl > 0
    result_str = "Win" if win else "Loss"

    return f"""
POST-MORTEM
{'='*50}
  Market: {t.get('question', '?')}
  Result: {result_str}
  Side: {t.get('side', '?')}
  Entry: ${entry:.4f} -> Exit: ${exit_p:.4f}
  Cost basis: ${cost:.2f}
  Realized P&L: ${pnl:+.2f} ({pnl_pct:+.1f}%)
  Close reason: {t.get('close_reason', '?')}
  Duration: {t.get('opened_at', '?')} to {t.get('closed_at', '?')}
  Thesis: {t.get('thesis', 'Not recorded')}
  Kelly fraction: {t.get('kelly_fraction', 0):.1%}

  ASSESSMENT:
  - Was the thesis correct? [Review needed]
  - Was the sizing appropriate? {'Yes' if (cost/BANKROLL) <= 0.10 else 'Oversized'} ({cost/BANKROLL:.1%} of bankroll)
  - What would I do differently? [Add notes]
"""


def all_post_mortems() -> str:
    """Generate post-mortems for all closed trades."""
    closed = get_closed_positions()
    if not closed:
        return "No closed trades to review."

    total_pnl = sum(p.get("realized_pnl", 0) or 0 for p in closed)
    wins = sum(1 for p in closed if (p.get("realized_pnl", 0) or 0) > 0)
    losses = len(closed) - wins

    lines = [
        f"TRADE REVIEW — {len(closed)} trades",
        f"{'='*50}",
        f"  Wins: {wins} | Losses: {losses} | Win rate: {wins/len(closed):.0%}",
        f"  Total P&L: ${total_pnl:+.2f}",
        "",
    ]

    for p in closed:
        lines.append(post_mortem(p.get("trade_id", "")))

    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trade Journal")
    parser.add_argument("--positions", action="store_true", help="Show open positions")
    parser.add_argument("--daily", action="store_true", help="Daily P&L report")
    parser.add_argument("--close", type=str, help="Close a position (trade_id)")
    parser.add_argument("--exit-price", type=float, help="Exit price for closing")
    parser.add_argument("--reason", type=str, default="manual", help="Close reason")
    parser.add_argument("--review", action="store_true", help="All post-mortems")
    args = parser.parse_args()

    init_db()

    if args.daily:
        print(daily_pnl_report())
    elif args.positions:
        positions = get_open_positions()
        if positions:
            for p in positions:
                market = p.get("question", p.get("thesis", "?"))[:50]
                pnl = p.get("unrealized_pnl", 0) or 0
                print(f"  {p.get('trade_id', '?'):8s} | {p.get('side', '?'):8s} | "
                      f"entry={p.get('entry_price', 0):.3f} | "
                      f"P&L=${pnl:+.2f} | {market}")
        else:
            print("No open positions.")
    elif args.close and args.exit_price is not None:
        result = close_position(args.close, args.exit_price, args.reason)
        print(json.dumps(result, indent=2))
    elif args.review:
        print(all_post_mortems())
    else:
        parser.print_help()
