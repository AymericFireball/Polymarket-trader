"""
Stage 5: Calibration Layer
============================
Tracks prediction accuracy via Brier scores, applies shrinkage toward
0.5 until we have enough data, and provides hooks for Platt scaling.

Key rules from the brief:
- 15% shrinkage toward 0.5 until 50+ predictions resolved
- Brier score tracking per category and overall
- Platt scaling / isotonic regression once enough data exists
- Calibration curve analysis for continuous improvement
"""

import os
import sys
import json
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import get_conn, init_db


# ─── Brier Score ──────────────────────────────────────────────────

def brier_score(predicted: float, actual: float) -> float:
    """
    Brier score for a single prediction.
    predicted: probability assigned to YES (0-1)
    actual: 1 if YES resolved, 0 if NO resolved
    Lower is better. Perfect = 0, worst = 1, uninformed = 0.25.
    """
    return (predicted - actual) ** 2


def log_score(predicted: float, actual: float) -> float:
    """
    Logarithmic scoring rule (for additional calibration tracking).
    More sensitive to confident wrong predictions.
    """
    eps = 1e-6
    predicted = max(eps, min(1 - eps, predicted))
    if actual == 1:
        return -math.log(predicted)
    else:
        return -math.log(1 - predicted)


# ─── Shrinkage ────────────────────────────────────────────────────

def apply_shrinkage(raw_probability: float, num_resolved: int,
                    shrinkage_rate: float = 0.15,
                    min_predictions: int = 50) -> float:
    """
    Apply shrinkage toward 0.5 (maximum uncertainty) until we have
    enough resolved predictions to trust our calibration.

    From the brief: 15% shrinkage toward 0.5 until 50+ predictions.

    The shrinkage decays linearly as we accumulate resolved predictions:
    - At 0 resolved: full shrinkage (15% toward 0.5)
    - At 50 resolved: no shrinkage
    - Between: linear interpolation
    """
    if num_resolved >= min_predictions:
        return raw_probability

    # Fraction of shrinkage to apply (1.0 at 0 resolved, 0.0 at min_predictions)
    shrinkage_fraction = 1.0 - (num_resolved / min_predictions)
    effective_shrinkage = shrinkage_rate * shrinkage_fraction

    # Shrink toward 0.5
    calibrated = raw_probability * (1 - effective_shrinkage) + 0.5 * effective_shrinkage
    return round(calibrated, 4)


# ─── Platt Scaling ────────────────────────────────────────────────

def platt_scale(raw_probability: float, a: float, b: float) -> float:
    """
    Platt scaling: calibrated_p = 1 / (1 + exp(a * logit + b))
    where logit = log(p / (1-p))

    Parameters a and b are fitted from resolved predictions.
    When a=1.0, b=0.0, this is the identity function.
    """
    eps = 1e-6
    p = max(eps, min(1 - eps, raw_probability))
    logit = math.log(p / (1 - p))
    return 1.0 / (1.0 + math.exp(-(a * logit + b)))


def fit_platt_parameters(predictions: List[Tuple[float, float]]) -> Tuple[float, float]:
    """
    Fit Platt scaling parameters from (predicted, actual) pairs.

    Uses a simple gradient descent approach. For production, you'd use
    sklearn.linear_model.LogisticRegression or similar.

    Returns (a, b) parameters for platt_scale().
    """
    if len(predictions) < 20:
        return (1.0, 0.0)  # Identity — not enough data

    # Simple approach: compute bias and slope adjustment
    # Group predictions into bins and fit linear correction
    eps = 1e-6
    logits = []
    actuals = []
    for pred, actual in predictions:
        p = max(eps, min(1 - eps, pred))
        logits.append(math.log(p / (1 - p)))
        actuals.append(actual)

    n = len(logits)
    mean_logit = sum(logits) / n
    mean_actual = sum(actuals) / n

    # Linear regression: actual ~ a * logit + b (in logit space)
    # This is a simplified Platt scaling fit
    ss_logit = sum((l - mean_logit) ** 2 for l in logits)
    if ss_logit < eps:
        return (1.0, 0.0)

    ss_cross = sum((l - mean_logit) * (act - mean_actual)
                   for l, act in zip(logits, actuals))

    # Slope in probability space (approximate)
    platt_a = max(0.5, min(2.0, 1.0 + ss_cross / ss_logit))  # Bounded adjustment

    # Intercept: correct for mean bias
    target_logit = math.log(max(eps, mean_actual) / max(eps, 1 - mean_actual))
    current_logit = mean_logit * platt_a
    b = target_logit - current_logit
    b = max(-1.0, min(1.0, b))  # Bounded

    return (round(platt_a, 4), round(b, 4))


# ─── Calibration Analysis ────────────────────────────────────────

def get_calibration_data(category: str = None) -> List[Tuple[float, float]]:
    """
    Pull (predicted_probability, actual_outcome) pairs from the DB.
    Uses the actual schema: predictions.our_estimate joined with
    resolutions on condition_id. Resolution is "Yes"/"No" text.
    """
    conn = get_conn()
    query = """
        SELECT p.our_estimate, r.resolution
        FROM predictions p
        JOIN resolutions r ON p.condition_id = r.condition_id
        WHERE r.resolution IS NOT NULL AND p.our_estimate IS NOT NULL
    """
    params = []
    if category:
        query += " AND p.condition_id IN (SELECT condition_id FROM markets WHERE market_type=?)"
        params.append(category)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    results = []
    for row in rows:
        estimate = row[0]
        resolution = row[1]
        # Convert resolution text to 0/1
        if isinstance(resolution, str):
            actual = 1.0 if resolution.lower() == "yes" else 0.0
        else:
            actual = float(resolution) if resolution else 0.0
        results.append((estimate, actual))

    return results


def compute_calibration_stats(predictions: List[Tuple[float, float]],
                              num_bins: int = 10) -> Dict:
    """
    Compute calibration statistics including Brier score,
    calibration curve data, and reliability metrics.
    """
    if not predictions:
        return {
            "num_predictions": 0,
            "avg_brier": None,
            "avg_log_score": None,
            "calibration_bins": [],
            "overconfidence_score": None,
            "platt_params": (1.0, 0.0),
        }

    # Overall scores
    brier_scores = [brier_score(p, a) for p, a in predictions]
    log_scores_list = [log_score(p, a) for p, a in predictions]

    avg_brier = sum(brier_scores) / len(brier_scores)
    avg_log = sum(log_scores_list) / len(log_scores_list)

    # Calibration curve (binned)
    bins = []
    bin_width = 1.0 / num_bins
    for i in range(num_bins):
        lo = i * bin_width
        hi = (i + 1) * bin_width
        mid = (lo + hi) / 2

        in_bin = [(p, a) for p, a in predictions if lo <= p < hi]
        if not in_bin:
            continue

        avg_predicted = sum(p for p, _ in in_bin) / len(in_bin)
        avg_actual = sum(a for _, a in in_bin) / len(in_bin)
        bins.append({
            "bin_center": round(mid, 2),
            "avg_predicted": round(avg_predicted, 4),
            "avg_actual": round(avg_actual, 4),
            "count": len(in_bin),
            "deviation": round(avg_actual - avg_predicted, 4),
        })

    # Overconfidence: are we too far from 0.5 on average?
    overconfidence = sum(
        abs(p - 0.5) - abs(a - 0.5)
        for p, a in predictions
    ) / len(predictions)

    # Fit Platt parameters
    platt_params = fit_platt_parameters(predictions)

    return {
        "num_predictions": len(predictions),
        "avg_brier": round(avg_brier, 4),
        "avg_log_score": round(avg_log, 4),
        "calibration_bins": bins,
        "overconfidence_score": round(overconfidence, 4),
        "platt_params": platt_params,
        "baseline_brier": 0.25,  # Uninformed baseline (always predict 0.5)
        "skill_score": round(1 - avg_brier / 0.25, 4) if avg_brier else None,
    }


def get_category_stats() -> Dict[str, Dict]:
    """Get calibration stats broken down by market category."""
    conn = get_conn()
    categories = conn.execute(
        "SELECT DISTINCT market_type FROM markets WHERE market_type IS NOT NULL"
    ).fetchall()
    conn.close()

    results = {}
    for (cat,) in categories:
        data = get_calibration_data(cat)
        if data:
            results[cat] = compute_calibration_stats(data)

    return results


# ─── DB Operations ────────────────────────────────────────────────

def record_prediction(condition_id: str, calibrated_prob: float,
                      raw_probability: float = None,
                      model_version: str = "v1",
                      signals_used: Dict = None) -> None:
    """Record a prediction in the database for later calibration."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO predictions (condition_id, our_estimate,
                                  sim_probability_raw, sim_probability_cal,
                                  signal_bundle, predicted_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        condition_id,
        calibrated_prob,
        raw_probability or calibrated_prob,
        calibrated_prob,
        json.dumps(signals_used or {}),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def record_resolution(condition_id: str, resolution: str,
                      final_price: float = None) -> None:
    """Record a market resolution for calibration scoring."""
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO resolutions
        (condition_id, resolution, final_price, resolved_at)
        VALUES (?, ?, ?, ?)
    """, (
        condition_id,
        resolution,
        final_price,
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


# ─── Full Calibration Pipeline ───────────────────────────────────

def calibrate_probability(raw_probability: float,
                          category: str = None) -> Dict:
    """
    Full calibration pipeline for a raw probability estimate.

    Steps:
    1. Get current calibration data from DB
    2. Apply shrinkage if < 50 resolved predictions
    3. Apply Platt scaling if enough data exists
    4. Return calibrated probability with metadata

    Returns:
        {
            "raw": float,
            "calibrated": float,
            "adjustments": [...],
            "num_resolved": int,
            "avg_brier": float or None,
            "platt_params": (a, b),
        }
    """
    adjustments = []
    calibrated = raw_probability

    # Get resolved prediction data
    cal_data = get_calibration_data(category)
    num_resolved = len(cal_data)

    # Step 1: Shrinkage toward 0.5
    shrunk = apply_shrinkage(calibrated, num_resolved)
    if abs(shrunk - calibrated) > 0.001:
        adjustments.append({
            "type": "shrinkage",
            "before": calibrated,
            "after": shrunk,
            "reason": f"Shrinkage applied ({num_resolved}/{50} resolved predictions)",
        })
        calibrated = shrunk

    # Step 2: Platt scaling (only with enough data)
    platt_params = (1.0, 0.0)
    if num_resolved >= 30:
        platt_params = fit_platt_parameters(cal_data)
        platt_adjusted = platt_scale(calibrated, platt_params[0], platt_params[1])

        if abs(platt_adjusted - calibrated) > 0.005:
            adjustments.append({
                "type": "platt_scaling",
                "before": calibrated,
                "after": round(platt_adjusted, 4),
                "params": {"a": platt_params[0], "b": platt_params[1]},
                "reason": f"Platt scaling from {num_resolved} resolved predictions",
            })
            calibrated = platt_adjusted

    # Compute current Brier score if we have data
    stats = compute_calibration_stats(cal_data) if cal_data else {}

    return {
        "raw": raw_probability,
        "calibrated": round(calibrated, 4),
        "adjustments": adjustments,
        "num_resolved": num_resolved,
        "avg_brier": stats.get("avg_brier"),
        "skill_score": stats.get("skill_score"),
        "platt_params": platt_params,
    }


# ─── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()

    print("=== Calibration Layer Status ===\n")

    # Overall stats
    cal_data = get_calibration_data()
    stats = compute_calibration_stats(cal_data)

    print(f"Total resolved predictions: {stats['num_predictions']}")
    if stats['avg_brier'] is not None:
        print(f"Average Brier score: {stats['avg_brier']:.4f} (baseline: 0.25)")
        print(f"Skill score: {stats['skill_score']:.4f} (>0 = better than coin flip)")
        print(f"Overconfidence: {stats['overconfidence_score']:+.4f}")
    else:
        print("No resolved predictions yet — calibration data will accumulate over time.")

    print(f"\nShrinkage active: {'Yes (< 50 resolved)' if stats['num_predictions'] < 50 else 'No (enough data)'}")
    print(f"Platt scaling: {'Active' if stats['num_predictions'] >= 30 else 'Inactive (need 30+ resolved)'}")

    # Demo calibration
    print("\n--- Demo: Calibrating raw probability 0.72 ---")
    result = calibrate_probability(0.72)
    print(f"Raw:        {result['raw']:.4f}")
    print(f"Calibrated: {result['calibrated']:.4f}")
    for adj in result['adjustments']:
        print(f"  {adj['type']}: {adj['before']:.4f} → {adj['after']:.4f} ({adj['reason']})")

    # Category breakdown
    cat_stats = get_category_stats()
    if cat_stats:
        print("\n--- Category Breakdown ---")
        for cat, s in cat_stats.items():
            print(f"  {cat}: n={s['num_predictions']}, Brier={s['avg_brier']}")
