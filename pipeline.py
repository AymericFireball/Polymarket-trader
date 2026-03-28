"""
Stage 6: Full Pipeline Orchestrator & Decision Gate
=====================================================
Wires all stages together:
  Market Selection → Signal Ingestion → Preprocessing →
  MiroFish Simulation → Calibration → Decision Gate → Output

Decision gate rules (from the brief):
- Minimum 8c delta between calibrated probability and market price
- Sharp trader agreement required (if data available)
- Confidence must be medium or higher
- All risk limits must be respected
"""

import os
import sys
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from db import init_db, get_conn, upsert_market
from config import (
    BANKROLL, CASH_RESERVE_PCT, MAX_SINGLE_POSITION_PCT,
    MIN_EDGE_CENTS, KELLY_FRACTION, DEFENSIVE_DRAWDOWN_PCT,
    HALT_DRAWDOWN_PCT,
)
from risk_manager import RiskManager, PortfolioState
from calibration import calibrate_probability, record_prediction


# ─── Decision Gate ────────────────────────────────────────────────

class DecisionGate:
    """
    Final filter: only pass trades that meet ALL criteria.
    This is the last line of defense against bad trades.
    """

    def __init__(self, min_edge_cents: int = 5,
                 require_sharp_agreement: bool = True,
                 min_confidence: str = "medium"):
        self.min_edge_cents = min_edge_cents
        self.require_sharp_agreement = require_sharp_agreement
        self.min_confidence = min_confidence
        self.confidence_levels = {"low": 0, "medium": 1, "high": 2}

    def evaluate(self, market: Dict, calibrated_prob: float,
                 signal_bundle: Dict, risk_check: Dict) -> Dict:
        """
        Evaluate whether a trade should be taken.

        Returns:
            {
                "pass": bool,
                "reasons": [str],  # Why it passed or failed
                "trade_signal": {...} or None,
            }
        """
        market_price = market.get("yes_price") or 0.5
        if isinstance(market_price, str):
            try:
                market_price = float(market_price)
            except (ValueError, TypeError):
                market_price = 0.5
        market_price = float(market_price)
        reasons = []
        failures = []

        # ── Check 1: Edge size ──
        edge = calibrated_prob - market_price
        edge_cents = round(abs(edge) * 100)
        side = "YES" if edge > 0 else "NO"

        if edge_cents >= self.min_edge_cents:
            reasons.append(f"Edge: {edge_cents}c {side} (min: {self.min_edge_cents}c)")
        else:
            failures.append(f"Edge too small: {edge_cents}c < {self.min_edge_cents}c minimum")

        # ── Check 2: Confidence ──
        confidence = signal_bundle.get("confidence", "low")
        conf_level = self.confidence_levels.get(confidence, 0)
        min_level = self.confidence_levels.get(self.min_confidence, 1)

        if conf_level >= min_level:
            reasons.append(f"Confidence: {confidence}")
        else:
            failures.append(f"Confidence too low: {confidence} < {self.min_confidence}")

        # ── Check 3: Sharp trader agreement ──
        sharp_signal = signal_bundle.get("signals", {}).get("sharp_trader", {})
        sharp_consensus = "NONE"

        if sharp_signal:
            sharp_score = sharp_signal.get("score", 0)
            if abs(sharp_score) < 0.01:
                sharp_consensus = "NONE"
            elif sharp_score > 0:
                sharp_consensus = "YES"
            else:
                sharp_consensus = "NO"

        if self.require_sharp_agreement:
            if sharp_consensus == "NONE":
                # No sharp data — waive requirement, proceed on edge alone
                reasons.append("Sharp traders: no data (waived)")
            elif (side == "YES" and sharp_consensus == "YES") or \
                 (side == "NO" and sharp_consensus == "NO"):
                reasons.append(f"Sharp traders agree: {sharp_consensus}")
            elif sharp_consensus == "MIXED":
                reasons.append("Sharp traders: mixed (caution)")
            else:
                failures.append(
                    f"Sharp traders disagree: consensus={sharp_consensus}, our side={side}"
                )

        # ── Check 4: Risk limits ──
        if risk_check.get("approved", False):
            reasons.append(f"Risk: approved (size=${risk_check.get('position_size', 0):.2f})")
        else:
            failures.append(f"Risk: {risk_check.get('reason', 'rejected')}")

        # ── Check 5: Contradictions ──
        contradictions = signal_bundle.get("contradictions", [])
        if contradictions:
            if conf_level >= 2:  # High confidence overrides
                reasons.append(f"Contradictions detected but high confidence overrides")
            else:
                failures.append(
                    f"Signal contradictions: {'; '.join(contradictions[:2])}"
                )

        # ── Final decision ──
        passed = len(failures) == 0

        result = {
            "pass": passed,
            "reasons": reasons if passed else failures,
            "edge_cents": edge_cents,
            "side": side,
            "confidence": confidence,
            "sharp_consensus": sharp_consensus,
            "contradictions": len(contradictions),
        }

        if passed:
            result["trade_signal"] = self._build_signal(
                market, calibrated_prob, edge, side,
                confidence, risk_check, signal_bundle
            )

        return result

    def _build_signal(self, market: Dict, calibrated_prob: float,
                      edge: float, side: str, confidence: str,
                      risk_check: Dict, signal_bundle: Dict) -> Dict:
        """Build a structured trade signal."""
        market_price = market.get("yes_price", 0.5)

        if side == "YES":
            entry_target = market_price  # Buy at current price or better
            stop_loss = max(0.01, market_price - 0.15)
            take_profit = min(0.99, calibrated_prob + 0.05)
        else:
            # Buying NO is equivalent to selling YES
            entry_target = 1 - market_price
            stop_loss = max(0.01, (1 - market_price) - 0.15)
            take_profit = min(0.99, (1 - calibrated_prob) + 0.05)

        return {
            "market": market.get("question", ""),
            "condition_id": market.get("condition_id", ""),
            "side": f"BUY {side}",
            "entry_target": round(entry_target, 4),
            "stop_loss": round(stop_loss, 4),
            "take_profit": round(take_profit, 4),
            "position_size": risk_check.get("position_size", 0),
            "position_pct": risk_check.get("position_pct", 0),
            "edge_cents": round(abs(edge) * 100),
            "calibrated_probability": calibrated_prob,
            "market_price": market_price,
            "confidence": confidence,
            "order_type": "GTC",
            "signal_summary": {
                k: v.get("summary", "")
                for k, v in signal_bundle.get("signals", {}).items()
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }


# ─── Pipeline Orchestrator ───────────────────────────────────────

class Pipeline:
    """
    Full prediction pipeline orchestrator.

    Runs all 6 stages in sequence for a given market:
    1. Market data (already fetched)
    2. Signal ingestion (news, cross-platform, sharp traders, base rate)
    3. Signal preprocessing (normalize, weight, detect contradictions)
    4. MiroFish simulation (if available)
    5. Calibration (shrinkage, Platt scaling)
    6. Decision gate (edge, confidence, risk checks)
    """

    def __init__(self, risk_manager: RiskManager = None):
        init_db()
        if risk_manager:
            self.risk_manager = risk_manager
        else:
            state = PortfolioState(bankroll=BANKROLL, peak_bankroll=BANKROLL)
            self.risk_manager = RiskManager(state)
        self.decision_gate = DecisionGate(
            min_edge_cents=MIN_EDGE_CENTS,
            require_sharp_agreement=True,
            min_confidence="medium",
        )
        from signal_fusion import SignalFusionEngine
        self._fusion = SignalFusionEngine()

    def analyze_market(self, market: Dict,
                       my_probability: float = None,
                       run_mirofish: bool = False) -> Dict:
        """
        Run full pipeline for a single market.

        Args:
            market: Market dict with question, yes_price, condition_id, etc.
            my_probability: Override probability estimate (skip signals)
            run_mirofish: Whether to run MiroFish simulation

        Returns:
            Full analysis result with trade signal (if applicable)
        """
        question = market.get("question", "Unknown")
        market_price = market.get("yes_price") or market.get("outcomePrices") or 0.5
        if isinstance(market_price, str):
            try:
                market_price = float(market_price)
            except (ValueError, TypeError):
                market_price = 0.5
        market_price = float(market_price) if market_price else 0.5
        market_type = market.get("market_type", "other")
        condition_id = market.get("condition_id", "")

        result = {
            "market": question,
            "condition_id": condition_id,
            "market_price": market_price,
            "market_type": market_type,
            "stages": {},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # ── Stage 2: Signal Ingestion ──
        signals = {}

        if my_probability is None:
            signals = self._ingest_signals(market)
            result["stages"]["signals"] = {
                k: {"score": v.get("score", 0), "source": v.get("source", "")}
                for k, v in signals.items()
            }

        # ── Stage 3: Preprocessing (signal fusion) ──
        if signals:
            bundle = self._fusion.fuse(signals, market_type, market_price=market_price)
            result["stages"]["preprocessing"] = {
                "aggregate_score": bundle.get("aggregate_score"),
                "confidence": bundle.get("confidence"),
                "confidence_score": bundle.get("confidence_score"),
                "contradictions": bundle.get("contradictions", []),
                "signal_count": bundle.get("signal_count"),
                "profile_used": bundle.get("profile_used"),
            }
        else:
            bundle = {
                "signals": {},
                "aggregate_score": 0,
                "confidence": "medium" if my_probability else "low",
                "confidence_score": 0.5 if my_probability else 0.2,
                "contradictions": [],
                "fused_probability": None,
            }

        # ── Stage 4: MiroFish (optional) ──
        mirofish_result = None
        if run_mirofish and signals:
            mirofish_result = self._run_mirofish(market, bundle, market_type)
            result["stages"]["mirofish"] = {
                "status": mirofish_result.get("status"),
                "sim_probability": mirofish_result.get("sim_probability"),
                "confidence": mirofish_result.get("confidence"),
                "dissent_flag": mirofish_result.get("dissent_flag"),
            }
            # Re-fuse with MiroFish result for adaptive blending
            if signals and mirofish_result.get("sim_probability") is not None:
                bundle = self._fusion.fuse(
                    signals, market_type,
                    mirofish_result=mirofish_result,
                    market_price=market_price,
                )
                result["stages"]["mirofish"]["blend_ratio"] = bundle.get("mirofish_blend_ratio")
                result["stages"]["mirofish"]["strong_disagreement"] = bundle.get("strong_disagreement")

        # ── Compute raw probability ──
        if my_probability is not None:
            raw_prob = my_probability
        elif bundle.get("fused_probability") is not None:
            raw_prob = bundle["fused_probability"]
        else:
            agg_score = bundle.get("aggregate_score", 0)
            raw_prob = market_price + agg_score * 0.15
            raw_prob = max(0.01, min(0.99, raw_prob))

        result["raw_probability"] = round(raw_prob, 4)

        # ── Stage 5: Calibration ──
        cal_result = calibrate_probability(raw_prob, market_type)
        calibrated = cal_result["calibrated"]
        result["calibrated_probability"] = calibrated
        result["stages"]["calibration"] = cal_result

        # ── Stage 6: Decision Gate ──
        # Risk check — pass original calibrated + market_price so
        # kelly_size determines the correct side internally
        risk_check = self._check_risk(
            calibrated, market_price,
            bundle.get("confidence", "medium"),
            market.get("narrative_tag", market_type),
        )

        gate_result = self.decision_gate.evaluate(
            market, calibrated, bundle, risk_check
        )
        result["decision"] = gate_result
        result["confidence_score"] = bundle.get("confidence_score", 0.5)

        # Record prediction for calibration tracking
        if gate_result.get("pass"):
            try:
                record_prediction(
                    condition_id, calibrated, raw_prob,
                    signals_used={k: v.get("score") for k, v in signals.items()}
                )
            except Exception:
                pass  # Don't let DB errors block pipeline

        return result

    def _ingest_signals(self, market: Dict) -> Dict:
        """Run all signal modules. Each one is independent and fault-tolerant."""
        signals = {}
        question = market.get("question", "")
        market_price = market.get("yes_price", 0.5)

        # News signal
        try:
            from signals.news import get_news_signal
            signals["news"] = get_news_signal(question)
        except Exception as e:
            signals["news"] = {"score": 0, "error": str(e)}

        # Cross-platform signal
        try:
            from signals.cross_platform import get_cross_platform_signal
            signals["cross_platform"] = get_cross_platform_signal(question, market_price)
        except Exception as e:
            signals["cross_platform"] = {"score": 0, "error": str(e)}

        # Sharp trader signal
        try:
            from signals.sharp_traders import get_sharp_trader_signal
            token_ids = market.get("token_ids", [])
            if len(token_ids) >= 2:
                signals["sharp_trader"] = get_sharp_trader_signal(token_ids)
            else:
                signals["sharp_trader"] = {"score": 0, "consensus": "NONE",
                                           "traders_positioned": 0}
        except Exception as e:
            signals["sharp_trader"] = {"score": 0, "error": str(e)}

        # Base rate signal
        try:
            from signals.base_rate import get_base_rate_signal
            signals["base_rate"] = get_base_rate_signal(
                question, market.get("market_type")
            )
        except Exception as e:
            signals["base_rate"] = {"score": 0, "error": str(e)}

        return signals

    def _run_mirofish(self, market: Dict, bundle: Dict,
                      market_type: str) -> Dict:
        """Run MiroFish simulation if available."""
        try:
            from mirofish_wrapper import run_mirofish_prediction
            from preprocessor import build_mirofish_context
            from signals.news import format_for_mirofish as news_fmt
            from signals.cross_platform import format_for_mirofish as xp_fmt
            from signals.sharp_traders import format_for_mirofish as sharp_fmt
            from signals.base_rate import format_for_mirofish as base_fmt

            context_text = build_mirofish_context(
                market, bundle,
                signal_formatters={
                    "news": news_fmt,
                    "cross_platform": xp_fmt,
                    "sharp_trader": sharp_fmt,
                    "base_rate": base_fmt,
                }
            )
            return run_mirofish_prediction(
                market, context_text, market_type,
                num_agents=500, num_rounds=5
            )
        except Exception as e:
            return {"status": "error", "sim_probability": None, "error": str(e)}

    def _check_risk(self, my_prob: float, market_price: float,
                    confidence: str, narrative_tag: str) -> Dict:
        """Run risk manager checks."""
        try:
            # RiskManager.kelly_size expects confidence as "Low"/"Medium"/"High"
            conf_map = {"low": "Low", "medium": "Medium", "high": "High"}
            conf_str = conf_map.get(confidence.lower(), "Medium") if isinstance(confidence, str) else "Medium"

            sizing = self.risk_manager.kelly_size(my_prob, market_price, conf_str)
            corr_check = self.risk_manager.check_correlation(narrative_tag)

            pos_size = sizing.get("position_size_usd", 0)
            bankroll = self.risk_manager.portfolio.bankroll

            if not sizing.get("passes_risk_checks", False):
                checks = sizing.get("risk_checks", [])
                return {"approved": False,
                        "reason": "; ".join(checks) if checks else "Risk check failed",
                        "position_size": 0, "position_pct": 0}

            if not corr_check.get("can_trade", True):
                return {"approved": False,
                        "reason": corr_check.get("reason", "Correlation limit"),
                        "position_size": 0, "position_pct": 0}

            return {
                "approved": True,
                "position_size": pos_size,
                "position_pct": pos_size / bankroll if bankroll > 0 else 0,
                "kelly_fraction": sizing.get("half_kelly_pct", 0),
                "reason": "Approved",
            }
        except Exception as e:
            return {"approved": False, "reason": str(e),
                    "position_size": 0, "position_pct": 0}

    def scan_and_analyze(self, top_n: int = 5,
                         my_estimates: Dict[str, float] = None) -> List[Dict]:
        """
        Full scan: find top markets, run pipeline on each, return trade signals.

        Args:
            top_n: Number of top markets to analyze
            my_estimates: Optional dict of {condition_id: my_probability}

        Returns:
            List of pipeline results, sorted by edge size
        """
        # Get scored markets from scanner
        try:
            from scanner import scan_markets
            scored = scan_markets(top_n=top_n * 2)  # Scan more, filter down
        except Exception:
            # Fallback: load from DB
            conn = get_conn()
            rows = conn.execute(
                "SELECT * FROM markets WHERE resolved=0 ORDER BY total_volume DESC LIMIT ?",
                (top_n * 2,)
            ).fetchall()
            conn.close()
            scored = [dict(r) for r in rows]

        results = []
        for market in scored[:top_n]:
            cid = market.get("condition_id", "")
            my_prob = (my_estimates or {}).get(cid)

            try:
                analysis = self.analyze_market(market, my_probability=my_prob)
                results.append(analysis)
            except Exception as e:
                results.append({
                    "market": market.get("question", ""),
                    "error": str(e),
                })

        # Sort: passing trades first, then by edge size
        results.sort(
            key=lambda r: (
                r.get("decision", {}).get("pass", False),
                r.get("decision", {}).get("edge_cents", 0),
            ),
            reverse=True,
        )

        return results


# ─── Output Formatting ───────────────────────────────────────────

def format_trade_signal(result: Dict) -> str:
    """Format a pipeline result as a trade signal string."""
    decision = result.get("decision", {})
    signal = decision.get("trade_signal")

    if not decision.get("pass"):
        return (
            f"PASS — {result.get('market', '?')}\n"
            f"  Market price: {result.get('market_price', 0):.2f}\n"
            f"  Our estimate: {result.get('calibrated_probability', 0):.2f}\n"
            f"  Reasons: {'; '.join(decision.get('reasons', ['Unknown']))}\n"
        )

    return (
        f"{'='*60}\n"
        f"TRADE SIGNAL\n"
        f"{'='*60}\n"
        f"\n"
        f"Market: {signal['market']}\n"
        f"Side: {signal['side']}\n"
        f"Entry target: ${signal['entry_target']:.4f}\n"
        f"Order type: {signal['order_type']}\n"
        f"Position size: ${signal['position_size']:.2f} "
        f"({signal['position_pct']:.1%} of bankroll)\n"
        f"Edge: {signal['edge_cents']}c\n"
        f"Confidence: {signal['confidence']}\n"
        f"\n"
        f"Our probability: {signal['calibrated_probability']:.1%}\n"
        f"Market price: {signal['market_price']:.1%}\n"
        f"Stop-loss: ${signal['stop_loss']:.4f}\n"
        f"Take-profit: ${signal['take_profit']:.4f}\n"
        f"\n"
        f"Signal breakdown:\n"
    ) + "\n".join(
        f"  {k}: {v}" for k, v in signal.get("signal_summary", {}).items()
    ) + "\n"


def format_portfolio_report(results: List[Dict],
                            risk_manager: RiskManager) -> str:
    """Generate a full portfolio report from pipeline results."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state = risk_manager.portfolio
    bankroll = state.bankroll if hasattr(state, 'bankroll') else float(state)

    lines = [
        f"PORTFOLIO REPORT — {now}",
        f"{'='*60}",
        f"",
        f"SUMMARY:",
        f"  Bankroll: ${bankroll:.2f}",
        f"  Deployed: ${state.deployed_capital:.2f}" if hasattr(state, 'deployed_capital') else "",
        f"  Cash: ${state.cash_available:.2f} ({state.cash_reserve_pct:.0%})" if hasattr(state, 'cash_available') else "",
        f"  Peak: ${state.peak_bankroll:.2f}" if hasattr(state, 'peak_bankroll') else "",
        f"  Drawdown: {state.drawdown_from_peak:.1%}" if hasattr(state, 'drawdown_from_peak') else "",
        f"  Mode: {'DEFENSIVE' if state.is_defensive_mode else 'HALTED' if state.is_halted else 'Normal'}" if hasattr(state, 'is_defensive_mode') else "",
        f"",
    ]
    lines = [l for l in lines if l != ""]  # Remove empty conditional lines
    lines.append("")

    # Trade signals
    passing = [r for r in results if r.get("decision", {}).get("pass")]
    failing = [r for r in results if not r.get("decision", {}).get("pass")]

    if passing:
        lines.append(f"ACTIONABLE SIGNALS ({len(passing)}):")
        lines.append("")
        for r in passing:
            lines.append(format_trade_signal(r))

    if failing:
        lines.append(f"\nPASSED ON ({len(failing)}):")
        for r in failing:
            lines.append(format_trade_signal(r))

    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Polymarket Trading Pipeline")
    parser.add_argument("--scan", action="store_true", help="Scan and analyze top markets")
    parser.add_argument("--top", type=int, default=5, help="Number of markets to analyze")
    parser.add_argument("--market", type=str, help="Analyze a specific market by condition ID")
    parser.add_argument("--prob", type=float, help="Your probability estimate (0-1)")
    parser.add_argument("--mirofish", action="store_true", help="Run MiroFish simulation")
    parser.add_argument("--report", action="store_true", help="Generate portfolio report")
    args = parser.parse_args()

    init_db()
    pipeline = Pipeline()

    if args.market:
        # Analyze specific market
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM markets WHERE condition_id=?", (args.market,)
        ).fetchone()
        conn.close()

        if not row:
            print(f"Market {args.market} not found in DB")
            sys.exit(1)

        market = dict(row)
        result = pipeline.analyze_market(
            market,
            my_probability=args.prob,
            run_mirofish=args.mirofish,
        )
        print(format_trade_signal(result))
        print(json.dumps(result, indent=2, default=str))

    elif args.scan or args.report:
        results = pipeline.scan_and_analyze(top_n=args.top)
        if args.report:
            print(format_portfolio_report(results, pipeline.risk_manager))
        else:
            for r in results:
                print(format_trade_signal(r))
                print()

    else:
        parser.print_help()
        print("\nExamples:")
        print("  python pipeline.py --scan --top 10")
        print("  python pipeline.py --market <condition_id> --prob 0.72")
        print("  python pipeline.py --scan --report")
