"""
Backtester
==========
Compares the signal fusion engine against a simple weighted average
on resolved markets already in the database.

Uses only signals that work on historical data (base_rate + cross_platform).
News and sharp trader signals require real-time context so are excluded.

Usage:
    python backtester.py --limit 200
    python run.py backtest --limit 200 --verbose
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from collections import defaultdict
from db import get_conn, init_db, get_resolved_markets, record_signal_accuracy
from calibration import brier_score
from signal_fusion import SignalFusionEngine
from preprocessor import preprocess_signals


def run_backtest(limit: int = 200, verbose: bool = False,
                 record: bool = False) -> dict:
    """
    Run backtest on resolved markets. Returns stats dict or None if not enough data.

    Args:
        limit:   Max resolved markets to test against.
        verbose: Print per-market results.
        record:  Write per-signal Brier rows to signal_accuracy table.
    """
    init_db()
    conn = get_conn()
    markets = get_resolved_markets(conn, limit=limit)

    if len(markets) < 10:
        print(f"  Not enough resolved markets ({len(markets)}/10 minimum).")
        print("  Fetch some first:  python run.py fetch --resolved --pages 5")
        conn.close()
        return {}

    fusion_engine = SignalFusionEngine()
    results = []

    for i, market in enumerate(markets):
        resolution = market.get("resolution", "")
        if not resolution:
            continue

        actual = 1.0 if str(resolution).lower() == "yes" else 0.0
        market_price = float(market.get("yes_price") or 0.5)
        market_type = market.get("market_type") or "other"
        cid = market["condition_id"]

        market_brier = brier_score(market_price, actual)

        # ── Signals available for historical data ──────────────────
        signals = {}

        try:
            from signals.base_rate import get_base_rate_signal
            # Exclude self from comparables to avoid data leakage
            br = get_base_rate_signal(
                market["question"], market_type,
                exclude_condition_id=cid,
            )
            signals["base_rate"] = br
        except TypeError:
            # Older version without exclude param
            try:
                from signals.base_rate import get_base_rate_signal
                signals["base_rate"] = get_base_rate_signal(
                    market["question"], market_type
                )
            except Exception:
                pass
        except Exception:
            pass

        # Cross-platform is optional and slow; skip in bulk backtest
        # (enable per-market with --cross-platform flag if desired)

        if not signals:
            continue

        # ── Simple weighted average path ───────────────────────────
        pp_bundle = preprocess_signals(signals)
        agg = pp_bundle.get("aggregate_score", 0)
        simple_prob = max(0.01, min(0.99, market_price + float(agg) * 0.15))
        simple_brier = brier_score(simple_prob, actual)

        # ── Fusion engine path ─────────────────────────────────────
        fusion_bundle = fusion_engine.fuse(signals, market_type,
                                           market_price=market_price)
        fusion_prob = float(fusion_bundle.get("fused_probability") or market_price)
        fusion_brier = brier_score(fusion_prob, actual)

        row = {
            "condition_id": cid,
            "question": market["question"][:70],
            "market_type": market_type,
            "resolution": resolution,
            "actual": actual,
            "market_price": market_price,
            "simple_prob": round(simple_prob, 4),
            "fusion_prob": round(fusion_prob, 4),
            "market_brier": round(market_brier, 4),
            "simple_brier": round(simple_brier, 4),
            "fusion_brier": round(fusion_brier, 4),
            "fusion_improvement": round(simple_brier - fusion_brier, 4),
            "signals": signals,
        }
        results.append(row)

        if verbose:
            winner = "fusion" if fusion_brier < simple_brier else "simple"
            print(f"  [{i+1:>3}] {winner:>6} | "
                  f"mkt={market_price:.2f} sim={simple_prob:.2f} fus={fusion_prob:.2f} "
                  f"| Δ={row['fusion_improvement']:+.4f} | {market['question'][:45]}")

        if record:
            try:
                record_signal_accuracy(
                    conn, cid, signals, resolution,
                    market_type=market_type,
                    our_estimate=fusion_prob,
                    market_price=market_price,
                )
            except Exception:
                pass

    if record:
        conn.commit()
    conn.close()

    if not results:
        return {}

    n = len(results)
    avg_market  = sum(r["market_brier"]  for r in results) / n
    avg_simple  = sum(r["simple_brier"]  for r in results) / n
    avg_fusion  = sum(r["fusion_brier"]  for r in results) / n
    fusion_wins = sum(1 for r in results if r["fusion_brier"] < r["simple_brier"])

    return {
        "n": n,
        "avg_market_brier":  round(avg_market,  4),
        "avg_simple_brier":  round(avg_simple,  4),
        "avg_fusion_brier":  round(avg_fusion,  4),
        "fusion_improvement": round(avg_simple - avg_fusion, 4),
        "fusion_win_rate":   round(fusion_wins / n, 3),
        "skill_vs_market":   round(1 - avg_fusion / avg_market, 4) if avg_market else 0,
        "per_type":          _by_type(results),
        "results":           results,
    }


def _by_type(results: list) -> dict:
    by_type: dict = defaultdict(list)
    for r in results:
        by_type[r["market_type"]].append(r)
    out = {}
    for mtype, items in by_type.items():
        n = len(items)
        out[mtype] = {
            "n": n,
            "avg_market_brier":   round(sum(r["market_brier"]  for r in items) / n, 4),
            "avg_simple_brier":   round(sum(r["simple_brier"]  for r in items) / n, 4),
            "avg_fusion_brier":   round(sum(r["fusion_brier"]  for r in items) / n, 4),
            "fusion_improvement": round(sum(r["fusion_improvement"] for r in items) / n, 4),
        }
    return out


def print_backtest_report(stats: dict) -> None:
    if not stats:
        return
    print("=" * 60)
    print("BACKTEST — FUSION ENGINE vs SIMPLE AVERAGE")
    print("=" * 60)
    print(f"  Markets tested    : {stats['n']}")
    print(f"  Uninformed baseline (always 50%): 0.2500")
    print()
    print(f"  {'Method':<22} {'Avg Brier':>10} {'Skill Score':>12}")
    print(f"  {'-'*46}")
    for label, key, in [
        ("Market price",   "avg_market_brier"),
        ("Simple average", "avg_simple_brier"),
        ("Fusion engine",  "avg_fusion_brier"),
    ]:
        b = stats[key]
        skill = round(1 - b / 0.25, 4)
        print(f"  {label:<22} {b:>10.4f} {skill:>+12.4f}")
    print()
    print(f"  Fusion improvement : {stats['fusion_improvement']:+.4f} Brier points")
    print(f"  Fusion win rate    : {stats['fusion_win_rate']:.1%} of markets")
    print(f"  Skill vs market    : {stats['skill_vs_market']:+.4f}")

    if stats.get("per_type"):
        print()
        print(f"  {'Type':<16} {'N':>4} {'Market':>8} {'Simple':>8} "
              f"{'Fusion':>8} {'Δ':>8}")
        print(f"  {'-'*56}")
        for mtype, t in sorted(stats["per_type"].items(),
                               key=lambda x: -x[1]["n"]):
            print(f"  {mtype:<16} {t['n']:>4} "
                  f"{t['avg_market_brier']:>8.4f} "
                  f"{t['avg_simple_brier']:>8.4f} "
                  f"{t['avg_fusion_brier']:>8.4f} "
                  f"{t['fusion_improvement']:>+8.4f}")
    print()


# ─── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Backtest fusion engine vs simple average")
    p.add_argument("--limit",   type=int, default=200, help="Max markets to test")
    p.add_argument("--verbose", action="store_true",   help="Print per-market results")
    p.add_argument("--record",  action="store_true",   help="Write to signal_accuracy table")
    args = p.parse_args()

    stats = run_backtest(limit=args.limit, verbose=args.verbose, record=args.record)
    print_backtest_report(stats)
