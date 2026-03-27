"""
Stage 3: Signal Pre-Processing & Scoring
==========================================
Normalizes all signals into a structured context bundle.
Applies recency decay, scores sentiment, flags contradictions.
Outputs a clean context_bundle for MiroFish simulation.
"""

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional


def preprocess_signals(signals: Dict[str, Dict]) -> Dict:
    """
    Take raw signal outputs and produce a normalized context bundle.

    Args:
        signals: {
            "news": <news signal dict>,
            "cross_platform": <cross-platform signal dict>,
            "sharp_trader": <sharp trader signal dict>,
            "base_rate": <base rate signal dict>,
        }

    Returns:
        context_bundle: normalized, scored, contradiction-flagged bundle
    """
    bundle = {
        "signals": {},
        "aggregate_score": 0.0,
        "confidence": "medium",
        "contradictions": [],
        "signal_count": 0,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    scores = []
    signal_details = []

    # Process each signal
    for signal_type, signal in signals.items():
        if not signal or not isinstance(signal, dict):
            continue

        score = signal.get("score", 0.0)

        # Normalize score to -1/+1
        score = max(-1.0, min(1.0, score))

        # Weight by signal reliability
        weight = _get_signal_weight(signal_type, signal)

        bundle["signals"][signal_type] = {
            "score": score,
            "weight": weight,
            "weighted_score": round(score * weight, 4),
            "confidence_contribution": _assess_signal_quality(signal_type, signal),
            "summary": _summarize_signal(signal_type, signal),
        }

        scores.append((score, weight))
        signal_details.append(signal)
        bundle["signal_count"] += 1

    # Compute weighted aggregate score
    if scores:
        total_weight = sum(w for _, w in scores)
        if total_weight > 0:
            bundle["aggregate_score"] = round(
                sum(s * w for s, w in scores) / total_weight, 4
            )

    # Detect contradictions
    bundle["contradictions"] = _detect_contradictions(bundle["signals"])

    # Assess overall confidence
    bundle["confidence"] = _assess_confidence(bundle)

    return bundle


def _get_signal_weight(signal_type: str, signal: Dict) -> float:
    """
    Assign weight to each signal type based on reliability hierarchy from the brief:
    1. Primary sources / historical data (highest)
    2. High-quality journalism / news
    3. Expert analysis / cross-platform
    4. Prediction markets consensus
    5. Social media (lowest)
    6. MiroFish simulations (analytical tool)
    """
    base_weights = {
        "base_rate": 0.25,       # Historical data is grounding
        "sharp_trader": 0.30,    # Smart money is the strongest signal
        "news": 0.25,            # News is important but noisy
        "cross_platform": 0.15,  # Other platforms as consensus check
        "sentiment": 0.05,       # Social is unreliable
    }
    weight = base_weights.get(signal_type, 0.10)

    # Adjust weight based on signal quality
    if signal_type == "base_rate" and signal.get("sample_size", 0) < 5:
        weight *= 0.5  # Low sample = less reliable
    if signal_type == "sharp_trader" and signal.get("traders_positioned", 0) < 2:
        weight *= 0.5  # Few traders = less reliable
    if signal_type == "news" and signal.get("article_count", 0) < 3:
        weight *= 0.7  # Few articles = less coverage

    return round(weight, 3)


def _assess_signal_quality(signal_type: str, signal: Dict) -> str:
    """Assess quality of individual signal: high/medium/low."""
    if signal_type == "news":
        if signal.get("article_count", 0) >= 5 and signal.get("avg_relevance", 0) > 0.5:
            return "high"
        elif signal.get("article_count", 0) >= 2:
            return "medium"
        return "low"

    if signal_type == "sharp_trader":
        if signal.get("traders_positioned", 0) >= 3:
            return "high"
        elif signal.get("traders_positioned", 0) >= 1:
            return "medium"
        return "low"

    if signal_type == "cross_platform":
        if signal.get("num_matches", 0) >= 2:
            return "medium"
        return "low"

    if signal_type == "base_rate":
        if signal.get("sample_size", 0) >= 10:
            return "high"
        elif signal.get("sample_size", 0) >= 3:
            return "medium"
        return "low"

    return "medium"


def _summarize_signal(signal_type: str, signal: Dict) -> str:
    """One-line summary for each signal."""
    score = signal.get("score", 0)
    direction = "bullish YES" if score > 0.1 else "bearish (favors NO)" if score < -0.1 else "neutral"

    if signal_type == "news":
        return f"{signal.get('article_count', 0)} articles, {direction} ({score:+.2f})"
    if signal_type == "sharp_trader":
        return f"{signal.get('consensus', 'NONE')} consensus, {signal.get('traders_positioned', 0)} positioned"
    if signal_type == "cross_platform":
        delta = signal.get("delta_from_polymarket", 0)
        return f"Delta from Polymarket: {delta:+.1%}, {signal.get('num_matches', 0)} matches"
    if signal_type == "base_rate":
        return f"Base rate: {signal.get('base_rate', 0.5):.0%} YES (n={signal.get('sample_size', 0)})"

    return f"Score: {score:+.2f}"


def _detect_contradictions(processed_signals: Dict) -> List[str]:
    """Flag signals that contradict each other."""
    contradictions = []
    signal_scores = {k: v["score"] for k, v in processed_signals.items()}

    # Check for strong disagreements
    for s1, score1 in signal_scores.items():
        for s2, score2 in signal_scores.items():
            if s1 >= s2:
                continue
            # If one is strongly positive and other strongly negative
            if (score1 > 0.3 and score2 < -0.3) or (score1 < -0.3 and score2 > 0.3):
                contradictions.append(
                    f"{s1} ({score1:+.2f}) contradicts {s2} ({score2:+.2f})"
                )

    return contradictions


def _assess_confidence(bundle: Dict) -> str:
    """
    Overall confidence assessment.
    High: multiple signals agree, no contradictions, good data quality
    Medium: some agreement, minor contradictions
    Low: signals contradict, poor data quality
    """
    signals = bundle.get("signals", {})
    contradictions = bundle.get("contradictions", [])

    if not signals:
        return "low"

    # Count high-quality signals
    high_quality = sum(1 for s in signals.values() if s["confidence_contribution"] == "high")
    any_quality = len(signals)

    # Check agreement
    scores = [s["score"] for s in signals.values()]
    all_positive = all(s > 0 for s in scores if abs(s) > 0.1)
    all_negative = all(s < 0 for s in scores if abs(s) > 0.1)
    agreement = all_positive or all_negative

    if high_quality >= 2 and agreement and not contradictions:
        return "high"
    elif contradictions or (not agreement and any_quality >= 3):
        return "low"
    else:
        return "medium"


def build_mirofish_context(market: Dict, bundle: Dict,
                           signal_formatters: Dict = None) -> str:
    """
    Build the complete MiroFish seed prompt from market data and signal bundle.
    Follows the prompt structure from the technical brief.
    """
    question = market.get("question", "Unknown")
    description = market.get("description", "")
    price = market.get("yes_price", 0.5)
    end_date = market.get("end_date", "")

    lines = []
    lines.append(f"MARKET QUESTION: {question}")
    if description:
        lines.append(f"DESCRIPTION: {description[:500]}")
    lines.append(f"CURRENT MARKET PRICE: {price:.3f} (i.e. the crowd thinks {price*100:.1f}% chance of YES)")
    lines.append(f"CLOSING DATE: {end_date}")
    lines.append("")

    # Add each signal's formatted output
    lines.append("RELEVANT CONTEXT:")
    lines.append("")

    # Include formatted signals if formatters are provided
    if signal_formatters:
        for sig_type, formatter in signal_formatters.items():
            raw_signal = bundle.get("_raw_signals", {}).get(sig_type)
            if raw_signal and formatter:
                lines.append(formatter(raw_signal))
                lines.append("")

    # Aggregate signal summary
    lines.append("SIGNAL SUMMARY:")
    for sig_type, sig_data in bundle.get("signals", {}).items():
        lines.append(f"  {sig_type}: {sig_data['summary']}")
    lines.append(f"  Overall: {bundle.get('aggregate_score', 0):+.3f} "
                 f"(confidence: {bundle.get('confidence', '?')})")

    if bundle.get("contradictions"):
        lines.append("")
        lines.append("CONTRADICTIONS DETECTED:")
        for c in bundle["contradictions"]:
            lines.append(f"  - {c}")

    lines.append("")
    lines.append("SIMULATION QUESTION:")
    lines.append(f"Based on these inputs and the emergent behavior of the simulated agents, "
                 f"what probability would you assign to this market resolving YES? "
                 f"What are the key dynamics driving opinion in the simulation?")

    return "\n".join(lines)


if __name__ == "__main__":
    # Demo with synthetic signals
    signals = {
        "news": {"score": 0.3, "article_count": 5, "avg_relevance": 0.6},
        "sharp_trader": {"score": 0.5, "consensus": "YES", "traders_positioned": 3},
        "cross_platform": {"score": 0.1, "delta_from_polymarket": 0.05, "num_matches": 2},
        "base_rate": {"score": -0.2, "base_rate": 0.4, "sample_size": 12},
    }

    bundle = preprocess_signals(signals)
    print(json.dumps(bundle, indent=2))
