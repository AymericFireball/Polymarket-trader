#!/usr/bin/env python3
"""
Polymarket Trading Agent — Main Runner
========================================
Central CLI for daily operations. Ties all pipeline stages together.

Usage:
  python run.py status                         System status overview
  python run.py fetch [--pages N] [--limit N]  Fetch live market data from Gamma API
  python run.py scan [--top N]                 Scan top N markets & generate signals
  python run.py analyze <cid|keyword> [-p 0.7] Deep analysis on a specific market
  python run.py execute <cid> <side> <size>    Execute a trade (dry-run by default)
  python run.py monitor                        Check open positions for stop/take triggers
  python run.py positions                      List open positions with unrealized P&L
  python run.py daily                          Daily P&L report
  python run.py post-mortem <trade_id>         Post-mortem analysis of a closed trade
  python run.py portfolio                      Portfolio summary
  python run.py calibration                    Calibration status & stats
  python run.py seed                           Bootstrap calibration with synthetic data
  python run.py import-resolved [--csv file]   Import resolved markets for calibration
  python run.py import <file>                  Import market data from JSON
  python run.py backtest [--limit N]           Backtest fusion engine on resolved markets
  python run.py tune-weights                   Update signal weights from accuracy history
  python run.py paper-trade [--top N]          Paper-trade session (no real orders)
  python run.py paper-report                   Report on paper-trade performance
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import init_db, get_conn, db_stats
from config import (
    BANKROLL, NEWSAPI_KEY, BLACKLIST_KEYWORDS,
    TIME_HORIZON_EDGE, TIME_HORIZON_CAPITAL_PCT,
)
try:
    from config import MIROFISH_API_URL
except ImportError:
    MIROFISH_API_URL = ""
try:
    from config import MIROFISH_PATH
except ImportError:
    MIROFISH_PATH = ""
from pipeline import Pipeline, format_trade_signal, format_portfolio_report
from calibration import calibrate_probability, compute_calibration_stats, get_calibration_data
from risk_manager import RiskManager, PortfolioState
from scraper import normalize_gamma_market


def cmd_status(args):
    """System status overview."""
    init_db()
    conn = get_conn()
    try:
        stats = db_stats(conn)
    except Exception:
        stats = {}

    print("=" * 60)
    print("POLYMARKET TRADING AGENT — STATUS")
    print("=" * 60)
    print(f"  Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Bankroll: ${BANKROLL:.2f}")
    print()

    # DB stats
    active = conn.execute("SELECT COUNT(*) FROM markets WHERE resolved=0").fetchone()[0]
    resolved = conn.execute("SELECT COUNT(*) FROM markets WHERE resolved=1").fetchone()[0]
    predictions = conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
    print(f"  Markets: {active} active, {resolved} resolved")
    print(f"  Predictions: {predictions}")
    print()

    # Module status
    print("  MODULE STATUS:")
    print(f"    NewsAPI:    {'Configured' if NEWSAPI_KEY else 'Not set (set NEWSAPI_KEY in config.py)'}")
    mirofish_ok = MIROFISH_API_URL or MIROFISH_PATH
    print(f"    MiroFish:   {'Configured (' + (MIROFISH_API_URL or MIROFISH_PATH) + ')' if mirofish_ok else 'Not set (set MIROFISH_API_URL in config.py)'}")

    # Calibration
    cal_data = get_calibration_data()
    if cal_data:
        cal_stats = compute_calibration_stats(cal_data)
        print(f"    Calibration: {cal_stats['num_predictions']} resolved, Brier={cal_stats['avg_brier']:.4f}")
    else:
        print(f"    Calibration: No resolved predictions yet (shrinkage active)")

    # Top markets by volume
    top = conn.execute("""
        SELECT question, yes_price, volume_24h, market_type
        FROM markets WHERE resolved=0
        ORDER BY volume_24h DESC LIMIT 5
    """).fetchall()
    conn.close()

    if top:
        print()
        print("  TOP 5 MARKETS BY 24H VOLUME:")
        for m in top:
            price = f"${m['yes_price']:.2f}" if m['yes_price'] else "?"
            vol = f"${m['volume_24h']/1000:.0f}k" if m['volume_24h'] else "?"
            cat = f"[{m['market_type']}]" if m['market_type'] else ""
            print(f"    {price} | {vol} | {m['question'][:55]} {cat}")

    print()


def cmd_scan(args):
    """Scan markets and generate trade signals."""
    init_db()
    pipeline = Pipeline()

    conn = get_conn()
    # Get top markets by volume that have real prices
    markets = conn.execute("""
        SELECT * FROM markets
        WHERE resolved=0 AND yes_price IS NOT NULL AND yes_price > 0.03 AND yes_price < 0.97
        ORDER BY volume_24h DESC
        LIMIT ?
    """, (args.top,)).fetchall()
    conn.close()

    if not markets:
        print("No tradeable markets found. Import market data first:")
        print("  python run.py import <file.json>")
        return

    # Apply blacklist filter
    filtered = []
    rejected = 0
    for row in markets:
        q = (row["question"] or "").lower()
        if any(kw in q for kw in BLACKLIST_KEYWORDS):
            rejected += 1
            continue
        filtered.append(row)
    markets = filtered

    if rejected:
        print(f"  Filtered out {rejected} blacklisted market(s)")

    print(f"Scanning top {len(markets)} markets...\n")

    results = []
    for row in markets:
        market = dict(row)

        # Tag time horizon for edge threshold
        from scanner import MarketScanner
        dtl = MarketScanner.classify_time_horizon(
            (datetime.fromisoformat(market["end_date"].replace("Z", "+00:00")) - datetime.now(timezone.utc)).days
            if market.get("end_date") else None
        )
        market["_time_horizon"] = dtl
        market["_min_edge_cents"] = TIME_HORIZON_EDGE.get(dtl, 5)

        try:
            # Use market price as base, let signals adjust
            result = pipeline.analyze_market(market)
            results.append(result)

            d = result.get("decision", {})
            status = "TRADE" if d.get("pass") else "PASS "
            edge = d.get("edge_cents", 0)
            side = d.get("side", "?")
            cal = result.get("calibrated_probability", 0)
            price = result.get("market_price", 0)

            print(f"  {status} | {edge:3d}c {side:3s} | mkt={price:.2f} est={cal:.2f} | {market['question'][:50]}")
        except Exception as e:
            print(f"  ERROR | {market['question'][:50]} | {e}")

    # Summary
    trades = [r for r in results if r.get("decision", {}).get("pass")]
    print(f"\n{'='*60}")
    print(f"SCAN COMPLETE: {len(trades)} actionable trades out of {len(results)} analyzed")

    if trades:
        print(f"\nACTIONABLE SIGNALS:\n")
        for r in trades:
            print(format_trade_signal(r))

    if args.json:
        outfile = args.json
        with open(outfile, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nFull results saved to {outfile}")


def cmd_analyze(args):
    """Deep analysis on a specific market."""
    init_db()
    pipeline = Pipeline()

    conn = get_conn()
    # Search by condition_id or question substring
    market = conn.execute(
        "SELECT * FROM markets WHERE condition_id=?", (args.market,)
    ).fetchone()

    if not market:
        # Try searching by question
        market = conn.execute(
            "SELECT * FROM markets WHERE question LIKE ? AND resolved=0 LIMIT 1",
            (f"%{args.market}%",)
        ).fetchone()

    conn.close()

    if not market:
        print(f"Market not found: {args.market}")
        print("Try searching with a keyword from the question.")
        return

    market = dict(market)
    print(f"ANALYZING: {market['question']}")
    print(f"  Condition ID: {market['condition_id']}")
    print(f"  Current price: YES={market.get('yes_price', '?')}")
    print(f"  24h volume: ${market.get('volume_24h', 0):,.0f}")
    print(f"  Liquidity: ${market.get('liquidity', 0):,.0f}")
    print(f"  End date: {market.get('end_date', 'Unknown')}")
    print(f"  Type: {market.get('market_type', 'Unknown')}")
    print()

    if args.prob:
        result = pipeline.analyze_market(
            market,
            my_probability=args.prob,
            run_mirofish=args.mirofish
        )
    else:
        result = pipeline.analyze_market(
            market,
            run_mirofish=args.mirofish
        )

    # Display stages
    stages = result.get("stages", {})
    if "signals" in stages:
        print("SIGNALS:")
        for name, info in stages["signals"].items():
            print(f"  {name}: score={info.get('score', '?')}")

    if "preprocessing" in stages:
        pp = stages["preprocessing"]
        print(f"\nPREPROCESSING:")
        print(f"  Aggregate: {pp.get('aggregate_score', '?')}")
        print(f"  Confidence: {pp.get('confidence', '?')}")
        if pp.get("contradictions"):
            print(f"  Contradictions: {pp['contradictions']}")

    cal = stages.get("calibration", {})
    print(f"\nCALIBRATION:")
    print(f"  Raw: {result.get('raw_probability', '?')}")
    print(f"  Calibrated: {result.get('calibrated_probability', '?')}")
    for adj in cal.get("adjustments", []):
        print(f"  {adj['type']}: {adj['before']:.4f} -> {adj['after']:.4f}")

    # Decision
    d = result.get("decision", {})
    print(f"\nDECISION: {'TRADE' if d.get('pass') else 'PASS'}")
    print(f"  Edge: {d.get('edge_cents', 0)}c | Side: {d.get('side', '?')}")
    for reason in d.get("reasons", []):
        print(f"  - {reason}")

    if d.get("trade_signal"):
        print(f"\n{format_trade_signal(result)}")


def cmd_execute(args):
    """Execute a trade (dry-run by default, use --live for real execution)."""
    init_db()
    from executor import TradeExecutor

    conn = get_conn()
    market = conn.execute(
        "SELECT * FROM markets WHERE condition_id=?",
        (args.market,)
    ).fetchone()

    if not market:
        market = conn.execute(
            "SELECT * FROM markets WHERE question LIKE ? AND resolved=0 LIMIT 1",
            (f"%{args.market}%",)
        ).fetchone()

    conn.close()

    if not market:
        print(f"Market not found: {args.market}")
        return

    market = dict(market)
    side = args.side.upper()
    if side not in ("YES", "NO"):
        print(f"Side must be YES or NO, got: {args.side}")
        return

    size = float(args.size)
    live = getattr(args, "live", False)

    print(f"{'LIVE' if live else 'DRY-RUN'} EXECUTION")
    print(f"  Market : {market['question'][:70]}")
    print(f"  Side   : {side}")
    print(f"  Size   : ${size:.2f}")
    print(f"  Price  : ${market.get('yes_price', 0):.4f} YES / ${market.get('no_price', 0):.4f} NO")

    if not live:
        print(f"\n  [DRY-RUN] Order would be placed for {size:.2f} USDC of {side}")
        print(f"  Run with --live to execute for real")
        return

    executor = TradeExecutor(dry_run=not live)
    thesis = getattr(args, "thesis", "Manual execution via CLI")
    price = market.get("yes_price" if side == "YES" else "no_price") or 0.5
    signal = {
        "market": market["question"],
        "condition_id": market["condition_id"],
        "side": f"BUY {side}",
        "entry_target": price,
        "position_size": size,
        "position_pct": size / BANKROLL if BANKROLL else 0,
        "stop_loss": round(max(0.01, price - 0.10), 4),
        "take_profit": round(min(0.99, price + 0.15), 4),
        "order_type": "GTC",
        "edge_cents": 0,
        "thesis": thesis,
        "invalidation": "Manual trade — set your own invalidation criteria",
    }
    result = executor.execute_signal(signal)

    if result.get("status") in ("executed", "partial"):
        orders = result.get("orders", [])
        print(f"\n  Trade status: {result['status']}")
        for o in orders:
            print(f"    order_id={o.get('order_id')} price=${o.get('price', 0):.4f} size=${o.get('size_usd', 0):.2f}")
    else:
        errors = result.get("errors", [])
        print(f"\n  Execution failed: {'; '.join(errors) if errors else result.get('status')}")


def cmd_monitor(args):
    """Check open positions for stop-loss and take-profit triggers."""
    init_db()
    from executor import PositionMonitor

    monitor = PositionMonitor()
    alerts = monitor.check_positions()

    if not alerts:
        print("No position alerts. All positions within thresholds.")
        return

    print(f"POSITION ALERTS ({len(alerts)} found):")
    print()
    for a in alerts:
        marker = "🔴 STOP-LOSS" if a["type"] == "stop_loss" else "🟢 TAKE-PROFIT"
        print(f"  {marker}")
        print(f"    Trade ID   : {a['trade_id']}")
        print(f"    Market     : {a['question'][:60]}")
        print(f"    Side       : {a['side']}")
        print(f"    Entry      : ${a['entry_price']:.4f}")
        print(f"    Current    : ${a['current_price']:.4f}")
        print(f"    Move       : {a['move_cents']:+d}c")
        print(f"    Recommended: {a['action']}")
        print()


def cmd_positions(args):
    """List open positions with unrealized P&L."""
    init_db()
    from journal import get_open_positions, update_position_prices

    # Refresh prices first
    try:
        update_position_prices()
    except Exception as e:
        pass  # Non-critical if price update fails

    positions = get_open_positions()

    if not positions:
        print("No open positions.")
        print("  Run 'python run.py scan' to find trade signals.")
        return

    total_cost = 0
    total_value = 0

    print("=" * 70)
    print("OPEN POSITIONS")
    print("=" * 70)
    print(f"  {'ID':8s} | {'Side':3s} | {'Entry':>6s} | {'Current':>7s} | {'Qty':>8s} | {'Cost':>8s} | {'PnL':>8s} | Market")
    print("  " + "-" * 68)

    for p in positions:
        pnl = p.get("unrealized_pnl") or 0
        cost = p.get("cost_basis") or 0
        cur = p.get("current_price") or p.get("entry_price") or 0
        total_cost += cost
        total_value += cost + pnl
        pnl_str = f"${pnl:+.2f}"
        print(f"  {p['trade_id']:8s} | {p['side']:3s} | ${p['entry_price']:5.3f} | ${cur:6.4f} | "
              f"{p['quantity']:8.2f} | ${cost:7.2f} | {pnl_str:>8s} | {(p['question'] or '')[:30]}")

    print("  " + "-" * 68)
    total_pnl = total_value - total_cost
    print(f"  {'TOTAL':8s}   {'':3s}   {'':6s}   {'':7s}   {'':8s}   ${total_cost:7.2f} | ${total_pnl:+7.2f}")
    print()
    cash = BANKROLL - total_cost
    print(f"  Bankroll: ${BANKROLL:.2f}  |  Deployed: ${total_cost:.2f} ({total_cost/BANKROLL*100:.0f}%)  |  Cash: ${cash:.2f}")


def cmd_daily(args):
    """Daily P&L report."""
    init_db()
    from journal import daily_pnl_report
    report = daily_pnl_report()
    print(report)


def cmd_post_mortem(args):
    """Post-mortem analysis of a closed trade."""
    init_db()
    from journal import post_mortem
    report = post_mortem(args.trade_id)
    print(report)


def cmd_seed(args):
    """Bootstrap calibration database with synthetic data."""
    from seed_calibration import seed_resolutions, print_calibration_baseline
    conn = get_conn()

    if args.clear:
        deleted = conn.execute(
            "DELETE FROM resolutions WHERE question LIKE '[SEED]%'"
        ).rowcount
        conn.execute("DELETE FROM markets WHERE question LIKE '[SEED]%'")
        conn.commit()
        print(f"Cleared {deleted} seeded resolutions")

    existing = conn.execute(
        "SELECT COUNT(*) FROM resolutions WHERE question LIKE '[SEED]%'"
    ).fetchone()[0]

    if existing > 0 and not args.clear:
        print(f"Already seeded with {existing} resolutions. Use --clear to reset.")
    else:
        n = getattr(args, "n", 15)
        stats = seed_resolutions(n_per_category=n, conn=conn)
        print(f"Inserted {stats['inserted']} synthetic resolutions")

    print_calibration_baseline(conn)
    conn.close()


def cmd_import_resolved(args):
    """Import resolved markets CSV for calibration."""
    from import_resolved import import_csv, import_rows, print_summary

    csv_path = getattr(args, "csv", None)
    if not csv_path:
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolved_markets_400.csv"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "resolved_markets.csv"),
            os.path.join(os.path.expanduser("~"), "Downloads", "resolved_markets_400.csv"),
            os.path.join(os.path.expanduser("~"), "Downloads", "resolved_markets.csv"),
        ]
        for c in candidates:
            if os.path.exists(c):
                csv_path = c
                print(f"Auto-found: {csv_path}")
                break

    if not csv_path or not os.path.exists(csv_path):
        print("No resolved markets CSV found.")
        print("Usage: python run.py import-resolved --csv /path/to/resolved_markets_400.csv")
        print()
        print("To generate the CSV:")
        print("  1. In your Chrome browser, the file was auto-downloaded as 'resolved_markets_400.csv'")
        print("  2. Move it to the Polymarket Trader project folder")
        print("  3. Run: python run.py import-resolved")
        return

    conn = get_conn()
    rows = import_csv(csv_path)
    print(f"Parsed {len(rows)} rows from {csv_path}")
    skip_sports = getattr(args, "skip_sports", False)
    stats = import_rows(rows, conn, skip_sports=skip_sports)
    print_summary(stats, conn)
    conn.close()


def cmd_portfolio(args):
    """Portfolio report."""
    init_db()
    from journal import daily_pnl_report
    conn = get_conn()
    open_trades = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE status='open'"
    ).fetchone()[0]
    resolved_count = conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
    conn.close()

    state = PortfolioState(bankroll=BANKROLL, peak_bankroll=BANKROLL)
    rm = RiskManager(state)
    print(format_portfolio_report([], rm))
    print(f"  Open trades tracked: {open_trades}")
    print(f"  Resolved predictions: {resolved_count}")


def cmd_calibration(args):
    """Calibration status and stats."""
    init_db()
    cal_data = get_calibration_data()
    stats = compute_calibration_stats(cal_data)

    print("=" * 60)
    print("CALIBRATION STATUS")
    print("=" * 60)
    print(f"  Resolved predictions: {stats['num_predictions']}")

    if stats["avg_brier"] is not None:
        print(f"  Average Brier: {stats['avg_brier']:.4f} (baseline: 0.25)")
        print(f"  Skill score: {stats['skill_score']:.4f} (>0 = better than coin flip)")
        print(f"  Log score: {stats['avg_log_score']:.4f}")
        print(f"  Overconfidence: {stats['overconfidence_score']:+.4f}")
    else:
        print("  No resolved predictions yet.")

    shrink = stats["num_predictions"] < 50
    platt = stats["num_predictions"] >= 30
    print(f"\n  Shrinkage: {'Active' if shrink else 'Disabled'} ({stats['num_predictions']}/50 needed)")
    print(f"  Platt scaling: {'Active' if platt else 'Inactive'} ({stats['num_predictions']}/30 needed)")

    if stats.get("calibration_bins"):
        print(f"\n  CALIBRATION CURVE:")
        print(f"  {'Bin':>6s} | {'Predicted':>9s} | {'Actual':>9s} | {'Count':>5s} | {'Dev':>6s}")
        print(f"  {'-'*6}-+-{'-'*9}-+-{'-'*9}-+-{'-'*5}-+-{'-'*6}")
        for b in stats["calibration_bins"]:
            print(f"  {b['bin_center']:6.2f} | {b['avg_predicted']:9.4f} | {b['avg_actual']:9.4f} | {b['count']:5d} | {b['deviation']:+6.4f}")


def cmd_import(args):
    """Import market data from JSON file."""
    init_db()

    filepath = args.file
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return

    with open(filepath) as f:
        data = json.load(f)

    if not isinstance(data, list):
        data = [data]

    conn = get_conn()
    now = datetime.now(timezone.utc).isoformat()
    from db import upsert_market, classify_market
    
    imported = 0
    for m in data:
        try:
            # Check if this is already in our simple format (has yes_price field)
            if "yes_price" in m and "no_price" in m:
                # Direct insert for pre-parsed data
                cid = m.get("condition_id", "")
                q = m.get("question", "")
                if cid and q:
                    conn.execute("""
                        INSERT OR REPLACE INTO markets
                        (condition_id, question, yes_price, no_price, volume_24h, total_volume,
                         liquidity, end_date, resolved, active, market_type, last_scraped_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?)
                    """, (
                        cid, q,
                        float(m.get("yes_price") or 0),
                        float(m.get("no_price") or 0),
                        float(m.get("volume_24h") or 0),
                        float(m.get("volume") or 0),
                        float(m.get("liquidity") or 0),
                        m.get("end_date", ""),
                        classify_market(q, ""),
                        now,
                    ))
                    imported += 1
            else:
                # Try normalize_gamma_market for raw API data
                normalized = normalize_gamma_market(m)
                if normalized:
                    upsert_market(conn, normalized)
                    imported += 1
        except Exception as e:
            continue
    
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    conn.close()
    print(f"Imported {imported} markets from {filepath}. DB total: {total}")


def cmd_backtest(args):
    """Run backtest comparing fusion engine vs simple average on resolved markets."""
    from backtester import run_backtest, print_backtest_report
    limit   = getattr(args, "limit",   200)
    verbose = getattr(args, "verbose", False)
    record  = getattr(args, "record",  False)

    print(f"Running backtest on up to {limit} resolved markets...")
    if record:
        print("  (--record: writing Brier scores to signal_accuracy table)")
    print()

    stats = run_backtest(limit=limit, verbose=verbose, record=record)
    print_backtest_report(stats)

    if not stats:
        print("  Tip: fetch resolved markets first:")
        print("       python run.py fetch --resolved --pages 10")


def cmd_tune_weights(args):
    """Recompute signal weights from signal_accuracy history and print recommendations."""
    init_db()
    from signal_fusion import SignalFusionEngine

    conn = get_conn()
    engine = SignalFusionEngine()
    lookback = getattr(args, "lookback", 90)
    min_samples = getattr(args, "min_samples", 20)

    result = engine.compute_optimal_weights(
        conn, lookback_days=lookback, min_samples=min_samples
    )
    conn.close()

    if result["status"] == "insufficient_data":
        print("Insufficient signal_accuracy data to tune weights.")
        print("  Run: python run.py backtest --record  (to populate history)")
        return

    print("=" * 60)
    print("SIGNAL WEIGHT RECOMMENDATIONS")
    print("=" * 60)
    print(f"  Based on last {lookback} days, min {min_samples} samples per signal")
    print()

    from signal_fusion import WEIGHT_PROFILES
    for mtype, weights in sorted(result["profiles"].items()):
        counts = result["sample_counts"].get(mtype, {})
        current = WEIGHT_PROFILES.get(mtype, WEIGHT_PROFILES["default"])
        print(f"  Profile: {mtype}")
        print(f"  {'Signal':<16} {'Current':>8} {'Suggested':>10} {'Samples':>8}")
        print(f"  {'-'*44}")
        for sig, new_w in sorted(weights.items()):
            old_w = current.get(sig, 0.0)
            n = counts.get(sig, 0)
            delta = new_w - old_w
            flag = " <-- update" if abs(delta) > 0.03 and n >= min_samples else ""
            print(f"  {sig:<16} {old_w:>8.3f} {new_w:>10.3f} {n:>8d}{flag}")
        print()

    print("  To apply these weights permanently, edit WEIGHT_PROFILES in signal_fusion.py")


def cmd_paper_trade(args):
    """Run a paper-trading session (no real orders placed)."""
    from paper_trader import run_paper_session, paper_report
    top_n   = getattr(args, "top",     20)
    verbose = getattr(args, "verbose", False)
    market  = getattr(args, "market",  None)
    amount  = getattr(args, "amount",  None)

    if market:
        print(f"Paper trade — targeting market: {market}" +
              (f" | ${amount:.2f}" if amount else ""))
    else:
        print(f"Paper-trading session — scanning top {top_n} markets...")
    print()
    stats = run_paper_session(top_n=top_n, verbose=verbose,
                              market_keyword=market, amount=amount)
    print(f"\nScanned: {stats['scanned']}  |  Signals: {stats['signals']}  |  Recorded: {stats['recorded']}")
    print()
    print(paper_report(verbose=verbose))


def cmd_paper_report(args):
    """Print the paper-trading performance report."""
    from paper_trader import paper_report
    verbose = getattr(args, "verbose", False)
    print(paper_report(verbose=verbose))


def cmd_dashboard(args):
    """Launch the trading terminal dashboard."""
    from dashboard import Dashboard
    dash = Dashboard()
    try:
        dash.render()
    finally:
        dash.close()


def cmd_fetch(args):
    """Fetch live market data from the Gamma API and upsert into the DB."""
    init_db()
    from api_client import PolymarketClient
    from db import upsert_market

    client = PolymarketClient()
    limit = getattr(args, "limit", 100)
    pages = getattr(args, "pages", 5)
    resolved = getattr(args, "resolved", False)

    conn = get_conn()
    total_upserted = 0

    for page in range(pages):
        offset = page * limit
        print(f"  Fetching page {page + 1}/{pages} (offset={offset}, limit={limit})...", end=" ", flush=True)
        try:
            raw_markets = client.get_gamma_markets(
                limit=limit,
                offset=offset,
                active=not resolved,
                closed=resolved,
            )
        except Exception as e:
            print(f"ERROR: {e}")
            break

        if not raw_markets:
            print("no results — stopping.")
            break

        page_count = 0
        for m in raw_markets:
            try:
                normalized = normalize_gamma_market(m)
                if normalized:
                    upsert_market(conn, normalized)
                    page_count += 1
            except Exception:
                continue

        conn.commit()
        total_upserted += page_count
        print(f"{page_count} markets upserted.")

        if len(raw_markets) < limit:
            break  # Last page

    active = conn.execute("SELECT COUNT(*) FROM markets WHERE resolved=0").fetchone()[0]
    conn.close()
    print(f"\nDone. Total upserted this run: {total_upserted}. Active markets in DB: {active}")


def main():
    parser = argparse.ArgumentParser(
        description="Polymarket Trading Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py status                          System overview
  python run.py fetch                           Fetch 5 pages of live market data
  python run.py fetch --pages 1 --limit 50      Fetch 1 page of 50 markets
  python run.py scan --top 20                   Scan top 20 markets for signals
  python run.py analyze "Iran ceasefire"        Analyze market by keyword
  python run.py analyze <cid> -p 0.72           Analyze with your probability estimate
  python run.py analyze <cid> -p 0.72 --mirofish  Full analysis with MiroFish sim
  python run.py execute <cid> YES 50            Execute $50 YES trade (dry-run)
  python run.py execute <cid> YES 50 --live     Execute for real
  python run.py monitor                         Check all open positions for alerts
  python run.py positions                       List open positions with P&L
  python run.py daily                           Daily P&L report
  python run.py post-mortem <trade_id>          Analyze a closed trade
  python run.py portfolio                       Portfolio summary
  python run.py calibration                     Calibration stats & curve
  python run.py seed                            Bootstrap calibration with synthetic data
  python run.py seed --clear --n 20             Reset and re-seed with 20 samples/template
  python run.py import-resolved                 Import resolved_markets_400.csv
  python run.py import-resolved --csv file.csv  Import from specific CSV
  python run.py import markets.json             Import active market data from JSON
  python run.py fetch --resolved --pages 10     Fetch 10 pages of resolved markets
  python run.py backtest --limit 200 --record   Backtest + record Brier scores
  python run.py tune-weights                    Recommend new signal weights
  python run.py paper-trade --top 30            Paper-trade session on top 30 markets
  python run.py paper-report --verbose          Show all paper trades + P&L
        """
    )
    subs = parser.add_subparsers(dest="command")

    # status
    subs.add_parser("status", help="System status overview")

    # fetch
    fetch_p = subs.add_parser("fetch", help="Fetch live market data from Gamma API")
    fetch_p.add_argument("--limit", type=int, default=100, help="Markets per page (default: 100)")
    fetch_p.add_argument("--pages", type=int, default=5, help="Number of pages to fetch (default: 5)")
    fetch_p.add_argument("--resolved", action="store_true", help="Fetch resolved markets instead of active")

    # scan
    scan_p = subs.add_parser("scan", help="Scan markets for trade signals")
    scan_p.add_argument("--top", type=int, default=20, help="Number of markets to scan (default: 20)")
    scan_p.add_argument("--json", type=str, help="Save results to JSON file")

    # analyze
    analyze_p = subs.add_parser("analyze", help="Deep analysis on a market")
    analyze_p.add_argument("market", help="Condition ID or question keyword")
    analyze_p.add_argument("-p", "--prob", type=float, help="Your probability estimate (0-1)")
    analyze_p.add_argument("--mirofish", action="store_true", help="Run MiroFish simulation")

    # execute
    execute_p = subs.add_parser("execute", help="Execute a trade (dry-run by default)")
    execute_p.add_argument("market", help="Condition ID or question keyword")
    execute_p.add_argument("side", help="YES or NO")
    execute_p.add_argument("size", type=float, help="Position size in USDC")
    execute_p.add_argument("--live", action="store_true", help="Execute for real (not a dry-run)")
    execute_p.add_argument("--thesis", type=str, default="Manual CLI execution", help="Trade thesis")

    # monitor
    subs.add_parser("monitor", help="Check open positions for stop-loss/take-profit triggers")

    # positions
    subs.add_parser("positions", help="List open positions with unrealized P&L")

    # daily
    subs.add_parser("daily", help="Daily P&L report")

    # post-mortem
    pm_p = subs.add_parser("post-mortem", help="Post-mortem analysis of a closed trade")
    pm_p.add_argument("trade_id", help="Trade ID to analyze")

    # portfolio
    subs.add_parser("portfolio", help="Portfolio summary")

    # calibration
    subs.add_parser("calibration", help="Calibration status and curve")

    # seed
    seed_p = subs.add_parser("seed", help="Bootstrap calibration with synthetic data")
    seed_p.add_argument("--clear", action="store_true", help="Clear existing seeded data first")
    seed_p.add_argument("--n", type=int, default=15, help="Samples per category template")

    # import-resolved
    ir_p = subs.add_parser("import-resolved", help="Import resolved markets CSV for calibration")
    ir_p.add_argument("--csv", type=str, help="Path to CSV file (auto-finds if not specified)")
    ir_p.add_argument("--skip-sports", action="store_true", help="Skip sports markets")

    # import (JSON active markets)
    import_p = subs.add_parser("import", help="Import active market data from JSON")
    import_p.add_argument("file", help="Path to JSON file")

    # backtest
    bt_p = subs.add_parser("backtest", help="Backtest fusion engine on resolved markets")
    bt_p.add_argument("--limit",   type=int, default=200,   help="Max resolved markets to test (default: 200)")
    bt_p.add_argument("--verbose", action="store_true",     help="Print per-market results")
    bt_p.add_argument("--record",  action="store_true",     help="Write Brier scores to signal_accuracy table")

    # tune-weights
    tw_p = subs.add_parser("tune-weights", help="Recommend signal weight updates from accuracy history")
    tw_p.add_argument("--lookback",     type=int, default=90, help="Days of history to use (default: 90)")
    tw_p.add_argument("--min-samples",  type=int, default=20, help="Min samples required per signal (default: 20)")

    # paper-trade
    pt_p = subs.add_parser("paper-trade", help="Simulated trading session (no real orders)")
    pt_p.add_argument("--top",     type=int,   default=20,  help="Markets to scan (default: 20)")
    pt_p.add_argument("--market",  type=str,   default=None, help="Target a specific market (keyword or condition ID)")
    pt_p.add_argument("--amount",  type=float, default=None, help="Override position size in USDC")
    pt_p.add_argument("--verbose", action="store_true",      help="Print per-market details")

    # paper-report
    pr_p = subs.add_parser("paper-report", help="Paper-trading performance report")
    pr_p.add_argument("--verbose", action="store_true", help="Show individual trade details")

    # dashboard
    subs.add_parser("dashboard", help="Trading terminal dashboard")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "fetch": cmd_fetch,
        "scan": cmd_scan,
        "analyze": cmd_analyze,
        "execute": cmd_execute,
        "monitor": cmd_monitor,
        "positions": cmd_positions,
        "daily": cmd_daily,
        "post-mortem": cmd_post_mortem,
        "portfolio": cmd_portfolio,
        "calibration": cmd_calibration,
        "seed": cmd_seed,
        "import-resolved": cmd_import_resolved,
        "import": cmd_import,
        "backtest": cmd_backtest,
        "tune-weights": cmd_tune_weights,
        "paper-trade": cmd_paper_trade,
        "paper-report": cmd_paper_report,
        "dashboard": cmd_dashboard,
    }

    if args.command in commands:
        commands[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
