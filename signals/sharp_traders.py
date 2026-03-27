"""
Signal 5: Sharp Trader Positions (Copy Trading)
=================================================
Tracks known high-performing Polymarket whale wallets.
Queries their current positions via the Polymarket Data API.
If multiple sharp traders are positioned on one side, that's a strong prior.

Primary source: data-api.polymarket.com/positions (no auth needed)
Fallback:       Polygonscan API (on-chain ERC-1155 balance queries)

Identified whales — all suspected to be the same entity (Theo4 cluster),
funded via Kraken with coordinated bet sizing on US politics markets.
Combined profits: ~$85M+. Source: on-chain analysis by @fozzydiablo.
"""

import os
import sys
import json
import time
import requests
from datetime import datetime, timezone
from typing import List, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Polymarket CTF Exchange on Polygon
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

POLYGONSCAN_API = "https://api.polygonscan.com/api"
POLYMARKET_DATA_API = "https://data-api.polymarket.com"


# ─── Known sharp traders ───────────────────────────────────────
# Format: (proxy_wallet_address, label, est_total_pnl_usd, notes)
#
# All 7 wallets are believed to be operated by the same entity ("Theo4 cluster")
# — verified by shared Kraken funding source and coordinated bet timing.
# Treat as ONE high-conviction signal source, not 7 independent ones.

KNOWN_SHARP_TRADERS = [
    # ── Tier 1: Confirmed identities with full P&L history ──────
    (
        "0x56687bf447db6ffa42ffe2204a05edaa20f55839",
        "Theo4",
        22_000_000,
        "Primary account. $22M profit, 88.9% win rate. "
        "Uses commissioned YouGov polls with neighbor-effect bias correction. "
        "Has 11 known accounts to avoid front-running. Politics specialist.",
    ),
    (
        "0x1f2dd6d473f3e824cd2f8a89d9c69fb96f6ad0cf",
        "Fredi9999",
        16_600_000,
        "Secondary account for Theo4. $16.6M profit, 73.3% win rate. "
        "Same operator, different wallet to reduce front-running exposure.",
    ),
    (
        "0xd235973291b2b75ff4070e9c0b01728c520b0f29",
        "zxgngl",
        11_400_000,
        "Pure conviction plays. $11.4M profit, 80% win rate. "
        "Highly selective — only 8 total positions. Enters very large.",
    ),
    # ── Tier 2: Connected accounts (100% win rate, smaller history) ──
    (
        "0x863134d00841b2e200492805a01e1e2f5defaa53",
        "RepTrump",
        7_500_000,
        "Connected to Theo4 cluster. $7.5M profit, 100% win rate on 21 positions. "
        "Coordinated Kraken funding, same bet timing as Theo4.",
    ),
    (
        "0x78b9ac44a6d7d7a076c14e0ad518b301b63c6b76",
        "Len9311238",
        8_700_000,
        "Connected to Theo4 cluster. $8.7M profit across 7 politics markets. "
        "$16.4M total volume. 100% win rate.",
    ),
    (
        "0x8119010a6e589062aa03583bb3f39ca632d9f887",
        "PrincessCaro",
        6_100_000,
        "Connected to Theo4 cluster. $6.1M profit, 100% win rate. "
        "Identified in the original @fozzydiablo whale thread.",
    ),
    (
        "0xe9ad918c7678cd38b12603a762e638a5d1ee7091",
        "walletmobile",
        5_900_000,
        "Connected to Theo4 cluster. $5.9M profit, 100% win rate on 8 positions. "
        "Consistent with cluster pattern.",
    ),
]


def get_polygonscan_key() -> str:
    """Get Polygonscan API key from config or env."""
    try:
        from config import POLYGONSCAN_API_KEY
        if POLYGONSCAN_API_KEY:
            return POLYGONSCAN_API_KEY
    except (ImportError, AttributeError):
        pass
    return os.environ.get("POLYGONSCAN_API_KEY", "")


# ─── Polymarket Data API (primary method — no auth required) ───────────────

def fetch_positions_data_api(wallet: str, size_threshold: float = 5.0,
                              timeout: int = 8) -> List[Dict]:
    """
    Fetch a wallet's open positions from the Polymarket Data API.

    Returns list of position dicts with keys:
      conditionId, title, side (YES/NO), size (USDC), currentValue, outcome
    """
    try:
        url = f"{POLYMARKET_DATA_API}/positions"
        resp = requests.get(url, params={
            "user": wallet,
            "sizeThreshold": size_threshold,
            "limit": 100,
        }, timeout=timeout)
        resp.raise_for_status()
        raw = resp.json()
        if not isinstance(raw, list):
            return []

        positions = []
        for p in raw:
            # Determine which outcome token they hold
            outcome_index = p.get("outcomeIndex", 0)  # 0=YES, 1=NO typically
            side = "YES" if outcome_index == 0 else "NO"
            # Some responses use 'title' or 'question'
            title = p.get("title") or p.get("question") or p.get("market", {}).get("question", "")
            cid = p.get("conditionId") or p.get("condition_id") or \
                  p.get("market", {}).get("conditionId", "")
            size = float(p.get("size") or p.get("currentValue") or p.get("value") or 0)
            avg_price = float(p.get("avgPrice") or p.get("averagePrice") or 0)

            if cid and size > 0:
                positions.append({
                    "conditionId": cid,
                    "title": title,
                    "side": side,
                    "size_usdc": size,
                    "avg_price": avg_price,
                    "outcome_index": outcome_index,
                })
        return positions

    except Exception:
        return []


def get_all_sharp_positions(size_threshold: float = 10.0,
                             delay_between: float = 0.5) -> Dict[str, List[Dict]]:
    """
    Fetch current positions for all known sharp traders.
    Returns dict: {wallet_address: [position, ...]}
    Rate-limited with delay_between seconds between requests.
    """
    results = {}
    for addr, label, _, _ in KNOWN_SHARP_TRADERS:
        positions = fetch_positions_data_api(addr, size_threshold=size_threshold)
        results[addr] = positions
        if delay_between > 0:
            time.sleep(delay_between)
    return results


def consensus_for_market(condition_id: str,
                          min_traders: int = 1,
                          size_threshold: float = 10.0) -> Dict:
    """
    Aggregate sharp trader positions for a specific market.

    Returns:
      {
        "direction":   "YES" | "NO" | "MIXED" | None,
        "conviction":  float,   # dominant_side_usd / total_usd (0-1)
        "total_usd":   float,
        "yes_usd":     float,
        "no_usd":      float,
        "traders":     [{"label": ..., "side": ..., "size_usdc": ...}],
        "signal":      float,   # -1 to +1 for pipeline ingestion
      }
    """
    all_positions = get_all_sharp_positions(size_threshold=size_threshold)

    yes_usd = 0.0
    no_usd = 0.0
    traders_in = []

    # Map address to label for display
    label_map = {addr: label for addr, label, _, _ in KNOWN_SHARP_TRADERS}

    for addr, positions in all_positions.items():
        for pos in positions:
            if pos.get("conditionId", "").lower() == condition_id.lower():
                label = label_map.get(addr, addr[:10])
                side = pos["side"]
                size = pos["size_usdc"]
                traders_in.append({"label": label, "side": side, "size_usdc": size})
                if side == "YES":
                    yes_usd += size
                else:
                    no_usd += size

    total_usd = yes_usd + no_usd

    if total_usd == 0 or len(traders_in) < min_traders:
        return {
            "direction": None, "conviction": 0.0,
            "total_usd": 0.0, "yes_usd": 0.0, "no_usd": 0.0,
            "traders": [], "signal": 0.0,
        }

    conviction = max(yes_usd, no_usd) / total_usd
    direction = "YES" if yes_usd >= no_usd else "NO"
    if yes_usd > 0 and no_usd > 0 and conviction < 0.65:
        direction = "MIXED"

    # Signal: +1 = all-in YES, -1 = all-in NO, 0 = mixed/no data
    raw_signal = (yes_usd - no_usd) / total_usd  # -1 to +1
    # Scale by conviction: even 100% YES with $10 isn't as strong as $10k
    size_factor = min(1.0, total_usd / 1000.0)  # saturates at $1k
    signal = raw_signal * size_factor

    return {
        "direction": direction,
        "conviction": round(conviction, 4),
        "total_usd": round(total_usd, 2),
        "yes_usd": round(yes_usd, 2),
        "no_usd": round(no_usd, 2),
        "traders": traders_in,
        "signal": round(signal, 4),
    }


def get_token_balance(wallet: str, token_id: str,
                      api_key: str = "") -> Optional[float]:
    """
    Query a wallet's balance of a specific conditional token (YES or NO)
    via Polygonscan API.

    token_id: The CLOB token ID for the outcome (YES or NO token)
    """
    key = api_key or get_polygonscan_key()
    if not key:
        return None

    # The conditional tokens contract uses ERC-1155
    # We need to query balanceOf(address, tokenId)
    try:
        # Use the Polygonscan token balance endpoint
        resp = requests.get(POLYGONSCAN_API, params={
            "module": "account",
            "action": "tokenbalance",
            "contractaddress": CONDITIONAL_TOKENS,
            "address": wallet,
            "tag": "latest",
            "apikey": key,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") == "1":
            balance = int(data.get("result", 0))
            return balance / 1e6  # USDC has 6 decimals
        return None

    except Exception as e:
        return None


def query_sharp_positions_rpc(wallet: str, token_ids: List[str],
                               rpc_url: str = "https://polygon-rpc.com") -> Dict:
    """
    Query a wallet's positions via direct RPC call to Polygon.
    This works without an API key.

    Returns: {token_id: balance_in_usd}
    """
    positions = {}

    for token_id in token_ids:
        try:
            # ERC-1155 balanceOf(address, id)
            # Function signature: 0x00fdd58e
            padded_addr = wallet.lower().replace("0x", "").zfill(64)
            padded_id = hex(int(token_id))[2:].zfill(64)
            data = f"0x00fdd58e{padded_addr}{padded_id}"

            resp = requests.post(rpc_url, json={
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{
                    "to": CONDITIONAL_TOKENS,
                    "data": data,
                }, "latest"],
                "id": 1,
            }, timeout=10)
            resp.raise_for_status()
            result = resp.json().get("result", "0x0")
            balance = int(result, 16) / 1e6  # Convert from raw to USDC (6 decimals)
            if balance > 0:
                positions[token_id] = round(balance, 2)

        except Exception as e:
            continue

    return positions


def get_sharp_trader_signal(token_ids: List[str], outcomes: List[str] = None,
                            custom_wallets: List[Dict] = None) -> Dict:
    """
    Check sharp trader positions for a market.

    Args:
        token_ids: [YES_token_id, NO_token_id]
        outcomes: ["Yes", "No"] or similar
        custom_wallets: Override KNOWN_SHARP_TRADERS with custom list

    Returns:
    {
        "signal_type": "sharp_trader",
        "score": float (-1 to +1),
        "consensus": "YES" | "NO" | "MIXED" | "NONE",
        "traders_checked": int,
        "traders_positioned": int,
        "positions": [...],
        "aggregate_yes_usd": float,
        "aggregate_no_usd": float,
    }
    """
    wallets = custom_wallets or KNOWN_SHARP_TRADERS
    outcomes = outcomes or ["Yes", "No"]

    if not wallets:
        return {
            "signal_type": "sharp_trader",
            "score": 0.0,
            "consensus": "NONE",
            "traders_checked": 0,
            "traders_positioned": 0,
            "positions": [],
            "aggregate_yes_usd": 0,
            "aggregate_no_usd": 0,
            "freshness_ts": datetime.now(timezone.utc).isoformat(),
            "source": "polygon_rpc",
            "raw_data": {"error": "No sharp trader wallets configured"},
        }

    if len(token_ids) < 2:
        return {
            "signal_type": "sharp_trader",
            "score": 0.0,
            "consensus": "NONE",
            "traders_checked": 0,
            "traders_positioned": 0,
            "positions": [],
            "freshness_ts": datetime.now(timezone.utc).isoformat(),
            "source": "polygon_rpc",
            "raw_data": {"error": "Need both YES and NO token IDs"},
        }

    yes_token = token_ids[0]
    no_token = token_ids[1]

    all_positions = []
    total_yes = 0.0
    total_no = 0.0

    for wallet_info in wallets:
        if isinstance(wallet_info, tuple):
            addr, label, notes = wallet_info[0], wallet_info[1], wallet_info[2] if len(wallet_info) > 2 else ""
        elif isinstance(wallet_info, dict):
            addr = wallet_info.get("address", "")
            label = wallet_info.get("label", "")
            notes = wallet_info.get("notes", "")
        else:
            continue

        if not addr:
            continue

        # Query both YES and NO token balances
        positions = query_sharp_positions_rpc(addr, [yes_token, no_token])

        yes_bal = positions.get(yes_token, 0)
        no_bal = positions.get(no_token, 0)

        if yes_bal > 0 or no_bal > 0:
            side = "YES" if yes_bal > no_bal else "NO"
            pos_size = max(yes_bal, no_bal)

            all_positions.append({
                "wallet": addr[:10] + "...",
                "label": label,
                "side": side,
                "yes_balance": yes_bal,
                "no_balance": no_bal,
                "total_usd": round(yes_bal + no_bal, 2),
            })

            total_yes += yes_bal
            total_no += no_bal

    # Determine consensus
    yes_traders = sum(1 for p in all_positions if p["side"] == "YES")
    no_traders = sum(1 for p in all_positions if p["side"] == "NO")

    if not all_positions:
        consensus = "NONE"
        signal_score = 0.0
    elif yes_traders > no_traders * 2:
        consensus = "YES"
        signal_score = min(1.0, total_yes / max(total_yes + total_no, 1) * 2 - 1)
    elif no_traders > yes_traders * 2:
        consensus = "NO"
        signal_score = max(-1.0, -(total_no / max(total_yes + total_no, 1) * 2 - 1))
    else:
        consensus = "MIXED"
        signal_score = (total_yes - total_no) / max(total_yes + total_no, 1)

    return {
        "signal_type": "sharp_trader",
        "score": round(signal_score, 4),
        "consensus": consensus,
        "traders_checked": len(wallets),
        "traders_positioned": len(all_positions),
        "positions": all_positions,
        "aggregate_yes_usd": round(total_yes, 2),
        "aggregate_no_usd": round(total_no, 2),
        "freshness_ts": datetime.now(timezone.utc).isoformat(),
        "source": "polygon_rpc",
        "raw_data": {},
    }


def format_for_mirofish(signal: Dict) -> str:
    """Format sharp trader signal for MiroFish seed material."""
    lines = ["SHARP TRADER POSITIONS:"]

    if signal["consensus"] == "NONE":
        lines.append("  No sharp trader wallet data available.")
        return "\n".join(lines)

    lines.append(f"  Consensus: {signal['consensus']}")
    lines.append(f"  Traders positioned: {signal['traders_positioned']}/{signal['traders_checked']}")
    lines.append(f"  Aggregate YES: ${signal['aggregate_yes_usd']:,.0f}")
    lines.append(f"  Aggregate NO: ${signal['aggregate_no_usd']:,.0f}")
    lines.append("")

    for p in signal["positions"][:5]:
        lines.append(f"  [{p['label'] or p['wallet']}] {p['side']} — ${p['total_usd']:,.0f}")

    return "\n".join(lines)


if __name__ == "__main__":
    print("Sharp trader tracker initialized.")
    print(f"Known wallets configured: {len(KNOWN_SHARP_TRADERS)}")
    print("Add wallet addresses to KNOWN_SHARP_TRADERS in this file,")
    print("or insert them into the sharp_traders DB table.")
