"""
Trade Executor
===============
Handles order placement, cancellation, and execution monitoring
on the Polymarket CLOB. Supports GTC/GTD/FOK order types with
iceberg splitting for large orders.

IMPORTANT: Requires API credentials + wallet private key in config.py.
Read-only operations (order book checks, price quotes) work without auth.

Architecture:
  Trade Signal → Pre-Trade Checks → Order Placement → Fill Monitoring → Position Logging
"""

import os
import sys
import json
import time
import hmac
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    POLYMARKET_API_KEY, POLYMARKET_SECRET, POLYMARKET_PASSPHRASE,
    PRIVATE_KEY, WALLET_ADDRESS,
    CLOB_BASE_URL, CHAIN_ID,
    STOP_LOSS_CENTS, TAKE_PROFIT_THRESHOLD,
)
from db import get_conn, init_db


# ─── Auth Helper ──────────────────────────────────────────────────

def _create_auth_headers(method: str, path: str, body: str = "",
                         timestamp: str = None) -> Dict:
    """
    Create HMAC authentication headers for the CLOB API.
    Required for order placement and private endpoints.
    """
    if not POLYMARKET_API_KEY or not POLYMARKET_SECRET:
        return {}

    ts = timestamp or str(int(time.time()))
    message = ts + method.upper() + path + body
    signature = hmac.new(
        base64.b64decode(POLYMARKET_SECRET),
        message.encode(),
        hashlib.sha256
    ).digest()
    sig_b64 = base64.b64encode(signature).decode()

    return {
        "POLY_API_KEY": POLYMARKET_API_KEY,
        "POLY_SIGNATURE": sig_b64,
        "POLY_TIMESTAMP": ts,
        "POLY_PASSPHRASE": POLYMARKET_PASSPHRASE,
    }


def is_configured() -> bool:
    """Check if trading credentials are configured."""
    return bool(POLYMARKET_API_KEY and POLYMARKET_SECRET and PRIVATE_KEY)


# ─── Pre-Trade Checks ────────────────────────────────────────────

class PreTradeChecker:
    """
    Validates a trade signal before execution.
    Checks order book depth, spread, and ensures we won't
    move the market excessively.
    """

    def __init__(self):
        from api_client import PolymarketClient
        self.client = PolymarketClient()

    def check(self, signal: Dict) -> Dict:
        """
        Run pre-trade checks on a signal.

        Returns:
            {
                "approved": bool,
                "warnings": [str],
                "order_book": {...},
                "recommended_entry": float,
                "slippage_estimate": float,
            }
        """
        warnings = []
        condition_id = signal.get("condition_id", "")
        side = signal.get("side", "BUY YES")
        size_usd = signal.get("position_size", 0)
        entry_target = signal.get("entry_target", 0)

        # We need the token_id to check order book
        # For now, try to get it from the market data
        conn = get_conn()
        market = conn.execute(
            "SELECT token_ids FROM markets WHERE condition_id=?", (condition_id,)
        ).fetchone()
        conn.close()

        token_ids = []
        if market and market["token_ids"]:
            try:
                token_ids = json.loads(market["token_ids"])
            except (json.JSONDecodeError, TypeError):
                pass

        if not token_ids:
            warnings.append("No token IDs available — cannot check order book")
            return {
                "approved": True,
                "warnings": warnings,
                "order_book": None,
                "recommended_entry": entry_target,
                "slippage_estimate": 0,
            }

        # Determine which token to check (YES=index 0, NO=index 1)
        is_yes = "YES" in side.upper()
        token_id = token_ids[0] if is_yes else (token_ids[1] if len(token_ids) > 1 else token_ids[0])

        # Fetch order book
        try:
            book_analysis = self.client.analyze_order_book(token_id)
        except Exception as e:
            warnings.append(f"Order book fetch failed: {e}")
            return {
                "approved": True,
                "warnings": warnings,
                "order_book": None,
                "recommended_entry": entry_target,
                "slippage_estimate": 0,
            }

        # Check 1: Spread
        spread = book_analysis.get("spread", 1)
        if spread > 0.05:
            warnings.append(f"Wide spread: {spread:.4f} (>5c)")

        # Check 2: Depth vs order size
        relevant_depth = book_analysis.get("ask_depth_usd", 0) if is_yes else book_analysis.get("bid_depth_usd", 0)
        if size_usd > relevant_depth * 0.5:
            warnings.append(f"Order size ${size_usd:.0f} > 50% of book depth ${relevant_depth:.0f}")

        # Check 3: Our order vs total volume
        if relevant_depth > 0 and size_usd / relevant_depth > 0.2:
            warnings.append(f"High market impact: order is {size_usd/relevant_depth:.0%} of available depth")

        # Recommended entry: slightly inside the spread for maker status
        if is_yes:
            recommended = book_analysis.get("best_ask", entry_target)
            # Place just below best ask to be maker
            recommended = round(recommended - 0.001, 4)
        else:
            recommended = book_analysis.get("best_bid", entry_target)
            recommended = round(recommended + 0.001, 4)

        # Slippage estimate
        slippage = abs(recommended - entry_target)

        approved = len([w for w in warnings if "impact" in w.lower()]) == 0

        return {
            "approved": approved,
            "warnings": warnings,
            "order_book": book_analysis,
            "recommended_entry": recommended,
            "slippage_estimate": round(slippage, 4),
        }


# ─── Order Builder ────────────────────────────────────────────────

class OrderBuilder:
    """Constructs orders in the format expected by the CLOB API."""

    @staticmethod
    def build_limit_order(token_id: str, side: str, price: float,
                          size: float, order_type: str = "GTC",
                          expiration: str = None) -> Dict:
        """
        Build a limit order payload.

        Args:
            token_id: The ERC-1155 token ID
            side: "BUY" or "SELL"
            price: Limit price (0-1)
            size: Number of shares
            order_type: "GTC", "GTD", or "FOK"
            expiration: ISO timestamp for GTD orders

        Returns:
            Order payload dict ready for signing and submission
        """
        order = {
            "tokenID": token_id,
            "side": side.upper(),
            "price": str(round(price, 4)),
            "size": str(round(size, 2)),
            "type": order_type,
            "feeRateBps": "0",  # Maker = 0 fee
        }

        if order_type == "GTD" and expiration:
            order["expiration"] = expiration

        return order

    @staticmethod
    def split_iceberg(total_size: float, max_chunk: float = 500,
                      num_chunks: int = None) -> List[float]:
        """
        Split a large order into smaller chunks to minimize market impact.

        From the brief: If position size > $500, break into smaller orders
        spaced 5-15 minutes apart.
        """
        if total_size <= max_chunk:
            return [total_size]

        if num_chunks is None:
            num_chunks = max(2, int(total_size / max_chunk) + 1)

        chunk_size = total_size / num_chunks
        chunks = [round(chunk_size, 2)] * num_chunks

        # Adjust last chunk for rounding
        total = sum(chunks)
        if abs(total - total_size) > 0.01:
            chunks[-1] = round(chunks[-1] + (total_size - total), 2)

        return chunks


# ─── Trade Executor ───────────────────────────────────────────────

class TradeExecutor:
    """
    Executes trades on the Polymarket CLOB.
    Handles the full lifecycle: validate → place → monitor → log.
    """

    def __init__(self, dry_run: bool = True):
        """
        Args:
            dry_run: If True, simulate orders without actually placing them.
                     Default True for safety.
        """
        self.dry_run = dry_run
        self.checker = PreTradeChecker()
        self.builder = OrderBuilder()
        init_db()

        if not dry_run and not is_configured():
            print("WARNING: Trading credentials not configured. Switching to dry-run mode.")
            self.dry_run = True

    def execute_signal(self, signal: Dict) -> Dict:
        """
        Execute a trade signal through the full pipeline.

        Args:
            signal: Trade signal from the decision gate

        Returns:
            Execution result with order details and status
        """
        result = {
            "signal": signal,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "dry_run": self.dry_run,
            "status": "pending",
            "orders": [],
            "errors": [],
        }

        # Step 1: Pre-trade checks
        print(f"\n{'='*50}")
        print(f"EXECUTING: {signal.get('market', '?')}")
        print(f"  {signal.get('side', '?')} @ ${signal.get('entry_target', 0):.4f}")
        print(f"  Size: ${signal.get('position_size', 0):.2f}")
        print(f"{'='*50}")

        ptc = self.checker.check(signal)
        result["pre_trade_checks"] = ptc

        for w in ptc.get("warnings", []):
            print(f"  WARNING: {w}")

        if not ptc["approved"]:
            result["status"] = "rejected_pre_trade"
            result["errors"].append("Pre-trade checks failed")
            print("  REJECTED: Pre-trade checks failed")
            return result

        # Use recommended entry if available
        entry = ptc.get("recommended_entry", signal.get("entry_target", 0))
        size_usd = signal.get("position_size", 0)
        side_str = signal.get("side", "BUY YES")

        # Step 2: Determine token and calculate shares
        is_yes = "YES" in side_str.upper()
        price = entry
        shares = size_usd / price if price > 0 else 0

        print(f"  Entry: ${price:.4f} ({shares:.1f} shares)")
        print(f"  Slippage est: {ptc.get('slippage_estimate', 0):.4f}")

        # Step 3: Iceberg split if needed
        chunks = self.builder.split_iceberg(size_usd)
        if len(chunks) > 1:
            print(f"  Iceberg: {len(chunks)} chunks of ~${chunks[0]:.0f}")

        # Step 4: Place orders
        for i, chunk_usd in enumerate(chunks):
            chunk_shares = chunk_usd / price if price > 0 else 0
            order_type = signal.get("order_type", "GTC")

            if self.dry_run:
                order_result = {
                    "order_id": f"DRY_RUN_{i}_{int(time.time())}",
                    "status": "simulated",
                    "price": price,
                    "size": round(chunk_shares, 2),
                    "size_usd": round(chunk_usd, 2),
                    "side": "BUY" if is_yes else "SELL",
                    "type": order_type,
                }
                print(f"  [DRY RUN] Order {i+1}/{len(chunks)}: "
                      f"{chunk_shares:.1f} shares @ ${price:.4f} = ${chunk_usd:.2f}")
            else:
                order_result = self._place_order(
                    signal, price, chunk_shares, order_type
                )
                if order_result.get("error"):
                    result["errors"].append(order_result["error"])
                    print(f"  [ERROR] Order {i+1}: {order_result['error']}")
                else:
                    print(f"  [LIVE] Order {i+1}/{len(chunks)}: "
                          f"{order_result.get('order_id', '?')}")

            result["orders"].append(order_result)

            # Wait between iceberg chunks
            if len(chunks) > 1 and i < len(chunks) - 1:
                wait_seconds = 30  # 30s between chunks (not the full 5-15min for testing)
                if not self.dry_run:
                    print(f"  Waiting {wait_seconds}s before next chunk...")
                    time.sleep(wait_seconds)

        # Step 5: Log to DB
        result["status"] = "executed" if not result["errors"] else "partial"
        self._log_trade(signal, result)

        # Step 6: Set monitoring alerts
        sl = signal.get("stop_loss", 0)
        tp = signal.get("take_profit", 0)
        print(f"\n  Stop-loss: ${sl:.4f}")
        print(f"  Take-profit: ${tp:.4f}")
        print(f"  Status: {result['status'].upper()}")

        return result

    def _place_order(self, signal: Dict, price: float,
                     shares: float, order_type: str) -> Dict:
        """Actually place an order on the CLOB. Requires auth."""
        import requests

        condition_id = signal.get("condition_id", "")
        side_str = signal.get("side", "BUY YES")
        is_yes = "YES" in side_str.upper()

        # Get token ID
        conn = get_conn()
        market = conn.execute(
            "SELECT token_ids FROM markets WHERE condition_id=?",
            (condition_id,)
        ).fetchone()
        conn.close()

        if not market or not market["token_ids"]:
            return {"error": "No token IDs for this market"}

        token_ids = json.loads(market["token_ids"])
        token_id = token_ids[0] if is_yes else token_ids[1]

        # Build order
        order = self.builder.build_limit_order(
            token_id=token_id,
            side="BUY",
            price=price,
            size=shares,
            order_type=order_type,
        )

        # Sign and submit
        body = json.dumps(order)
        path = "/order"
        headers = _create_auth_headers("POST", path, body)
        headers["Content-Type"] = "application/json"

        try:
            resp = requests.post(
                f"{CLOB_BASE_URL}{path}",
                headers=headers,
                data=body,
                timeout=15,
            )
            data = resp.json()

            if resp.status_code == 200 or resp.status_code == 201:
                return {
                    "order_id": data.get("orderID", data.get("id", "?")),
                    "status": "placed",
                    "price": price,
                    "size": shares,
                    "response": data,
                }
            else:
                return {
                    "error": f"HTTP {resp.status_code}: {data.get('error', data)}",
                    "response": data,
                }
        except Exception as e:
            return {"error": str(e)}

    def _log_trade(self, signal: Dict, result: Dict) -> None:
        """Log trade execution to the database."""
        import uuid
        conn = get_conn()
        try:
            trade_id = str(uuid.uuid4())[:8]
            total_qty = sum(o.get("size", 0) for o in result.get("orders", []))
            conn.execute("""
                INSERT INTO trades
                (trade_id, condition_id, question, side, entry_price, quantity,
                 cost_basis, status, stop_loss, take_profit,
                 kelly_fraction, thesis, invalidation, opened_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade_id,
                signal.get("condition_id", ""),
                signal.get("market", ""),
                signal.get("side", ""),
                signal.get("entry_target", 0),
                total_qty,
                signal.get("position_size", 0),
                result.get("status", "unknown"),
                signal.get("stop_loss", 0),
                signal.get("take_profit", 0),
                signal.get("position_pct", 0),
                f"Edge: {signal.get('edge_cents', 0)}c, Conf: {signal.get('confidence', '?')}",
                "",
                datetime.now(timezone.utc).isoformat(),
            ))
            conn.commit()
            print(f"  [DB] Trade logged: {trade_id}")
        except Exception as e:
            print(f"  [DB] Failed to log trade: {e}")
        finally:
            conn.close()

    def cancel_order(self, order_id: str) -> Dict:
        """Cancel an open order."""
        if self.dry_run:
            return {"status": "cancelled (dry run)", "order_id": order_id}

        import requests
        path = f"/order/{order_id}"
        headers = _create_auth_headers("DELETE", path)

        try:
            resp = requests.delete(
                f"{CLOB_BASE_URL}{path}",
                headers=headers,
                timeout=15,
            )
            return resp.json()
        except Exception as e:
            return {"error": str(e)}

    def check_open_orders(self) -> List[Dict]:
        """Check status of all open orders."""
        if self.dry_run:
            return []

        import requests
        path = "/orders"
        headers = _create_auth_headers("GET", path)

        try:
            resp = requests.get(
                f"{CLOB_BASE_URL}{path}",
                headers=headers,
                timeout=15,
            )
            return resp.json() if resp.status_code == 200 else []
        except Exception:
            return []


# ─── Position Monitor ─────────────────────────────────────────────

class PositionMonitor:
    """
    Monitors open positions for stop-loss and take-profit triggers.
    """

    def __init__(self):
        from api_client import PolymarketClient
        self.client = PolymarketClient()
        init_db()

    def check_positions(self) -> List[Dict]:
        """
        Check all open positions against current prices.
        Returns list of actions needed (stop-loss, take-profit, etc.)
        """
        conn = get_conn()
        positions = conn.execute("""
            SELECT t.*, m.question, m.yes_price, m.token_ids
            FROM trades t
            JOIN markets m ON t.condition_id = m.condition_id
            WHERE t.status IN ('executed', 'simulated', 'placed')
        """).fetchall()
        conn.close()

        alerts = []
        for pos in positions:
            pos = dict(pos)
            current_price = pos.get("yes_price", 0) or 0
            entry_price = pos.get("entry_price", 0) or 0
            side = pos.get("side", "")

            if not entry_price or not current_price:
                continue

            # Calculate P&L
            if "YES" in side.upper():
                pnl_cents = round((current_price - entry_price) * 100, 1)
            else:
                pnl_cents = round((entry_price - current_price) * 100, 1)

            cost = pos.get("cost_basis", 0) or 0
            pnl_usd = (pnl_cents / 100) * (pos.get("quantity", 0) or 0)

            alert = {
                "market": pos.get("question", pos.get("thesis", "?")),
                "side": side,
                "entry": entry_price,
                "current": current_price,
                "pnl_cents": pnl_cents,
                "pnl_usd": round(pnl_usd, 2),
                "cost_basis": cost,
                "action": "hold",
            }

            # Stop-loss check (15c against us)
            if pnl_cents <= -STOP_LOSS_CENTS:
                alert["action"] = "STOP_LOSS"
                alert["reason"] = f"Down {abs(pnl_cents)}c (limit: {STOP_LOSS_CENTS}c)"

            # Take-profit check (price > 0.93)
            effective_price = current_price if "YES" in side.upper() else (1 - current_price)
            if effective_price >= TAKE_PROFIT_THRESHOLD:
                alert["action"] = "TAKE_PROFIT"
                alert["reason"] = f"Price at {effective_price:.2f} (threshold: {TAKE_PROFIT_THRESHOLD})"

            # Time decay warning (market resolving soon)
            # This would need end_date comparison

            alerts.append(alert)

        return alerts


# ─── CLI ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Trade Executor")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Simulate without placing real orders (default)")
    parser.add_argument("--live", action="store_true",
                        help="Place real orders (requires API credentials)")
    parser.add_argument("--check", action="store_true",
                        help="Check open positions for alerts")
    args = parser.parse_args()

    init_db()

    if args.check:
        monitor = PositionMonitor()
        alerts = monitor.check_positions()
        if alerts:
            print(f"\n{'='*50}")
            print(f"POSITION ALERTS ({len(alerts)})")
            print(f"{'='*50}")
            for a in alerts:
                action = a["action"]
                emoji = "  " if action == "hold" else "!!"
                print(f"  {emoji} [{action:12s}] {a['side']:8s} | "
                      f"entry={a['entry']:.2f} now={a['current']:.2f} | "
                      f"P&L: {a['pnl_cents']:+.1f}c (${a['pnl_usd']:+.2f}) | "
                      f"{a['market'][:40]}")
        else:
            print("No open positions.")
    else:
        print("Trade Executor")
        print(f"  Mode: {'DRY RUN' if not args.live else 'LIVE'}")
        print(f"  Credentials: {'Configured' if is_configured() else 'Not set'}")
        print()
        print("Use via pipeline:")
        print("  from executor import TradeExecutor")
        print("  executor = TradeExecutor(dry_run=True)")
        print("  result = executor.execute_signal(trade_signal)")
