"""
Signal Fusion Engine
====================
Replaces the simple fixed-weight average in preprocessor.py with an adaptive
engine that adjusts weights by market type, applies time-decay to all signals,
blends MiroFish results adaptively based on agreement, and produces a
continuous 0-1 confidence score.
"""

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ─── Weight Profiles ──────────────────────────────────────────────────────────
# Weights per market type × signal source (must sum to 1.0 within each profile,
# but are normalized at runtime so they don't need to be exact).

WEIGHT_PROFILES: Dict[str, Dict[str, float]] = {
    "politics":  {"sharp_trader": 0.30, "news": 0.20, "base_rate": 0.20, "cross_platform": 0.10, "mirofish": 0.20},
    "crypto":    {"sharp_trader": 0.20, "news": 0.30, "base_rate": 0.15, "cross_platform": 0.15, "mirofish": 0.20},
    "sports":    {"sharp_trader": 0.15, "news": 0.15, "base_rate": 0.35, "cross_platform": 0.15, "mirofish": 0.20},
    "science":   {"sharp_trader": 0.25, "news": 0.20, "base_rate": 0.20, "cross_platform": 0.15, "mirofish": 0.20},
    "default":   {"sharp_trader": 0.25, "news": 0.20, "base_rate": 0.20, "cross_platform": 0.15, "mirofish": 0.20},
}

# Alias common market-type strings to canonical keys
_PROFILE_ALIASES: Dict[str, str] = {
    "political": "politics",
    "election": "politics",
    "cryptocurrency": "crypto",
    "bitcoin": "crypto",
    "sport": "sports",
    "tech": "science",
    "technology": "science",
    "other": "default",
}

# ─── Time-Decay Half-Lives (hours) ───────────────────────────────────────────
# decay_multiplier = 0.5 ** (age_hours / half_life)
# Missing timestamp → no decay (multiplier = 1.0)

DECAY_HALF_LIVES: Dict[str, float] = {
    "news":           12.0,   # Articles age fast
    "sharp_trader":   48.0,   # Whale positions linger for ~2 days
    "cross_platform": 24.0,   # Prediction market prices drift daily
    "base_rate":     720.0,   # Historical comps are rarely stale (30 days)
    "mirofish":        6.0,   # Sim result should be fresh
}

# MiroFish confidence string → numeric multiplier on MiroFish blend weight
_MIROFISH_CONF_FACTOR: Dict[str, float] = {
    "high": 1.0,
    "medium": 0.8,
    "low": 0.5,
    "none": 0.0,
}


# ─── Fusion Engine ────────────────────────────────────────────────────────────

class SignalFusionEngine:
    """
    Adaptive signal fusion for Polymarket predictions.

    Usage:
        engine = SignalFusionEngine()
        bundle = engine.fuse(signals, market_type, mirofish_result, market_price)
    """

    def fuse(
        self,
        signals: Dict[str, Dict],
        market_type: str,
        mirofish_result: Optional[Dict] = None,
        market_price: float = 0.5,
    ) -> Dict:
        """
        Fuse all signals into a single probability estimate with confidence score.

        Args:
            signals: Raw signal dicts keyed by type
                     (news, sharp_trader, cross_platform, base_rate)
            market_type: Market category string (politics, crypto, sports, etc.)
            mirofish_result: Output dict from run_mirofish_prediction(), or None
            market_price: Current Polymarket YES price (0-1)

        Returns:
            Full bundle dict compatible with DecisionGate and pipeline.py
        """
        profile_key = self._resolve_profile(market_type)
        base_weights = WEIGHT_PROFILES[profile_key].copy()

        signal_details: Dict[str, Dict] = {}
        scores_weighted: List[tuple] = []  # (score, effective_weight)

        # ── Process each signal ───────────────────────────────────────────────
        for sig_type, signal in signals.items():
            if not signal or not isinstance(signal, dict):
                continue

            score = float(signal.get("score", 0.0))
            score = max(-1.0, min(1.0, score))

            base_w = base_weights.get(sig_type, 0.10)

            # Quality adjustment (mirrors preprocessor._get_signal_weight)
            quality_mult = self._quality_multiplier(sig_type, signal)

            # Time-decay adjustment
            decay_mult = self._decay_multiplier(sig_type, signal)

            effective_w = round(base_w * quality_mult * decay_mult, 5)

            signal_details[sig_type] = {
                "score": score,
                "base_weight": base_w,
                "quality_mult": quality_mult,
                "decay_mult": round(decay_mult, 4),
                "effective_weight": effective_w,
                "weighted_score": round(score * effective_w, 5),
                "quality": self._quality_label(sig_type, signal),
                "summary": self._summarize(sig_type, signal),
            }
            scores_weighted.append((score, effective_w))

        # ── Normalize weights across present signals ──────────────────────────
        total_w = sum(w for _, w in scores_weighted)
        if total_w > 0:
            scores_weighted = [(s, w / total_w) for s, w in scores_weighted]
            for sig_type in signal_details:
                signal_details[sig_type]["weight"] = round(
                    signal_details[sig_type]["effective_weight"] / total_w, 5
                )

        # ── Weighted aggregate score (-1 to +1) ───────────────────────────────
        aggregate_score = round(sum(s * w for s, w in scores_weighted), 5)

        # ── Signal probability (market_price adjusted by signal aggregate) ────
        signal_prob = market_price + aggregate_score * 0.15
        signal_prob = max(0.01, min(0.99, signal_prob))

        # ── Adaptive MiroFish blend ───────────────────────────────────────────
        sim_prob = None
        mirofish_ratio = 0.0
        strong_disagreement = False

        if mirofish_result and mirofish_result.get("sim_probability") is not None:
            sim_prob = float(mirofish_result["sim_probability"])
            mf_conf = (mirofish_result.get("confidence") or "none").lower()
            conf_factor = _MIROFISH_CONF_FACTOR.get(mf_conf, 0.5)

            # Age decay on MiroFish result
            mf_decay = self._decay_multiplier("mirofish", mirofish_result)

            # Agreement-based split
            diff = abs(sim_prob - signal_prob)
            if diff < 0.10:
                raw_mf_ratio = 0.70
            elif diff <= 0.25:
                raw_mf_ratio = 0.50
            else:
                raw_mf_ratio = 0.40
                strong_disagreement = True

            # Scale by confidence and freshness
            mirofish_ratio = raw_mf_ratio * conf_factor * mf_decay
            # Clamp so MiroFish never dominates if it aged or had low confidence
            mirofish_ratio = min(mirofish_ratio, 0.70)
            signal_ratio = 1.0 - mirofish_ratio

            fused_probability = mirofish_ratio * sim_prob + signal_ratio * signal_prob
        else:
            fused_probability = signal_prob

        fused_probability = round(max(0.01, min(0.99, fused_probability)), 5)

        # ── Confidence score (continuous 0-1) ─────────────────────────────────
        signal_scores = [s for s, _ in scores_weighted] if scores_weighted else []
        signal_agreement = self._agreement_score(signal_scores)
        data_completeness = len(signal_details) / max(len(WEIGHT_PROFILES["default"]) - 1, 1)  # exclude mirofish slot
        data_completeness = min(1.0, data_completeness)
        sample_quality = self._avg_quality_score(signal_details)
        historical_accuracy = 0.5  # Placeholder — Phase 6 fills from DB

        confidence_score = round(
            signal_agreement * 0.30
            + data_completeness * 0.25
            + sample_quality * 0.25
            + historical_accuracy * 0.20,
            4,
        )
        confidence_score = max(0.0, min(1.0, confidence_score))

        # ── Confidence label (backward compat) ───────────────────────────────
        if confidence_score >= 0.65:
            confidence = "high"
        elif confidence_score >= 0.40:
            confidence = "medium"
        else:
            confidence = "low"

        # ── Contradictions (reuse preprocessor logic) ─────────────────────────
        contradictions = self._detect_contradictions(signal_details)

        # ── Weights used (for logging/reporting) ──────────────────────────────
        weights_used = {k: v.get("weight", v.get("effective_weight", 0))
                        for k, v in signal_details.items()}

        return {
            # Core outputs
            "fused_probability": fused_probability,
            "confidence_score": confidence_score,
            "confidence": confidence,
            # Backward-compat keys read by pipeline.py / DecisionGate
            "aggregate_score": aggregate_score,
            "signal_count": len(signal_details),
            "signals": signal_details,
            "contradictions": contradictions,
            # Fusion metadata
            "market_type": market_type,
            "profile_used": profile_key,
            "weights_used": weights_used,
            "mirofish_blend_ratio": round(mirofish_ratio, 4),
            "signal_agreement": round(signal_agreement, 4),
            "data_completeness": round(data_completeness, 4),
            "sample_quality": round(sample_quality, 4),
            "strong_disagreement": strong_disagreement,
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_profile(self, market_type: str) -> str:
        """Map any market_type string to a canonical profile key."""
        if not market_type:
            return "default"
        key = market_type.lower().strip()
        key = _PROFILE_ALIASES.get(key, key)
        return key if key in WEIGHT_PROFILES else "default"

    def _quality_multiplier(self, sig_type: str, signal: Dict) -> float:
        """
        Adjust base weight downward if signal has poor underlying data.
        Mirrors the logic in preprocessor._get_signal_weight.
        """
        mult = 1.0
        if sig_type == "base_rate" and signal.get("sample_size", 0) < 5:
            mult = 0.5
        elif sig_type == "sharp_trader" and signal.get("traders_positioned", 0) < 2:
            mult = 0.5
        elif sig_type == "news" and signal.get("article_count", 0) < 3:
            mult = 0.7
        return mult

    def _decay_multiplier(self, sig_type: str, signal: Dict) -> float:
        """
        Compute time-decay weight multiplier.
        Looks for 'timestamp', 'fetched_at', 'ran_at', or 'completed_at' in signal.
        Returns 1.0 if no timestamp found (no penalty).
        """
        half_life = DECAY_HALF_LIVES.get(sig_type)
        if half_life is None:
            return 1.0

        ts_str = (
            signal.get("freshness_ts")   # primary key used by all signal generators
            or signal.get("timestamp")
            or signal.get("fetched_at")
            or signal.get("ran_at")
            or signal.get("completed_at")
        )
        if not ts_str:
            return 1.0

        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            age_hours = (now - ts).total_seconds() / 3600.0
            return 0.5 ** (age_hours / half_life)
        except (ValueError, TypeError, OverflowError):
            return 1.0

    def _quality_label(self, sig_type: str, signal: Dict) -> str:
        """String quality label for reporting (mirrors preprocessor._assess_signal_quality)."""
        if sig_type == "news":
            if signal.get("article_count", 0) >= 5 and signal.get("avg_relevance", 0) > 0.5:
                return "high"
            elif signal.get("article_count", 0) >= 2:
                return "medium"
            return "low"
        if sig_type == "sharp_trader":
            if signal.get("traders_positioned", 0) >= 3:
                return "high"
            elif signal.get("traders_positioned", 0) >= 1:
                return "medium"
            return "low"
        if sig_type == "cross_platform":
            return "medium" if signal.get("num_matches", 0) >= 2 else "low"
        if sig_type == "base_rate":
            if signal.get("sample_size", 0) >= 10:
                return "high"
            elif signal.get("sample_size", 0) >= 3:
                return "medium"
            return "low"
        return "medium"

    def _summarize(self, sig_type: str, signal: Dict) -> str:
        """One-line summary per signal (mirrors preprocessor._summarize_signal)."""
        score = signal.get("score", 0)
        direction = "bullish YES" if score > 0.1 else "bearish (favors NO)" if score < -0.1 else "neutral"
        if sig_type == "news":
            return f"{signal.get('article_count', 0)} articles, {direction} ({score:+.2f})"
        if sig_type == "sharp_trader":
            return f"{signal.get('consensus', 'NONE')} consensus, {signal.get('traders_positioned', 0)} positioned"
        if sig_type == "cross_platform":
            delta = signal.get("delta_from_polymarket", 0)
            return f"Delta from Polymarket: {delta:+.1%}, {signal.get('num_matches', 0)} matches"
        if sig_type == "base_rate":
            return f"Base rate: {signal.get('base_rate', 0.5):.0%} YES (n={signal.get('sample_size', 0)})"
        return f"Score: {score:+.2f}"

    def _agreement_score(self, scores: List[float]) -> float:
        """
        0-1 measure of how much signals agree.
        1.0 = all point same direction, 0.0 = maximally split.
        Uses 1 - normalized_std_dev of scores.
        """
        if not scores:
            return 0.5
        if len(scores) == 1:
            return 1.0
        n = len(scores)
        mean = sum(scores) / n
        variance = sum((s - mean) ** 2 for s in scores) / n
        std_dev = math.sqrt(variance)
        # Max possible std_dev for scores in [-1, 1] is 1.0
        return max(0.0, 1.0 - std_dev)

    def _avg_quality_score(self, signal_details: Dict) -> float:
        """Average quality across signals as a 0-1 float (high=1, medium=0.6, low=0.3)."""
        quality_map = {"high": 1.0, "medium": 0.6, "low": 0.3}
        if not signal_details:
            return 0.3
        scores = [quality_map.get(v.get("quality", "low"), 0.3)
                  for v in signal_details.values()]
        return sum(scores) / len(scores)

    def _detect_contradictions(self, signal_details: Dict) -> List[str]:
        """Flag pairs of signals with strong opposing scores."""
        contradictions = []
        items = list(signal_details.items())
        for i, (s1, v1) in enumerate(items):
            for s2, v2 in items[i + 1:]:
                score1, score2 = v1["score"], v2["score"]
                if (score1 > 0.3 and score2 < -0.3) or (score1 < -0.3 and score2 > 0.3):
                    contradictions.append(
                        f"{s1} ({score1:+.2f}) contradicts {s2} ({score2:+.2f})"
                    )
        return contradictions

    def compute_optimal_weights(
        self,
        conn,
        market_type: Optional[str] = None,
        lookback_days: int = 90,
        min_samples: int = 20,
    ) -> Dict:
        """
        Compute suggested weight adjustments from historical signal_accuracy data.

        Uses inverse-Brier weighting: signals with lower average Brier score get
        proportionally more weight. Falls back to the current WEIGHT_PROFILES default
        if there is insufficient data.

        Args:
            conn:           Open DB connection.
            market_type:    If given, tune weights for that profile only; otherwise
                            returns a mapping of {market_type: weights}.
            lookback_days:  How many days of history to include.
            min_samples:    Minimum rows needed per signal to update its weight.

        Returns:
            dict with keys:
              "profiles"    – {profile_key: {signal: weight, ...}} (normalized to sum=1)
              "sample_counts" – {profile_key: {signal: n}}
              "status"      – "updated" | "insufficient_data"
        """
        try:
            from db import get_signal_brier_by_type
        except ImportError:
            return {"status": "insufficient_data", "profiles": {}, "sample_counts": {}}

        signal_types = ["news", "sharp_trader", "base_rate", "cross_platform"]

        # Which market types to process
        if market_type:
            profile_key = self._resolve_profile(market_type)
            target_types = [profile_key]
        else:
            target_types = list(WEIGHT_PROFILES.keys())

        profiles: Dict[str, Dict[str, float]] = {}
        sample_counts: Dict[str, Dict[str, int]] = {}
        any_updated = False

        for mtype in target_types:
            row = get_signal_brier_by_type(
                conn, market_type=mtype, lookback_days=lookback_days
            )
            if not row or not row.get("n"):
                continue

            # row is a flat dict: {n, news_brier, sharp_brier, base_brier, xp_brier, ...}
            n_total = row["n"]
            # Map canonical signal names to their column names in the row
            _col = {
                "news": "news_brier",
                "sharp_trader": "sharp_brier",
                "base_rate": "base_brier",
                "cross_platform": "xp_brier",
            }
            brier_map = {
                sig: (row.get(col), n_total)
                for sig, col in _col.items()
            }

            base = WEIGHT_PROFILES.get(mtype, WEIGHT_PROFILES["default"]).copy()
            new_weights: Dict[str, float] = {}
            counts: Dict[str, int] = {}

            for sig in signal_types:
                if sig in brier_map:
                    avg_brier, n = brier_map[sig]
                    if n >= min_samples and avg_brier is not None and avg_brier > 0:
                        # Inverse-Brier: better signal → lower brier → higher weight
                        new_weights[sig] = 1.0 / avg_brier
                        counts[sig] = n
                    else:
                        # Insufficient data — keep base weight
                        new_weights[sig] = base.get(sig, 0.10)
                        counts[sig] = brier_map[sig][1] if sig in brier_map else 0
                else:
                    new_weights[sig] = base.get(sig, 0.10)
                    counts[sig] = 0

            # Keep MiroFish weight from base profile unchanged (not tracked separately)
            new_weights["mirofish"] = base.get("mirofish", 0.20)

            # Normalize so weights sum to 1.0
            total = sum(new_weights.values())
            if total > 0:
                new_weights = {k: round(v / total, 4) for k, v in new_weights.items()}

            profiles[mtype] = new_weights
            sample_counts[mtype] = counts
            any_updated = True

        return {
            "status": "updated" if any_updated else "insufficient_data",
            "profiles": profiles,
            "sample_counts": sample_counts,
        }


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import json

    engine = SignalFusionEngine()

    signals = {
        "news":           {"score": 0.3, "article_count": 5, "avg_relevance": 0.6},
        "sharp_trader":   {"score": 0.5, "consensus": "YES", "traders_positioned": 3},
        "cross_platform": {"score": 0.1, "delta_from_polymarket": 0.05, "num_matches": 2},
        "base_rate":      {"score": -0.2, "base_rate": 0.4, "sample_size": 12},
    }

    mf = {
        "sim_probability": 0.72,
        "confidence": "high",
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }

    result = engine.fuse(signals, "crypto", mirofish_result=mf, market_price=0.55)
    print(json.dumps(result, indent=2))
