"""
Polymarket API Client
======================
Handles all communication with Polymarket's CLOB and Gamma APIs.
Read-only operations require no authentication.
"""

import time
import requests
from typing import Optional
from config import CLOB_BASE_URL, GAMMA_BASE_URL


class PolymarketClient:
    """Lightweight client for Polymarket REST APIs."""

    def __init__(self, clob_url: str = CLOB_BASE_URL, gamma_url: str = GAMMA_BASE_URL):
        self.clob_url = clob_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._request_count = 0
        self._last_request_time = 0

    # ─── Rate limiting ──────────────────────────────────────────────

    def _throttle(self):
        """Simple rate limiter: max ~55 requests/min to stay under 60/min limit."""
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < 1.1:  # At least 1.1s between requests
            time.sleep(1.1 - elapsed)
        self._last_request_time = time.time()
        self._request_count += 1

    def _get(self, url: str, params: Optional[dict] = None) -> dict:
        """GET with rate limiting and error handling."""
        self._throttle()
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  [API ERROR] {e}")
            return {}

    # ─── CLOB API: Markets ──────────────────────────────────────────

    def get_markets(self, next_cursor: Optional[str] = None) -> dict:
        """Fetch markets from the CLOB API with pagination."""
        params = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return self._get(f"{self.clob_url}/markets", params)

    def get_all_markets(self, max_pages: int = 20) -> list:
        """Fetch all markets, paginating through results."""
        all_markets = []
        next_cursor = None
        for _ in range(max_pages):
            resp = self.get_markets(next_cursor)
            data = resp.get("data", [])
            if not data:
                break
            all_markets.extend(data)
            next_cursor = resp.get("next_cursor")
            if not next_cursor or next_cursor == "LTE=":
                break
        return all_markets

    def get_market(self, condition_id: str) -> dict:
        """Fetch a single market by condition ID."""
        return self._get(f"{self.clob_url}/markets/{condition_id}")

    # ─── CLOB API: Order Book & Pricing ─────────────────────────────

    def get_order_book(self, token_id: str) -> dict:
        """Fetch order book for a specific token."""
        return self._get(f"{self.clob_url}/book", {"token_id": token_id})

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token."""
        resp = self._get(f"{self.clob_url}/midpoint", {"token_id": token_id})
        mid = resp.get("mid")
        return float(mid) if mid else None

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get current best price for a token on a given side."""
        resp = self._get(f"{self.clob_url}/price", {"token_id": token_id, "side": side})
        price = resp.get("price")
        return float(price) if price else None

    # ─── Gamma API: Rich Market Data ────────────────────────────────

    def get_gamma_markets(self, limit: int = 100, offset: int = 0,
                          active: bool = True, closed: bool = False,
                          order: str = "volume24hr",
                          ascending: bool = False) -> list:
        """
        Fetch markets from the Gamma API with rich metadata
        (volume, liquidity, tags, descriptions, resolution source, etc.)
        """
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        resp = self._get(f"{self.gamma_url}/markets", params)
        # Gamma API returns a list directly
        if isinstance(resp, list):
            return resp
        return resp.get("data", resp.get("markets", []))

    def get_gamma_events(self, limit: int = 50, active: bool = True,
                         order: str = "volume24hr", ascending: bool = False) -> list:
        """Fetch events (groups of related markets) from Gamma API."""
        params = {
            "limit": limit,
            "active": str(active).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        resp = self._get(f"{self.gamma_url}/events", params)
        if isinstance(resp, list):
            return resp
        return resp.get("data", resp.get("events", []))

    def _safe_float(self, value, default=0.0) -> float:
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # ─── Order Book Analysis ────────────────────────────────────────

    def analyze_order_book(self, token_id: str) -> dict:
        """
        Analyze order book depth and spread for a token.
        Returns summary stats useful for trade decisions.
        """
        book = self.get_order_book(token_id)
        if not book:
            return {"error": "Failed to fetch order book"}

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = self._safe_float(bids[0].get("price")) if bids else 0
        best_ask = self._safe_float(asks[0].get("price"), default=1.0) if asks else 1.0
        spread = best_ask - best_bid

        # Calculate depth (total $ available within 5 cents of best price)
        bid_depth = sum(
            self._safe_float(b.get("size")) * self._safe_float(b.get("price"))
            for b in bids
            if self._safe_float(b.get("price")) >= best_bid - 0.05
        )
        ask_depth = sum(
            self._safe_float(a.get("size")) * self._safe_float(a.get("price"))
            for a in asks
            if self._safe_float(a.get("price")) <= best_ask + 0.05
        )

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": round(spread, 4),
            "midpoint": round((best_bid + best_ask) / 2, 4) if best_bid and best_ask else None,
            "bid_depth_usd": round(bid_depth, 2),
            "ask_depth_usd": round(ask_depth, 2),
            "total_depth_usd": round(bid_depth + ask_depth, 2),
            "num_bid_levels": len(bids),
            "num_ask_levels": len(asks),
        }


# ─── Quick test ─────────────────────────────────────────────────────

if __name__ == "__main__":
    client = PolymarketClient()
    print("Testing connection...")
    markets = client.get_gamma_markets(limit=3)
    for m in markets:
        q = m.get("question", "?")
        vol = m.get("volume24hr", 0)
        print(f"  {q} | 24h vol: ${vol:,.0f}" if vol else f"  {q}")
    print(f"\nFetched {len(markets)} markets. API is working.")
