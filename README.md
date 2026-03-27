# Polymarket Trading Agent

An autonomous prediction market trading system for [Polymarket](https://polymarket.com). Combines multi-signal fusion, MiroFish swarm simulation, sharp trader copy-trading, and disciplined risk management into a 6-stage pipeline.

---

## Architecture

```
Market Selector → Signal Ingestion → Preprocessing → MiroFish Sim → Calibration → Decision Gate → Executor
```

**6 Signal Sources:**
1. **Sharp Traders** — 7 identified whale wallets ($85M+ combined profit) tracked via Polymarket Data API
2. **News** — NewsAPI real-time sentiment scoring
3. **Base Rate** — Historical resolution rates from 400+ resolved markets by category
4. **Cross-Platform** — Metaculus + Manifold consensus delta
5. **Sentiment** — Social signal (supplementary)

**Risk Management:** Half-Kelly sizing, 30% minimum cash reserve, 10% max per position, 15c stop-loss, defensive/halt modes at 20%/35% drawdown.

---

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url>
cd polymarket-trader

# 2. Install dependencies
pip install requests

# 3. Configure (no keys needed for read-only scanning)
# Edit config.py to add optional: NewsAPI key, Polygonscan key, MiroFish path

# 4. Initialize the database and seed calibration
python run.py status
python run.py seed

# 5. Scan for trade signals
python run.py scan --top 20
```

---

## CLI Reference

```
python run.py status                          System overview
python run.py scan --top 20                   Scan top 20 markets for signals
python run.py analyze "Iran ceasefire"        Analyze a market by keyword
python run.py analyze <cid> -p 0.72           Analyze with your probability estimate
python run.py analyze <cid> -p 0.72 --mirofish  Full analysis with MiroFish simulation
python run.py execute <cid> YES 50            Execute $50 YES trade (dry-run)
python run.py execute <cid> YES 50 --live     Execute for real
python run.py monitor                         Check open positions for stop/take triggers
python run.py positions                       List open positions with unrealized P&L
python run.py daily                           Daily P&L report
python run.py post-mortem <trade_id>          Post-mortem analysis of a closed trade
python run.py portfolio                       Portfolio summary
python run.py calibration                     Calibration stats & Brier curve
python run.py seed                            Bootstrap calibration with synthetic data
python run.py import-resolved [--csv file]    Import resolved markets for real calibration
python run.py import markets.json             Import active market data from JSON
```

---

## Project Structure

```
├── run.py                   # Main CLI — all commands
├── config.py                # Settings, credentials, risk params
├── db.py                    # SQLite schema (8 tables)
├── pipeline.py              # 6-stage pipeline orchestrator
│
├── signals/
│   ├── sharp_traders.py     # 7 whale wallets + Data API fetcher
│   ├── news.py              # NewsAPI sentiment scoring
│   ├── base_rate.py         # Historical base rates from DB
│   └── cross_platform.py   # Metaculus + Manifold consensus
│
├── risk_manager.py          # Half-Kelly sizing, drawdown tracking
├── calibration.py           # Brier score, shrinkage, Platt scaling
├── preprocessor.py          # Signal normalization & weighting
├── mirofish_wrapper.py      # MiroFish swarm simulation wrapper
│
├── executor.py              # Trade execution, order building, iceberg splitting
├── journal.py               # Position tracking, P&L reporting
├── scanner.py               # Market scanning & filtering
├── scraper.py               # Gamma API market scraper
├── api_client.py            # Polymarket CLOB + Gamma API client
│
├── seed_calibration.py      # Bootstrap calibration with synthetic data
├── import_resolved.py       # Import resolved markets CSV
└── dashboard.html           # Browser-based live dashboard
```

---

## Sharp Traders

Seven identified whale wallets — believed to be the same entity ("Theo4 cluster") coordinated via Kraken funding. Combined $85M+ in profits, primarily on US politics markets.

| Handle | Est. P&L | Win Rate |
|--------|----------|----------|
| Theo4 | $22M | 89% |
| Fredi9999 | $16.6M | 73% |
| zxgngl | $11.4M | 80% |
| Len9311238 | $8.7M | 100% |
| RepTrump | $7.5M | 100% |
| PrincessCaro | $6.1M | 100% |
| walletmobile | $5.9M | 100% |

Wallet addresses and notes are in `signals/sharp_traders.py`. The system queries `data-api.polymarket.com/positions` — no API key required.

---

## Configuration

Edit `config.py`:

```python
BANKROLL = 500.0           # Your starting bankroll in USDC

# Optional — enables more signals
NEWSAPI_KEY = ""           # newsapi.org
POLYGONSCAN_API_KEY = ""   # polygonscan.com — for on-chain queries

# MiroFish — swarm AI simulation via Docker
MIROFISH_API_URL = "http://localhost:5001"  # MiroFish backend URL
```

### MiroFish Docker Setup

```bash
cd mirofish-docker/
# Edit .env with your keys:
#   LLM_API_KEY=sk-your-openai-key
#   ZEP_API_KEY=your-zep-key  (from https://app.getzep.com)
docker compose up -d
# Health check: curl http://localhost:5001/health
```

**Important:** Never commit real credentials. The `.env` files and `config.py` credential fields should remain empty in the repo. See `HANDOFF.md` for detailed setup instructions.

---

## Calibration

The system tracks Brier scores from day one to improve over time:
- **Shrinkage** — probabilities shrink toward 0.5 until 50 resolved predictions
- **Platt scaling** — logistic recalibration after 30+ resolved predictions
- **Category stratification** — separate calibration curves per market type

Seed synthetic calibration data with `python run.py seed`, then replace with real data using `python run.py import-resolved`.

---

## Decision Gate

A trade signal only passes if ALL conditions are met:
- Edge ≥ 8 cents (our probability vs. market price)
- Confidence ≥ Medium
- Position size within Half-Kelly limits
- Cash reserve stays ≥ 30% after the trade

---

## Dependencies

```
requests       # pip install requests
sqlite3        # stdlib — no install needed
```

Optional (enables additional signals):
```
newsapi-python    # News signal: pip install newsapi-python
```

---

## Phase Roadmap

- **Phase 1** ✅ — Full 6-stage pipeline, risk management, execution, calibration, journal
- **Phase 2** ✅ — Sharp trader tracker (7 wallets, Data API, consensus signal)
- **Phase 3** ✅ — MiroFish Docker setup + HTTP API wrapper (mirofish_wrapper.py)
- **Phase 4** 🔜 — Signal fusion layer (combine sharp traders + MiroFish + sentiment + base rates)
- **Phase 5** — Calibration with real resolved markets + decision gate tuning
- **Phase 6** — Cloud deployment (AWS Lambda / GCP Cloud Run)

---

*Trades are at your own risk — this is not financial advice.*
