"""
Signal 3: Historical Base Rate
================================
Queries our local database of resolved Polymarket markets to find
comparable past markets and calculate base rates.

"Markets like this resolved YES X% of the time."
"""

import os
import sys
import json
import re
from datetime import datetime, timezone
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from db import get_conn, init_db


def tokenize(text: str) -> set:
    """Simple word tokenization."""
    return set(re.findall(r'[a-z]+', text.lower()))


def find_comparable_markets(question: str, market_type: str = None,
                            min_similarity: float = 0.15,
                            limit: int = 20) -> List[Dict]:
    """
    Find resolved markets similar to the given question.
    Uses word overlap as similarity metric.
    """
    conn = get_conn()

    query = "SELECT * FROM markets WHERE resolved=1 AND resolution IS NOT NULL"
    params = []
    if market_type:
        query += " AND market_type=?"
        params.append(market_type)
    query += " ORDER BY total_volume DESC LIMIT 1000"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    question_tokens = tokenize(question)
    if not question_tokens:
        return []

    comparable = []
    for row in rows:
        row_dict = dict(row)
        row_tokens = tokenize(row_dict.get("question", ""))

        if not row_tokens:
            continue

        # Jaccard similarity
        overlap = question_tokens & row_tokens
        union = question_tokens | row_tokens
        similarity = len(overlap) / len(union) if union else 0

        if similarity >= min_similarity:
            row_dict["similarity"] = round(similarity, 3)
            comparable.append(row_dict)

    comparable.sort(key=lambda x: x["similarity"], reverse=True)
    return comparable[:limit]


def compute_base_rate(comparable_markets: List[Dict]) -> Dict:
    """
    Compute the base rate from comparable resolved markets.

    Returns:
    {
        "base_rate": float (0-1, rate of YES resolution),
        "sample_size": int,
        "yes_count": int,
        "no_count": int,
        "avg_similarity": float,
        "comparable_markets": [...]  # Top matches
    }
    """
    if not comparable_markets:
        return {
            "base_rate": 0.5,  # Default: maximum uncertainty
            "sample_size": 0,
            "yes_count": 0,
            "no_count": 0,
            "avg_similarity": 0,
            "comparable_markets": [],
        }

    yes_count = sum(1 for m in comparable_markets if m.get("resolution") == "Yes")
    no_count = sum(1 for m in comparable_markets if m.get("resolution") == "No")
    total = yes_count + no_count

    if total == 0:
        return {
            "base_rate": 0.5,
            "sample_size": 0,
            "yes_count": 0,
            "no_count": 0,
            "avg_similarity": 0,
            "comparable_markets": [],
        }

    # Weight by similarity
    weighted_yes = sum(
        m["similarity"] for m in comparable_markets
        if m.get("resolution") == "Yes"
    )
    weighted_total = sum(m["similarity"] for m in comparable_markets if m.get("resolution"))

    base_rate = weighted_yes / weighted_total if weighted_total > 0 else 0.5
    avg_sim = sum(m["similarity"] for m in comparable_markets) / len(comparable_markets)

    return {
        "base_rate": round(base_rate, 4),
        "sample_size": total,
        "yes_count": yes_count,
        "no_count": no_count,
        "avg_similarity": round(avg_sim, 3),
        "comparable_markets": [
            {
                "question": m["question"],
                "resolution": m["resolution"],
                "similarity": m["similarity"],
                "yes_price": m.get("yes_price"),
                "total_volume": m.get("total_volume"),
            }
            for m in comparable_markets[:5]
        ],
    }


def get_base_rate_signal(question: str, market_type: str = None) -> Dict:
    """
    Full base rate signal for a market question.
    """
    comparable = find_comparable_markets(question, market_type)
    base = compute_base_rate(comparable)

    # Convert base rate to a -1/+1 signal
    # 0.5 base rate = 0 signal (neutral)
    # 0.8 base rate = +0.6 signal (markets like this usually resolve YES)
    signal_score = (base["base_rate"] - 0.5) * 2

    return {
        "signal_type": "base_rate",
        "score": round(signal_score, 4),
        "base_rate": base["base_rate"],
        "sample_size": base["sample_size"],
        "yes_count": base["yes_count"],
        "no_count": base["no_count"],
        "avg_similarity": base["avg_similarity"],
        "comparable_markets": base["comparable_markets"],
        "freshness_ts": datetime.now(timezone.utc).isoformat(),
        "source": "local_db",
        "raw_data": {},
    }


def format_for_mirofish(signal: Dict) -> str:
    """Format base rate signal for MiroFish seed material."""
    lines = ["COMPARABLE HISTORICAL MARKETS:"]

    if signal["sample_size"] == 0:
        lines.append("  No comparable resolved markets found in database.")
        lines.append("  (Database may need more resolved market data.)")
        return "\n".join(lines)

    lines.append(f"  Base rate: {signal['base_rate']:.0%} YES resolution "
                 f"(n={signal['sample_size']}, "
                 f"{signal['yes_count']} YES / {signal['no_count']} NO)")
    lines.append("")

    for m in signal["comparable_markets"][:3]:
        res = m["resolution"]
        lines.append(f"  [{res}] {m['question']}")
        lines.append(f"    Similarity: {m['similarity']:.0%} | "
                     f"Final price: {m.get('yes_price', '?')}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    init_db()
    q = sys.argv[1] if len(sys.argv) > 1 else "Will Israel launch a ground offensive?"
    print(f"Base rate signal for: {q}")
    signal = get_base_rate_signal(q)
    print(json.dumps(signal, indent=2, default=str))
