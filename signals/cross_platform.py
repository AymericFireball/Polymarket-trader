"""
Signal 4: Cross-Platform Correlations
=======================================
Checks the same question on Metaculus, Manifold, and other platforms.
If a correlated market has already moved, flags it as a leading indicator.

No API keys needed — Metaculus and Manifold have public APIs.
"""

import os
import sys
import json
import re
import requests
from datetime import datetime, timezone
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


METACULUS_API = "https://www.metaculus.com/api2/questions/"
MANIFOLD_API = "https://api.manifold.markets/v0"


def normalize_question(question: str) -> str:
    """Strip prediction market boilerplate to get the core question."""
    q = question.lower().strip()
    for prefix in ["will ", "is ", "are ", "does ", "do ", "has ", "have "]:
        if q.startswith(prefix):
            q = q[len(prefix):]
    q = q.rstrip("?").strip()
    return q


def search_metaculus(question: str, limit: int = 5) -> List[Dict]:
    """Search Metaculus for related questions."""
    try:
        resp = requests.get(
            METACULUS_API,
            params={
                "search": normalize_question(question),
                "limit": limit,
                "status": "open",
                "type": "forecast",
            },
            timeout=10,
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])

        return [{
            "platform": "metaculus",
            "id": r.get("id"),
            "title": r.get("title", ""),
            "url": f"https://www.metaculus.com/questions/{r.get('id')}/",
            "community_prediction": r.get("community_prediction", {}).get("full", {}).get("q2"),
            "num_predictions": r.get("number_of_predictions", 0),
            "created_at": r.get("created_time", ""),
            "resolve_time": r.get("resolve_time", ""),
        } for r in results if r.get("community_prediction")]

    except Exception as e:
        return []


def search_manifold(question: str, limit: int = 5) -> List[Dict]:
    """Search Manifold Markets for related questions."""
    try:
        resp = requests.get(
            f"{MANIFOLD_API}/search-markets",
            params={"term": normalize_question(question), "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()

        if not isinstance(markets, list):
            markets = markets.get("data", markets.get("markets", []))

        return [{
            "platform": "manifold",
            "id": m.get("id", ""),
            "title": m.get("question", ""),
            "url": m.get("url", ""),
            "probability": m.get("probability"),
            "volume": m.get("volume", 0),
            "liquidity": m.get("totalLiquidity", 0),
            "num_traders": m.get("uniqueBettorCount", 0),
            "created_at": "",
            "close_time": m.get("closeTime", ""),
        } for m in markets if m.get("probability") is not None]

    except Exception as e:
        return []


STOP_WORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "by", "with", "from", "that", "this", "be", "been", "is", "was", "are",
    "were", "it", "its", "as", "not", "no", "yes", "if", "than", "then",
    "what", "when", "who", "which", "how", "their", "they", "he", "she",
    "we", "you", "i", "my", "his", "her", "our", "any", "all", "more",
    "least", "most", "before", "after", "during", "about", "between",
}


def compute_similarity(polymarket_question: str, other_title: str) -> float:
    """
    Simple word-overlap similarity between two questions.
    Returns 0-1 score.
    """
    words1 = set(normalize_question(polymarket_question).split()) - STOP_WORDS
    words2 = set(normalize_question(other_title).split()) - STOP_WORDS

    if not words1 or not words2:
        return 0.0

    overlap = words1 & words2
    union = words1 | words2

    # Jaccard similarity
    return len(overlap) / len(union) if union else 0.0


def get_cross_platform_signal(question: str, polymarket_price: float) -> Dict:
    """
    Full cross-platform signal for a market question.

    Returns:
    {
        "signal_type": "cross_platform",
        "score": float (-1 to +1),
        "matches": [...],
        "avg_cross_platform_price": float,
        "delta_from_polymarket": float,
        "platforms_checked": [...],
        "freshness_ts": str,
    }
    """
    all_matches = []
    platforms_checked = []

    # Search Metaculus
    metaculus_results = search_metaculus(question)
    platforms_checked.append("metaculus")
    for r in metaculus_results:
        sim = compute_similarity(question, r["title"])
        if sim >= 0.35:  # Minimum similarity threshold
            r["similarity"] = round(sim, 3)
            r["price"] = r.get("community_prediction")
            all_matches.append(r)

    # Search Manifold
    manifold_results = search_manifold(question)
    platforms_checked.append("manifold")
    for r in manifold_results:
        sim = compute_similarity(question, r["title"])
        if sim >= 0.2:
            r["similarity"] = round(sim, 3)
            r["price"] = r.get("probability")
            all_matches.append(r)

    # Sort by similarity
    all_matches.sort(key=lambda x: x.get("similarity", 0), reverse=True)

    # Compute aggregate cross-platform price
    valid_prices = [m["price"] for m in all_matches if m.get("price") is not None]

    if valid_prices:
        # Weight by similarity
        weights = [m.get("similarity", 0.5) for m in all_matches if m.get("price") is not None]
        weighted_avg = sum(p * w for p, w in zip(valid_prices, weights)) / sum(weights)
        delta = round(weighted_avg - polymarket_price, 4)

        # Signal: positive delta means cross-platforms think YES is more likely
        # than Polymarket does — could be a buy signal for YES
        signal_score = max(-1.0, min(1.0, delta * 5))  # Scale delta to -1/+1
    else:
        weighted_avg = None
        delta = 0.0
        signal_score = 0.0

    return {
        "signal_type": "cross_platform",
        "score": round(signal_score, 4),
        "matches": all_matches[:5],
        "avg_cross_platform_price": round(weighted_avg, 4) if weighted_avg else None,
        "delta_from_polymarket": delta,
        "num_matches": len(all_matches),
        "platforms_checked": platforms_checked,
        "freshness_ts": datetime.now(timezone.utc).isoformat(),
        "source": "metaculus+manifold",
        "raw_data": {
            "metaculus_results": len(metaculus_results),
            "manifold_results": len(manifold_results),
        },
    }


def format_for_mirofish(signal: Dict) -> str:
    """Format cross-platform signal for MiroFish seed material."""
    lines = ["CROSS-PLATFORM SIGNALS:"]

    if not signal.get("matches"):
        lines.append("  No matching markets found on other platforms.")
        return "\n".join(lines)

    avg = signal.get("avg_cross_platform_price")
    delta = signal.get("delta_from_polymarket", 0)
    if avg:
        lines.append(f"  Weighted avg probability across platforms: {avg:.1%}")
        lines.append(f"  Delta from Polymarket: {delta:+.1%}")
    lines.append("")

    for m in signal["matches"][:3]:
        price = m.get("price")
        lines.append(f"  [{m['platform'].upper()}] {m['title']}")
        if price is not None:
            lines.append(f"    Probability: {price:.1%} | Similarity: {m.get('similarity', 0):.0%}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "Will the SEC approve a Bitcoin ETF?"
    price = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
    print(f"Cross-platform signal for: {q} (Polymarket price: {price})")
    signal = get_cross_platform_signal(q, price)
    print(json.dumps(signal, indent=2, default=str))
    print("\nMiroFish format:")
    print(format_for_mirofish(signal))
