"""
Database Layer
===============
Structured storage for markets, predictions, resolutions, signals, and sharp traders.
Uses SQLite for development; migrate to DuckDB/Postgres when needed.

Schema follows the technical brief's data model:
  - markets: all fetched Polymarket markets (open + resolved)
  - predictions: all prediction outputs with full signal breakdown
  - resolutions: resolved market outcomes (for calibration)
  - sharp_traders: tracked wallet addresses + performance stats
  - signals_log: raw signal data per prediction (for debugging)
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any


# Store DB in a writable location; the mounted workspace may not support WAL mode.
# On your own machine, change this to the project directory.
_project_dir = os.path.dirname(os.path.abspath(__file__))
_candidate_paths = [
    os.path.join(_project_dir, "polymarket.db"),           # Try project dir first
    os.path.join(os.path.expanduser("~"), "polymarket.db"),  # Fallback to home
    "/tmp/polymarket.db",                                    # Last resort
]

DB_PATH = _candidate_paths[0]  # Default
for _p in _candidate_paths:
    try:
        _test_conn = sqlite3.connect(_p)
        _test_conn.execute("PRAGMA journal_mode=WAL")
        _test_conn.execute("CREATE TABLE IF NOT EXISTS _init_test (id INTEGER)")
        _test_conn.execute("DROP TABLE _init_test")
        _test_conn.close()
        DB_PATH = _p
        break
    except Exception:
        continue


def get_conn(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str = DB_PATH):
    """Create all tables if they don't exist."""
    conn = get_conn(db_path)
    cur = conn.cursor()

    # ─── Markets table ──────────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS markets (
        condition_id    TEXT PRIMARY KEY,
        question        TEXT NOT NULL,
        slug            TEXT,
        description     TEXT,
        category        TEXT,
        outcomes        TEXT,           -- JSON array: ["Yes","No"]
        token_ids       TEXT,           -- JSON array of CLOB token IDs
        resolution_source TEXT,
        end_date        TEXT,
        created_at      TEXT,

        -- Snapshot fields (updated on each scrape)
        yes_price       REAL,
        no_price        REAL,
        spread          REAL,
        best_bid        REAL,
        best_ask        REAL,
        volume_24h      REAL,
        total_volume    REAL,
        liquidity       REAL,
        price_change_1d REAL,
        price_change_1w REAL,

        -- State
        active          INTEGER DEFAULT 1,  -- boolean
        closed          INTEGER DEFAULT 0,
        accepting_orders INTEGER DEFAULT 1,
        resolved        INTEGER DEFAULT 0,
        resolution      TEXT,               -- "Yes", "No", or NULL
        resolved_at     TEXT,

        -- Metadata
        neg_risk        INTEGER DEFAULT 0,
        tags            TEXT,               -- JSON array
        last_scraped_at TEXT NOT NULL,

        -- Computed
        market_type     TEXT                -- "political", "crypto", "sports", "regulatory", "other"
    )
    """)

    # ─── Predictions table ──────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS predictions (
        prediction_id       TEXT PRIMARY KEY,
        condition_id        TEXT NOT NULL,
        question            TEXT,

        -- Market state at prediction time
        market_price_at     REAL,
        liquidity_at        REAL,

        -- Our estimate
        sim_probability_raw REAL,           -- Raw MiroFish output
        sim_probability_cal REAL,           -- After calibration
        our_estimate        REAL NOT NULL,  -- Final probability we're using
        delta               REAL,           -- our_estimate - market_price
        confidence          TEXT,           -- "low", "medium", "high"

        -- Signal breakdown
        news_signal         REAL,           -- -1 to +1
        sentiment_signal    REAL,           -- -1 to +1
        base_rate           REAL,           -- Historical base rate 0-1
        cross_platform_delta REAL,          -- Avg delta from other platforms
        sharp_trader_consensus TEXT,        -- "YES", "NO", "MIXED", NULL

        -- Decision
        actionable          INTEGER DEFAULT 0,
        kelly_fraction      REAL,
        recommended_side    TEXT,           -- "BUY YES" or "BUY NO"
        recommended_size    REAL,

        -- Drivers
        key_drivers         TEXT,           -- JSON array of strings
        dissent_flag        INTEGER DEFAULT 0,

        -- Metadata
        predicted_at        TEXT NOT NULL,
        signal_bundle       TEXT            -- Full JSON of all raw signals
    )
    """)

    # ─── Resolutions table (for calibration) ────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS resolutions (
        condition_id        TEXT PRIMARY KEY,
        question            TEXT,
        resolution          TEXT NOT NULL,   -- "Yes" or "No"
        resolved_at         TEXT,

        -- Market price at resolution
        final_price         REAL,

        -- Our prediction (if we made one)
        prediction_id       TEXT,
        our_estimate        REAL,
        our_delta           REAL,
        brier_score_market  REAL,           -- (market_price - actual)^2
        brier_score_ours    REAL,           -- (our_estimate - actual)^2

        -- P&L if we traded
        traded              INTEGER DEFAULT 0,
        realized_pnl        REAL,

        FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
    )
    """)

    # ─── Sharp traders table ────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sharp_traders (
        wallet_address  TEXT PRIMARY KEY,
        label           TEXT,               -- Human-readable name/alias
        chain           TEXT DEFAULT 'polygon',
        total_markets   INTEGER DEFAULT 0,
        win_rate        REAL,
        avg_roi         REAL,
        total_pnl       REAL,
        tracked_since   TEXT,
        last_updated    TEXT,
        notes           TEXT
    )
    """)

    # ─── Sharp trader positions ─────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sharp_positions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        wallet_address  TEXT NOT NULL,
        condition_id    TEXT NOT NULL,
        side            TEXT,               -- "YES" or "NO"
        size_usd        REAL,
        entry_price     REAL,
        observed_at     TEXT NOT NULL,

        FOREIGN KEY (wallet_address) REFERENCES sharp_traders(wallet_address),
        FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
    )
    """)

    # ─── Signals log ────────────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signals_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        prediction_id   TEXT NOT NULL,
        signal_type     TEXT NOT NULL,       -- "news", "sentiment", "base_rate", "cross_platform", "sharp_trader"
        raw_data        TEXT,                -- JSON blob
        score           REAL,
        freshness_ts    TEXT,
        source          TEXT,
        logged_at       TEXT NOT NULL,

        FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
    )
    """)

    # ─── Trades ledger ──────────────────────────────────────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        trade_id        TEXT PRIMARY KEY,
        condition_id    TEXT NOT NULL,
        prediction_id   TEXT,
        question        TEXT,

        side            TEXT NOT NULL,       -- "BUY YES" or "BUY NO"
        entry_price     REAL NOT NULL,
        quantity        REAL NOT NULL,
        cost_basis      REAL NOT NULL,

        -- Current state
        current_price   REAL,
        unrealized_pnl  REAL,
        status          TEXT DEFAULT 'open', -- "open", "closed", "stopped_out"

        -- Exit
        exit_price      REAL,
        exit_quantity   REAL,
        realized_pnl    REAL,
        closed_at       TEXT,
        close_reason    TEXT,               -- "take_profit", "stop_loss", "thesis_invalidated", "resolution", "manual"

        -- Risk
        stop_loss       REAL,
        take_profit     REAL,
        kelly_fraction  REAL,
        narrative_tag   TEXT,               -- For correlation tracking
        thesis          TEXT,
        invalidation    TEXT,

        -- Metadata
        opened_at       TEXT NOT NULL,

        FOREIGN KEY (condition_id) REFERENCES markets(condition_id),
        FOREIGN KEY (prediction_id) REFERENCES predictions(prediction_id)
    )
    """)

    # ─── Signal accuracy feedback (for weight tuning) ───────────
    cur.execute("""
    CREATE TABLE IF NOT EXISTS signal_accuracy (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        condition_id         TEXT NOT NULL,
        prediction_id        TEXT,
        market_type          TEXT,
        resolution           TEXT NOT NULL,   -- "Yes" or "No"
        actual               REAL NOT NULL,   -- 1.0 or 0.0
        resolved_at          TEXT,

        -- Per-signal scores and implied Brier at prediction time
        news_score           REAL,
        news_brier           REAL,
        sharp_trader_score   REAL,
        sharp_trader_brier   REAL,
        base_rate_score      REAL,
        base_rate_brier      REAL,
        cross_platform_score REAL,
        cross_platform_brier REAL,

        -- Aggregate
        our_estimate         REAL,
        aggregate_brier      REAL,
        market_brier         REAL,   -- (market_price - actual)^2 baseline

        confidence           TEXT,
        signal_count         INTEGER,
        recorded_at          TEXT NOT NULL,

        FOREIGN KEY (condition_id) REFERENCES markets(condition_id)
    )
    """)

    # ─── Trades ledger — add is_paper for existing DBs ──────────
    try:
        cur.execute("ALTER TABLE trades ADD COLUMN is_paper INTEGER DEFAULT 0")
    except Exception:
        pass  # Column already exists

    # ─── Indexes ────────────────────────────────────────────────
    cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active, closed)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_type ON markets(market_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_markets_end ON markets(end_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_cid ON predictions(condition_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_predictions_date ON predictions(predicted_at)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_resolutions_pred ON resolutions(prediction_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_cid ON trades(condition_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sharp_positions_cid ON sharp_positions(condition_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_signals_pred ON signals_log(prediction_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sig_acc_cid ON signal_accuracy(condition_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sig_acc_type ON signal_accuracy(market_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_trades_paper ON trades(is_paper)")

    conn.commit()
    conn.close()
    return db_path


# ─── Market CRUD ────────────────────────────────────────────────

def upsert_market(conn: sqlite3.Connection, market: Dict[str, Any]):
    """Insert or update a market record."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("""
    INSERT INTO markets (
        condition_id, question, slug, description, category, outcomes,
        token_ids, resolution_source, end_date, created_at,
        yes_price, no_price, spread, best_bid, best_ask,
        volume_24h, total_volume, liquidity, price_change_1d, price_change_1w,
        active, closed, accepting_orders, resolved, resolution, resolved_at,
        neg_risk, tags, last_scraped_at, market_type
    ) VALUES (
        :condition_id, :question, :slug, :description, :category, :outcomes,
        :token_ids, :resolution_source, :end_date, :created_at,
        :yes_price, :no_price, :spread, :best_bid, :best_ask,
        :volume_24h, :total_volume, :liquidity, :price_change_1d, :price_change_1w,
        :active, :closed, :accepting_orders, :resolved, :resolution, :resolved_at,
        :neg_risk, :tags, :last_scraped_at, :market_type
    )
    ON CONFLICT(condition_id) DO UPDATE SET
        question = :question,
        yes_price = :yes_price, no_price = :no_price,
        spread = :spread, best_bid = :best_bid, best_ask = :best_ask,
        volume_24h = :volume_24h, total_volume = :total_volume,
        liquidity = :liquidity,
        price_change_1d = :price_change_1d, price_change_1w = :price_change_1w,
        active = :active, closed = :closed, accepting_orders = :accepting_orders,
        resolved = :resolved, resolution = :resolution, resolved_at = :resolved_at,
        last_scraped_at = :last_scraped_at
    """, {
        "condition_id": market.get("conditionId") or market.get("condition_id", ""),
        "question": market.get("question", ""),
        "slug": market.get("slug", ""),
        "description": (market.get("description") or "")[:2000],
        "category": market.get("category", ""),
        "outcomes": json.dumps(market.get("outcomes", ["Yes", "No"])),
        "token_ids": json.dumps(market.get("token_ids", [])),
        "resolution_source": market.get("resolutionSource", ""),
        "end_date": market.get("end_date") or market.get("endDateIso", ""),
        "created_at": market.get("createdAt", ""),
        "yes_price": market.get("yes_price"),
        "no_price": market.get("no_price"),
        "spread": market.get("spread"),
        "best_bid": market.get("best_bid") or market.get("bestBid"),
        "best_ask": market.get("best_ask") or market.get("bestAsk"),
        "volume_24h": market.get("volume_24h") or market.get("volume24hr"),
        "total_volume": market.get("total_volume") or market.get("volumeNum"),
        "liquidity": market.get("liquidity") or market.get("liquidityNum"),
        "price_change_1d": market.get("price_change_1d") or market.get("oneDayPriceChange"),
        "price_change_1w": market.get("price_change_1w") or market.get("oneWeekPriceChange"),
        "active": 1 if market.get("active", True) else 0,
        "closed": 1 if market.get("closed", False) else 0,
        "accepting_orders": 1 if market.get("accepting_orders") or market.get("acceptingOrders", True) else 0,
        "resolved": 1 if market.get("resolved", False) else 0,
        "resolution": market.get("resolution"),
        "resolved_at": market.get("resolved_at") or market.get("closedTime"),
        "neg_risk": 1 if market.get("neg_risk") or market.get("negRisk", False) else 0,
        "tags": json.dumps(market.get("tags", [])),
        "last_scraped_at": now,
        "market_type": classify_market(market.get("question", ""), market.get("category", "")),
    })


def classify_market(question: str, category: str) -> str:
    """Classify market into our target niches."""
    q = question.lower()
    c = category.lower()

    # Political & regulatory (our primary niche)
    political_keywords = [
        "president", "election", "democrat", "republican", "senate", "congress",
        "vote", "bill", "legislation", "governor", "nomination", "impeach",
        "sec ", "fda ", "ftc ", "epa ", "ruling", "regulation", "executive order",
        "cabinet", "confirmation", "primary", "caucus", "ballot",
        "trump", "biden", "harris", "desantis", "newsom",
    ]
    if any(kw in q or kw in c for kw in political_keywords):
        return "political"

    # Regulatory
    regulatory_keywords = ["approve", "ban", "antitrust", "merger", "filing", "compliance"]
    if any(kw in q for kw in regulatory_keywords):
        return "regulatory"

    # Crypto
    crypto_keywords = ["bitcoin", "ethereum", "btc", "eth", "crypto", "token", "blockchain",
                       "solana", "fdv", "defi", "nft", "airdrop"]
    if any(kw in q or kw in c for kw in crypto_keywords):
        return "crypto"

    # Sports
    sports_keywords = ["vs.", "nba", "nhl", "nfl", "mlb", "world cup", "ufc",
                       "championship", "tournament", "game", "match"]
    if any(kw in q or kw in c for kw in sports_keywords):
        return "sports"

    # Geopolitics
    geo_keywords = ["war", "military", "ceasefire", "invasion", "nato", "iran",
                    "russia", "ukraine", "china", "sanctions", "missile"]
    if any(kw in q for kw in geo_keywords):
        return "geopolitical"

    return "other"


# ─── Query helpers ──────────────────────────────────────────────

def get_active_markets(conn: sqlite3.Connection, market_type: str = None) -> List[Dict]:
    """Get all active, open markets. Optionally filter by type."""
    query = "SELECT * FROM markets WHERE active=1 AND closed=0"
    params = []
    if market_type:
        query += " AND market_type=?"
        params.append(market_type)
    query += " ORDER BY volume_24h DESC"
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_resolved_markets(conn: sqlite3.Connection, market_type: str = None,
                         limit: int = 500) -> List[Dict]:
    """Get resolved markets for calibration."""
    query = "SELECT * FROM markets WHERE resolved=1"
    params = []
    if market_type:
        query += " AND market_type=?"
        params.append(market_type)
    query += " ORDER BY resolved_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(query, params).fetchall()]


def get_market(conn: sqlite3.Connection, condition_id: str) -> Optional[Dict]:
    """Get a single market by condition_id."""
    row = conn.execute("SELECT * FROM markets WHERE condition_id=?", (condition_id,)).fetchone()
    return dict(row) if row else None


def get_predictions_for_market(conn: sqlite3.Connection, condition_id: str) -> List[Dict]:
    """Get all predictions we've made for a market."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM predictions WHERE condition_id=? ORDER BY predicted_at DESC",
        (condition_id,)
    ).fetchall()]


def get_open_trades(conn: sqlite3.Connection) -> List[Dict]:
    """Get all open trades."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM trades WHERE status='open' ORDER BY opened_at DESC"
    ).fetchall()]


def get_calibration_data(conn: sqlite3.Connection) -> List[Dict]:
    """Get prediction-vs-resolution pairs for calibration analysis."""
    return [dict(r) for r in conn.execute("""
        SELECT r.condition_id, r.question, r.resolution,
               r.brier_score_market, r.brier_score_ours,
               p.our_estimate, p.market_price_at, p.confidence,
               p.predicted_at
        FROM resolutions r
        LEFT JOIN predictions p ON r.prediction_id = p.prediction_id
        WHERE r.resolution IS NOT NULL
        ORDER BY r.resolved_at DESC
    """).fetchall()]


def db_stats(conn: sqlite3.Connection) -> Dict:
    """Get summary stats for the database."""
    stats = {}
    for table in ["markets", "predictions", "resolutions", "sharp_traders", "trades", "signals_log"]:
        row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
        stats[table] = row["cnt"]

    # Additional stats
    row = conn.execute("SELECT COUNT(*) as cnt FROM markets WHERE active=1 AND closed=0").fetchone()
    stats["active_markets"] = row["cnt"]
    row = conn.execute("SELECT COUNT(*) as cnt FROM markets WHERE resolved=1").fetchone()
    stats["resolved_markets"] = row["cnt"]
    row = conn.execute("SELECT COUNT(*) as cnt FROM trades WHERE status='open'").fetchone()
    stats["open_trades"] = row["cnt"]

    # Market type breakdown
    types = conn.execute("""
        SELECT market_type, COUNT(*) as cnt
        FROM markets GROUP BY market_type ORDER BY cnt DESC
    """).fetchall()
    stats["by_type"] = {r["market_type"]: r["cnt"] for r in types}

    return stats


# ─── Signal accuracy ────────────────────────────────────────────

def record_signal_accuracy(conn: sqlite3.Connection, condition_id: str,
                           signals: Dict, resolution: str,
                           prediction_id: str = None, market_type: str = None,
                           our_estimate: float = None, market_price: float = None) -> None:
    """
    After a market resolves, record each signal's accuracy for weight tuning.
    Signal scores are in [-1, +1]; we map to [0, 1] via p = 0.5 + score * 0.5.
    """
    actual = 1.0 if str(resolution).lower() in ("yes", "1", "true") else 0.0
    now = datetime.now(timezone.utc).isoformat()

    sig_types = ["news", "sharp_trader", "base_rate", "cross_platform"]
    row: Dict[str, Any] = {
        "condition_id": condition_id,
        "prediction_id": prediction_id,
        "market_type": market_type,
        "resolution": resolution,
        "actual": actual,
        "our_estimate": our_estimate,
        "aggregate_brier": (our_estimate - actual) ** 2 if our_estimate is not None else None,
        "market_brier": (market_price - actual) ** 2 if market_price is not None else None,
        "confidence": None,
        "signal_count": 0,
        "resolved_at": now,
        "recorded_at": now,
    }
    for st in sig_types:
        sig = (signals or {}).get(st) or {}
        score = sig.get("score")
        if score is not None:
            prob = max(0.01, min(0.99, 0.5 + float(score) * 0.5))
            row[f"{st}_score"] = float(score)
            row[f"{st}_brier"] = (prob - actual) ** 2
            row["signal_count"] = (row["signal_count"] or 0) + 1
        else:
            row[f"{st}_score"] = None
            row[f"{st}_brier"] = None

    conn.execute("""
        INSERT INTO signal_accuracy (
            condition_id, prediction_id, market_type, resolution, actual, resolved_at,
            news_score, news_brier, sharp_trader_score, sharp_trader_brier,
            base_rate_score, base_rate_brier, cross_platform_score, cross_platform_brier,
            our_estimate, aggregate_brier, market_brier, confidence, signal_count, recorded_at
        ) VALUES (
            :condition_id, :prediction_id, :market_type, :resolution, :actual, :resolved_at,
            :news_score, :news_brier, :sharp_trader_score, :sharp_trader_brier,
            :base_rate_score, :base_rate_brier, :cross_platform_score, :cross_platform_brier,
            :our_estimate, :aggregate_brier, :market_brier, :confidence, :signal_count, :recorded_at
        )
    """, row)


def get_signal_brier_by_type(conn: sqlite3.Connection, market_type: str = None,
                              lookback_days: int = None) -> Dict:
    """Return avg Brier scores per signal type — used for weight tuning."""
    conditions = ["actual IS NOT NULL"]
    params: List = []
    if market_type:
        conditions.append("market_type = ?")
        params.append(market_type)
    if lookback_days:
        conditions.append("recorded_at >= datetime('now', ?)")
        params.append(f"-{lookback_days} days")
    where = " AND ".join(conditions)
    row = conn.execute(f"""
        SELECT COUNT(*) as n,
               AVG(news_brier)          as news_brier,
               AVG(sharp_trader_brier)  as sharp_brier,
               AVG(base_rate_brier)     as base_brier,
               AVG(cross_platform_brier) as xp_brier,
               AVG(aggregate_brier)     as agg_brier,
               AVG(market_brier)        as market_brier
        FROM signal_accuracy WHERE {where}
    """, params).fetchone()
    return dict(row) if row else {}


# ─── Init on import ─────────────────────────────────────────────

if __name__ == "__main__":
    path = init_db()
    conn = get_conn(path)
    print(f"Database initialized at: {path}")
    print(f"Stats: {json.dumps(db_stats(conn), indent=2)}")
    conn.close()
