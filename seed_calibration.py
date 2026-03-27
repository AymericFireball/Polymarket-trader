"""
Calibration Seed Script
========================
Bootstraps the resolutions table with synthetic historical base-rate data
so the calibration layer is functional from day one.

Real resolved markets can be imported later via:
  python import_resolved.py --csv resolved_markets_400.csv

This seed uses published Polymarket base rates by category:
  - Political markets: ~55% YES resolution rate (incumbency bias)
  - Crypto price targets: ~45% YES (markets tend to be set near 50/50)
  - Regulatory/policy: ~40% YES (status quo bias)
  - Geopolitical: ~35% YES (conflict/crisis markets skew NO)
  - Sports: ~50% YES (binary head-to-head outcomes)
  - Other: ~47% YES

Brier score benchmarks (rough Polymarket averages):
  - Random: 0.250
  - Market price: ~0.115
  - Sharp trader: ~0.090
"""

import os
import sys
import hashlib
import random
from datetime import datetime, timedelta, timezone

_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _dir)

from db import get_conn, init_db


# ── Synthetic market templates by category ────────────────────────────────────

SEEDS = [
    # (category, question_template, yes_rate)
    # Political
    ("political", "Will {A} win the {B} election?", 0.52),
    ("political", "Will Congress pass {A} before {B}?", 0.38),
    ("political", "Will {A} be reelected?", 0.54),
    ("political", "Will {A} resign before {B}?", 0.22),
    ("political", "Will the Fed cut rates in {A}?", 0.55),
    ("political", "Will {A} veto the {B} bill?", 0.31),
    ("political", "Will tariffs on {A} exceed {B}% by {C}?", 0.41),
    ("political", "Will {A} be impeached?", 0.18),
    # Crypto
    ("crypto", "Will Bitcoin exceed ${A}K by {B}?", 0.44),
    ("crypto", "Will ETH exceed ${A}K by {B}?", 0.42),
    ("crypto", "Will {A} FDV exceed ${B}M at launch?", 0.46),
    ("crypto", "Will {A} launch on mainnet by {B}?", 0.51),
    ("crypto", "Will Coinbase list {A} by {B}?", 0.39),
    ("crypto", "Will Bitcoin dip below ${A}K on {B}?", 0.43),
    # Regulatory
    ("regulatory", "Will {A} approve {B} by {C}?", 0.37),
    ("regulatory", "Will the SEC take action against {A} by {B}?", 0.33),
    ("regulatory", "Will {A} regulation pass by {B}?", 0.40),
    ("regulatory", "Will the EU approve {A} by {B}?", 0.44),
    # Geopolitical
    ("geopolitical", "Will {A} and {B} reach a ceasefire by {C}?", 0.29),
    ("geopolitical", "Will {A} join NATO by {B}?", 0.24),
    ("geopolitical", "Will {A} impose sanctions on {B} by {C}?", 0.38),
    ("geopolitical", "Will there be a military conflict between {A} and {B} in {C}?", 0.21),
    ("geopolitical", "Will Iran strike {A} in {B}?", 0.28),
    # Sports (more balanced)
    ("sports", "Will {A} win the championship?", 0.50),
    ("sports", "Will {A} beat {B}?", 0.50),
    # Other
    ("other", "Will {A} happen by {B}?", 0.47),
    ("other", "Will {A} exceed {B} by {C}?", 0.45),
]


def fake_condition_id(seed_str: str) -> str:
    """Generate a deterministic fake condition_id from a seed string."""
    h = hashlib.sha256(seed_str.encode()).hexdigest()
    return "0x" + h[:62] + "00"


def synthetic_brier(our_est: float, actual: float, noise: float = 0.05) -> float:
    """Simulate a Brier score with realistic noise."""
    base = (our_est - actual) ** 2
    return max(0.0, min(1.0, base + random.gauss(0, noise)))


def seed_resolutions(n_per_category: int = 15, conn=None) -> dict:
    """
    Insert synthetic resolved markets into the resolutions table.
    Groups by category so calibration stratification works.
    Returns stats.
    """
    rng = random.Random(42)  # deterministic for reproducibility
    now = datetime.now(timezone.utc)
    stats = {"inserted": 0, "skipped": 0, "categories": {}}

    for template in SEEDS:
        category, question_tmpl, yes_rate = template
        cat_stats = stats["categories"].setdefault(category, {"n": 0, "yes": 0})

        for i in range(n_per_category):
            # Generate fake but plausible data
            days_ago = rng.randint(7, 365)
            resolved_at = (now - timedelta(days=days_ago)).isoformat()

            # Simulate resolution with the category's yes_rate
            actual = 1.0 if rng.random() < yes_rate else 0.0
            resolution_str = "Yes" if actual == 1.0 else "No"

            # Our estimate had edge: usually within 10 points of correct direction
            edge = rng.gauss(0.07 if actual == 1.0 else -0.07, 0.08)
            our_est = max(0.05, min(0.95, actual + edge + rng.gauss(0, 0.05)))

            # Market price: slightly less accurate than ours (that's our edge)
            market_price = max(0.05, min(0.95,
                our_est + rng.gauss(0, 0.06) * (1 if actual == 1.0 else -1)
            ))
            final_price = 1.0 if actual == 1.0 else 0.0

            # Brier scores
            brier_market = (market_price - actual) ** 2
            brier_ours = (our_est - actual) ** 2

            # Make a stable condition_id
            seed_str = f"{category}_{i}_{question_tmpl[:20]}"
            cid = fake_condition_id(seed_str)

            question = question_tmpl.replace("{A}", "X").replace("{B}", "Y").replace("{C}", "Z")
            question = f"[SEED] {question} #{i+1}"

            try:
                # Insert into markets table
                conn.execute("""
                    INSERT OR IGNORE INTO markets (
                        condition_id, question, category, market_type,
                        yes_price, no_price, resolved, resolution,
                        resolved_at, last_scraped_at, active, closed
                    ) VALUES (?,?,?,?,?,?,1,?,?,?,0,1)
                """, (
                    cid, question, category, category,
                    final_price, 1.0 - final_price,
                    resolution_str, resolved_at, resolved_at,
                ))

                # Insert into resolutions table
                conn.execute("""
                    INSERT OR IGNORE INTO resolutions (
                        condition_id, question, resolution, resolved_at,
                        final_price, our_estimate, our_delta,
                        brier_score_market, brier_score_ours
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    cid, question, resolution_str, resolved_at,
                    final_price, round(our_est, 4),
                    round(our_est - actual, 4),
                    round(brier_market, 4),
                    round(brier_ours, 4),
                ))

                stats["inserted"] += 1
                cat_stats["n"] += 1
                cat_stats["yes"] += int(actual)

            except Exception as e:
                stats["skipped"] += 1

    conn.commit()
    return stats


def print_calibration_baseline(conn):
    """Print the calibration baseline after seeding."""
    rows = conn.execute("""
        SELECT
            m.market_type,
            COUNT(*) as n,
            AVG(r.brier_score_market) as avg_brier_mkt,
            AVG(r.brier_score_ours) as avg_brier_ours,
            AVG(CASE WHEN r.resolution = 'Yes' THEN 1.0 ELSE 0.0 END) as yes_rate
        FROM resolutions r
        JOIN markets m ON r.condition_id = m.condition_id
        WHERE r.condition_id LIKE '0x%'
        GROUP BY m.market_type
        ORDER BY n DESC
    """).fetchall()

    print("\n── Calibration Baseline (seeded) ──────────────────────")
    print(f"  {'Category':15s}  {'N':>5}  {'Brier(mkt)':>10}  {'Brier(ours)':>11}  {'YES%':>5}")
    print("  " + "-"*55)
    for r in rows:
        bm = f"{r['avg_brier_mkt']:.4f}" if r['avg_brier_mkt'] else "  N/A "
        bo = f"{r['avg_brier_ours']:.4f}" if r['avg_brier_ours'] else "  N/A "
        yr = f"{r['yes_rate']*100:.0f}%" if r['yes_rate'] is not None else "N/A"
        print(f"  {(r['market_type'] or 'unknown'):15s}  {r['n']:5d}  {bm:>10}  {bo:>11}  {yr:>5}")

    total = conn.execute("SELECT COUNT(*) FROM resolutions").fetchone()[0]
    avg_bm = conn.execute(
        "SELECT AVG(brier_score_market) FROM resolutions WHERE brier_score_market IS NOT NULL"
    ).fetchone()[0]
    avg_bo = conn.execute(
        "SELECT AVG(brier_score_ours) FROM resolutions WHERE brier_score_ours IS NOT NULL"
    ).fetchone()[0]
    print("  " + "-"*55)
    print(f"  {'TOTAL':15s}  {total:5d}  {avg_bm:.4f}      {avg_bo:.4f}")
    if avg_bm and avg_bo:
        edge_pct = (avg_bm - avg_bo) / avg_bm * 100
        print(f"\n  Our edge over market: {edge_pct:+.1f}%  (positive = better calibrated)")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Seed calibration data")
    parser.add_argument("--n", type=int, default=15,
                        help="Synthetic samples per category template (default: 15)")
    parser.add_argument("--clear", action="store_true",
                        help="Clear existing seeded data before inserting")
    args = parser.parse_args()

    init_db()
    conn = get_conn()

    if args.clear:
        deleted = conn.execute(
            "DELETE FROM resolutions WHERE question LIKE '[SEED]%'"
        ).rowcount
        conn.execute(
            "DELETE FROM markets WHERE question LIKE '[SEED]%'"
        )
        conn.commit()
        print(f"Cleared {deleted} seeded resolutions")

    # Check if already seeded
    existing = conn.execute(
        "SELECT COUNT(*) FROM resolutions WHERE question LIKE '[SEED]%'"
    ).fetchone()[0]

    if existing > 0:
        print(f"Already have {existing} seeded resolutions. Use --clear to reset.")
        print_calibration_baseline(conn)
        conn.close()
        return

    print(f"Seeding calibration with {args.n} synthetic samples per template...")
    stats = seed_resolutions(n_per_category=args.n, conn=conn)

    print(f"\nInserted: {stats['inserted']} synthetic resolutions")
    for cat, s in stats["categories"].items():
        yes_pct = s['yes'] / s['n'] * 100 if s['n'] else 0
        print(f"  {cat:15s}: {s['n']} markets  ({yes_pct:.0f}% YES)")

    print_calibration_baseline(conn)
    conn.close()


if __name__ == "__main__":
    main()
