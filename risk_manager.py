"""
Risk Management Module
=======================
Half-Kelly position sizing, drawdown tracking, and portfolio risk checks.
"""

from dataclasses import dataclass, field
from typing import List, Optional
from config import (
    BANKROLL, CASH_RESERVE_PCT, MAX_SINGLE_POSITION_PCT,
    MAX_CORRELATED_PCT, MIN_EDGE_CENTS, DEFENSIVE_MIN_EDGE_CENTS,
    KELLY_FRACTION, STOP_LOSS_CENTS, TAKE_PROFIT_THRESHOLD,
    DEFENSIVE_DRAWDOWN_PCT, HALT_DRAWDOWN_PCT,
)


@dataclass
class Position:
    market_id: str
    question: str
    side: str           # "YES" or "NO"
    entry_price: float
    quantity: float
    cost_basis: float
    current_price: float = 0.0
    narrative_tag: str = ""  # For correlation tracking
    thesis: str = ""
    invalidation: str = ""
    stop_loss: float = 0.0
    take_profit: float = 0.0

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.quantity

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.cost_basis == 0:
            return 0
        return self.unrealized_pnl / self.cost_basis * 100


@dataclass
class PortfolioState:
    bankroll: float = BANKROLL
    peak_bankroll: float = BANKROLL
    deployed_capital: float = 0.0
    positions: List[Position] = field(default_factory=list)
    closed_trades: list = field(default_factory=list)

    @property
    def cash_available(self) -> float:
        return self.bankroll - self.deployed_capital

    @property
    def cash_reserve_pct(self) -> float:
        return self.cash_available / self.bankroll if self.bankroll > 0 else 0

    @property
    def drawdown_from_peak(self) -> float:
        if self.peak_bankroll == 0:
            return 0
        return (self.peak_bankroll - self.bankroll) / self.peak_bankroll

    @property
    def is_defensive_mode(self) -> bool:
        return self.drawdown_from_peak >= DEFENSIVE_DRAWDOWN_PCT

    @property
    def is_halted(self) -> bool:
        return self.drawdown_from_peak >= HALT_DRAWDOWN_PCT


class RiskManager:
    """Enforces position sizing rules and portfolio risk limits."""

    def __init__(self, portfolio: Optional[PortfolioState] = None):
        self.portfolio = portfolio or PortfolioState()

    # ─── Half-Kelly Sizing ──────────────────────────────────────────

    def kelly_size(self, my_prob: float, market_price: float,
                   confidence: str = "Medium") -> dict:
        """
        Calculate Half-Kelly position size.

        Args:
            my_prob: Your estimated probability (0-1)
            market_price: Current market price (0-1), this is the cost per share
            confidence: "Low", "Medium", or "High"

        Returns:
            Dict with sizing details and whether the trade passes risk checks.
        """
        p = self.portfolio

        # Edge in cents
        edge_cents = round((my_prob - market_price) * 100, 1)
        abs_edge = abs(edge_cents)

        # Determine which side to buy
        if my_prob > market_price:
            side = "BUY YES"
            effective_price = market_price
            effective_prob = my_prob
        else:
            side = "BUY NO"
            effective_price = 1 - market_price
            effective_prob = 1 - my_prob
            edge_cents = round((effective_prob - effective_price) * 100, 1)
            abs_edge = abs(edge_cents)

        # Payout per dollar risked
        payout = (1 / effective_price) - 1 if effective_price > 0 else 0

        # Kelly calculation
        if payout <= 0:
            return self._reject("No positive payout available", edge_cents, side)

        kelly_edge = (effective_prob * payout) - (1 - effective_prob)
        if kelly_edge <= 0:
            return self._reject("Negative Kelly edge", edge_cents, side)

        kelly_pct = kelly_edge / payout
        half_kelly_pct = KELLY_FRACTION * kelly_pct

        # Confidence scaling
        confidence_multiplier = {"Low": 0.5, "Medium": 0.75, "High": 1.0}.get(confidence, 0.75)
        adjusted_pct = half_kelly_pct * confidence_multiplier

        # Calculate dollar size
        raw_size = adjusted_pct * p.bankroll
        max_size = MAX_SINGLE_POSITION_PCT * p.bankroll
        max_deployable = p.cash_available - (CASH_RESERVE_PCT * p.bankroll)

        position_size = min(raw_size, max_size, max(max_deployable, 0))
        position_size = round(max(position_size, 0), 2)

        # Quantity (shares)
        quantity = round(position_size / effective_price, 2) if effective_price > 0 else 0

        # ─── Risk checks ───────────────────────────────────────────

        min_edge = DEFENSIVE_MIN_EDGE_CENTS if p.is_defensive_mode else MIN_EDGE_CENTS
        checks = []

        if p.is_halted:
            checks.append("BLOCKED: Trading halted — drawdown exceeds 35%")
        if abs_edge < min_edge:
            checks.append(f"BLOCKED: Edge {abs_edge}c < minimum {min_edge}c")
        if position_size <= 0:
            checks.append("BLOCKED: No capital available (cash reserve constraint)")
        if p.cash_reserve_pct < CASH_RESERVE_PCT and position_size > 0:
            checks.append("WARNING: Cash reserve below 30% after this trade")

        passes = len([c for c in checks if c.startswith("BLOCKED")]) == 0

        return {
            "side": side,
            "edge_cents": edge_cents,
            "confidence": confidence,
            "kelly_pct": round(kelly_pct * 100, 2),
            "half_kelly_pct": round(half_kelly_pct * 100, 2),
            "adjusted_pct": round(adjusted_pct * 100, 2),
            "position_size_usd": position_size,
            "quantity": quantity,
            "entry_price": effective_price,
            "stop_loss": round(effective_price - (STOP_LOSS_CENTS / 100), 4),
            "take_profit": TAKE_PROFIT_THRESHOLD,
            "risk_checks": checks,
            "passes_risk_checks": passes,
            "defensive_mode": p.is_defensive_mode,
        }

    def _reject(self, reason: str, edge_cents: float, side: str) -> dict:
        return {
            "side": side,
            "edge_cents": edge_cents,
            "position_size_usd": 0,
            "quantity": 0,
            "passes_risk_checks": False,
            "risk_checks": [f"BLOCKED: {reason}"],
        }

    # ─── Correlation Check ──────────────────────────────────────────

    def check_correlation(self, narrative_tag: str) -> dict:
        """Check how much capital is already exposed to a narrative."""
        exposed = sum(
            pos.cost_basis for pos in self.portfolio.positions
            if pos.narrative_tag == narrative_tag
        )
        max_allowed = MAX_CORRELATED_PCT * self.portfolio.bankroll
        remaining = max(max_allowed - exposed, 0)
        return {
            "narrative": narrative_tag,
            "current_exposure_usd": round(exposed, 2),
            "max_allowed_usd": round(max_allowed, 2),
            "remaining_capacity_usd": round(remaining, 2),
            "at_limit": exposed >= max_allowed,
        }

    # ─── Portfolio Summary ──────────────────────────────────────────

    def portfolio_summary(self) -> dict:
        p = self.portfolio
        total_unrealized = sum(pos.unrealized_pnl for pos in p.positions)
        return {
            "bankroll": p.bankroll,
            "peak_bankroll": p.peak_bankroll,
            "deployed_capital": round(p.deployed_capital, 2),
            "deployed_pct": round(p.deployed_capital / p.bankroll * 100, 1) if p.bankroll > 0 else 0,
            "cash_available": round(p.cash_available, 2),
            "cash_reserve_pct": round(p.cash_reserve_pct * 100, 1),
            "num_positions": len(p.positions),
            "unrealized_pnl": round(total_unrealized, 2),
            "drawdown_from_peak": round(p.drawdown_from_peak * 100, 1),
            "defensive_mode": p.is_defensive_mode,
            "trading_halted": p.is_halted,
        }


# ─── Quick demo ─────────────────────────────────────────────────────

if __name__ == "__main__":
    rm = RiskManager()
    print("Portfolio:", rm.portfolio_summary())

    # Example: You think YES is 65% likely, market is at $0.55
    sizing = rm.kelly_size(my_prob=0.65, market_price=0.55, confidence="Medium")
    print(f"\nTrade sizing (65% vs market 55c):")
    for k, v in sizing.items():
        print(f"  {k}: {v}")
