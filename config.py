"""
Polymarket Trading Agent — Configuration
==========================================
Edit this file with your API credentials and risk parameters.
"""

# ─── API CREDENTIALS ───────────────────────────────────────────────
# For read-only market scanning, no credentials are needed.
# For trading, fill in your Polymarket CLOB API credentials.

POLYMARKET_API_KEY = ""        # UUID format
POLYMARKET_SECRET = ""         # Base64-encoded
POLYMARKET_PASSPHRASE = ""     # Random passphrase
PRIVATE_KEY = ""               # Your Polygon wallet private key
WALLET_ADDRESS = ""            # Your Polygon wallet address

# ─── API ENDPOINTS ─────────────────────────────────────────────────

CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
DATA_BASE_URL = "https://data-api.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet

# ─── BANKROLL & RISK PARAMETERS ───────────────────────────────────

BANKROLL = 500.0               # Total bankroll in USDC
CASH_RESERVE_PCT = 0.30        # Minimum 30% cash reserve
MAX_SINGLE_POSITION_PCT = 0.10 # Max 10% of bankroll per position
MAX_CORRELATED_PCT = 0.25      # Max 25% on correlated narrative
MIN_EDGE_CENTS = 5             # Minimum edge to trade (cents)
DEFENSIVE_MIN_EDGE_CENTS = 5   # Edge required in defensive mode
KELLY_FRACTION = 0.5           # Half-Kelly default
STOP_LOSS_CENTS = 15           # Exit if 15c against you
TAKE_PROFIT_THRESHOLD = 0.93   # Consider trimming above this

# Drawdown thresholds
DEFENSIVE_DRAWDOWN_PCT = 0.20  # Enter defensive mode at 20% drawdown
HALT_DRAWDOWN_PCT = 0.35       # Halt new trades at 35% drawdown

# ─── SCANNER PARAMETERS ───────────────────────────────────────────

MIN_LIQUIDITY_USD = 100        # Skip markets with < $100 liquidity
MAX_DAYS_TO_RESOLUTION = 90    # Focus on markets resolving within 90 days
MIN_VOLUME_24H = 50            # Skip very low-volume markets

# ─── CATEGORIES ────────────────────────────────────────────────────
# Which market categories to scan (empty = all)
FOCUS_CATEGORIES = []  # e.g., ["politics", "crypto", "sports"]

# ─── MARKET FILTERS ──────────────────────────────────────────────
# Blacklisted keywords — markets containing these phrases are auto-rejected.
# Case-insensitive substring match on the market question text.
BLACKLIST_KEYWORDS = [
    "jesus christ", "second coming", "rapture", "apocalypse",
    "god will", "messiah return", "end of the world",
    "alien invasion", "ufo disclosure", "flat earth",
    "simulation theory", "time travel",
]

# Minimum edge by time horizon (longer bets need bigger edge to justify capital lockup)
#   short  = resolves within 7 days  (e.g., tonight's NBA game)
#   medium = resolves within 30 days
#   long   = resolves within 90 days
TIME_HORIZON_EDGE = {
    "short":  5,   # 5c min edge  — fast turnover, lower bar
    "medium": 7,   # 7c min edge  — moderate lockup
    "long":  10,   # 10c min edge — long lockup needs bigger payoff
}

# Capital allocation by time horizon (% of deployable capital per bucket)
# This prevents tying up all your money in long-dated bets
TIME_HORIZON_CAPITAL_PCT = {
    "short":  0.50,  # Up to 50% of deployable capital in short-term trades
    "medium": 0.35,  # Up to 35% in medium-term
    "long":   0.15,  # Only 15% in long-dated positions
}

# Minimum market probability for trade consideration
# Markets below 3% or above 97% are usually noise
MIN_PROBABILITY = 0.03
MAX_PROBABILITY = 0.97

# ─── NEWS API ─────────────────────────────────────────────────────
# Get a free key at https://newsapi.org
NEWSAPI_KEY = ""  # Your NewsAPI key

# ─── POLYGONSCAN ──────────────────────────────────────────────────
# Optional: for on-chain sharp trader queries
# Get a free key at https://polygonscan.com/apis
POLYGONSCAN_API_KEY = ""

# ─── MIROFISH CONFIGURATION ──────────────────────────────────────
# MiroFish runs as a Docker container. Start with:
#   cd mirofish-docker && docker compose up -d
# Backend API runs on port 5001, UI on port 3000
MIROFISH_API_URL = "http://localhost:5001"  # MiroFish backend URL

# Legacy settings (kept for reference, Docker .env handles these now)
MIROFISH_PATH = ""  # Not needed when using Docker
MIROFISH_LLM_PROVIDER = "openai"       # Configured in mirofish-docker/.env
MIROFISH_LLM_MODEL = "gpt-4o-mini"     # Configured in mirofish-docker/.env
MIROFISH_LLM_API_KEY = ""              # Configured in mirofish-docker/.env
MIROFISH_LLM_BASE_URL = ""             # Configured in mirofish-docker/.env

# ─── CALIBRATION ─────────────────────────────────────────────────
SHRINKAGE_RATE = 0.15           # Shrink toward 0.5 by this %
SHRINKAGE_MIN_PREDICTIONS = 50  # Stop shrinking after this many resolved
PLATT_MIN_PREDICTIONS = 30      # Start Platt scaling after this many

# ─── DECISION GATE ───────────────────────────────────────────────
GATE_MIN_EDGE_CENTS = 5         # Minimum edge for pipeline to approve
GATE_REQUIRE_SHARP = True       # Require sharp trader agreement
GATE_MIN_CONFIDENCE = "medium"  # Minimum confidence level

# ─── PAPER TRADING ───────────────────────────────────────────────
PAPER_TRADE_MODE = False        # Set True to record paper trades instead of live orders
