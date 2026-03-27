# Polymarket Trading Agent — Complete Setup Guide

This guide walks you through setting up the Polymarket Trading Agent from scratch.

---

## Prerequisites

- **Python 3.8+** (check: `python3 --version`)
- **Git** (check: `git --version`)
- **pip** (check: `pip3 --version`)
- **~100MB disk space** for dependencies + database
- **Polygon wallet** (optional, only needed for live trading)
- **API keys** (optional for read-only mode, required for live trading)

---

## Part 1: Clone & Install (5 minutes)

### 1.1 Clone the Repository

```bash
git clone https://github.com/YOUR-USERNAME/polymarket-trader.git
cd polymarket-trader
```

### 1.2 Create Virtual Environment (Recommended)

```bash
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 1.3 Install Dependencies

```bash
pip install -r requirements.txt
```

This installs:
- `requests` — HTTP library for API calls (required)
- `newsapi-python` — News sentiment signal (optional but recommended)

### 1.4 Initialize Database

```bash
python3 run.py status
```

This creates `polymarket.db` with the full schema (8 tables). Output should show:
```
POLYMARKET TRADING AGENT — STATUS
  Date: 2026-03-26 12:11 UTC
  Bankroll: $500.00
  Markets: 0 active, 0 resolved
  Predictions: 0
```

---

## Part 2: Configuration (10 minutes)

### 2.1 Copy Environment Template

```bash
cp .env.example .env
```

### 2.2 Edit `.env` with Your Credentials

```bash
nano .env  # or vim, open with your editor
```

Fill in:
- **For read-only scanning** (SKIP these steps, use defaults):
  - Leave all API keys empty
  - Leave wallet fields empty

- **For live trading** (required):
  - `POLYMARKET_API_KEY` — Get from Polymarket account
  - `POLYMARKET_SECRET` — Get from Polymarket account
  - `POLYMARKET_PASSPHRASE` — Get from Polymarket account
  - `PRIVATE_KEY` — Your Polygon wallet private key (NEVER share)
  - `WALLET_ADDRESS` — Your Polygon wallet address

- **For news signal** (optional, improves results):
  - `NEWSAPI_KEY` — Get free key at https://newsapi.org (takes 2 minutes)

**IMPORTANT:** `.env` is in `.gitignore` — it will NEVER be committed to GitHub.

### 2.3 (Optional) Edit `config.py` for Advanced Settings

```bash
nano config.py
```

Key settings to consider:
- `BANKROLL = 500.0` — Starting capital in USDC
- `MIN_EDGE_CENTS = 5` — Minimum edge to trade (cents)
- `KELLY_FRACTION = 0.5` — Half-Kelly sizing (conservative)
- `STOP_LOSS_CENTS = 15` — Auto-exit threshold
- `FOCUS_CATEGORIES = []` — Leave empty for all, or `["political", "crypto"]` to focus

---

## Part 3: Seed Calibration Data (2 minutes)

The system learns from past prediction accuracy. Bootstrap it with synthetic data:

```bash
python3 run.py seed
```

Output:
```
Inserted 405 synthetic resolutions

── Calibration Baseline (seeded) ──────────────────────
  Category             N  Brier(mkt)  Brier(ours)   YES%
  -------------------------------------------------------
  political          120      0.0080       0.0030    35%
  crypto              90      0.0072       0.0031    42%
  ...
  TOTAL              405  0.0079      0.0034

  Our edge over market: +56.8%  (positive = better calibrated)
```

This gives you a baseline. **Next step:** Import real resolved markets (see Part 4).

---

## Part 4: Import Real Calibration Data (Optional)

If you have a CSV of resolved Polymarket markets:

```bash
python3 run.py import-resolved --csv resolved_markets_400.csv
```

This improves prediction accuracy by learning from actual outcomes.

---

## Part 5: Test the Pipeline (5 minutes)

### 5.1 Scan for Trade Signals

```bash
python3 run.py scan --top 20
```

Output example:
```
Scanning top 20 markets...

  PASS  |   3c YES | mkt=0.17 est=0.20 | US x Iran ceasefire by March 31?
  PASS  |   5c YES | mkt=0.17 est=0.22 | US forces enter Iran by March 31?
  ...
  TRADE |  10c YES | mkt=0.45 est=0.55 | [SIGNAL IDENTIFIED]
  ...

============================================================
SCAN COMPLETE: 2 actionable trades out of 20 analyzed
```

If you see "0 actionable trades," that's normal — it means no current edges meet the 5c minimum + confidence requirements.

### 5.2 Deep Dive on a Specific Market

```bash
python3 run.py analyze "Iran ceasefire"
```

Output shows:
- Market details (price, liquidity, end date)
- All 5 signal components
- Calibrated probability
- Decision (TRADE or PASS) with reasoning

### 5.3 Monitor Open Positions

```bash
python3 run.py positions
```

Output: Lists all open trades with entry price, current price, P&L. (Will be empty initially.)

### 5.4 View Daily Report

```bash
python3 run.py daily
```

Output: Daily P&L, trades, alerts, risk flags.

---

## Part 6: Execute Your First Trade (Dry-Run, Safe!)

### 6.1 Find a Market

```bash
python3 run.py scan --top 10 --json scan_results.json
```

Pick a market from the output, note its condition ID.

### 6.2 Deep Analyze It

```bash
python3 run.py analyze "your-market-question"
```

Read the decision and thesis carefully.

### 6.3 Execute (Dry-Run First)

```bash
python3 run.py execute <condition_id> YES 50
```

Output:
```
DRY-RUN EXECUTION
  Market : Will Israel launch a major ground offensive in Lebanon by March 31?
  Side   : YES
  Size   : $50.00
  Price  : $0.9985 YES / $0.0015 NO

  [DRY-RUN] Order would be placed for 50 USDC of YES
  Run with --live to execute for real
```

### 6.4 Execute For Real (When Ready)

```bash
python3 run.py execute <condition_id> YES 50 --live
```

⚠️ **This is irreversible. Only use after:**
- Testing with --dry-run
- Verifying API credentials work
- Confirming the market & thesis
- Having sufficient USDC in your wallet

---

## Part 7: Set Up Automated Monitoring

### 7.1 Daily Cron Job (Linux/Mac)

```bash
# Edit crontab:
crontab -e

# Add this line to run scan at 9 AM daily:
0 9 * * * cd /path/to/polymarket-trader && python3 run.py scan --top 30 --json scan_results.json >> scan.log 2>&1
```

### 7.2 Check Positions Periodically

```bash
# Set a reminder to run:
python3 run.py monitor
python3 run.py positions
```

---

## Part 8: Advanced Features (Optional)

### 8.1 MiroFish Simulation

For advanced multi-agent simulations:

```bash
# 1. Clone MiroFish:
git clone https://github.com/666ghj/MiroFish
cd MiroFish
docker-compose up -d  # Or follow their setup

# 2. Update config.py:
MIROFISH_PATH = "/path/to/MiroFish"
MIROFISH_LLM_PROVIDER = "openai"  # or "anthropic"
MIROFISH_LLM_API_KEY = "sk-..."

# 3. Run analysis with MiroFish:
python3 run.py analyze "Iran ceasefire" --mirofish
```

### 8.2 Browser Dashboard

Monitor in real-time:

```bash
# Open in browser:
open dashboard.html  # or double-click the file

# Or serve it:
python3 -m http.server 8000
# Then visit http://localhost:8000/dashboard.html
```

---

## Part 9: Troubleshooting

### Problem: "ModuleNotFoundError: No module named 'requests'"
**Solution:**
```bash
pip install requests
```

### Problem: "polymarket.db-wal: No such file or directory"
**Solution:**
```bash
python3 -c "from db import init_db; init_db()"
```

### Problem: "No tradeable markets found"
**Reasons:**
1. No market data imported yet
2. Market prices at extremes (0.01 or 0.99)
3. Very old market data

**Solution:**
```bash
# Import fresh data:
python3 run.py import market_data.json

# Or wait for next scheduled scrape (if you have one)
```

### Problem: "API credentials invalid"
**Solution:**
```bash
# Verify in .env:
grep POLYMARKET .env

# Test connection:
python3 -c "from api_client import PolymarketClient; print('OK')"
```

### Problem: Can't connect to Gamma API
**Reason:** Network blocked or API down

**Solution:**
```bash
# Test network:
curl https://gamma-api.polymarket.com/markets?limit=1

# Check API status:
# https://polymarket.com (look for any service announcements)
```

---

## Common Workflows

### Workflow 1: One-Time Manual Trading
```bash
# 1. Scan for opportunities
python3 run.py scan --top 30

# 2. Pick a market, analyze deeply
python3 run.py analyze <cid> -p 0.72

# 3. Test with dry-run
python3 run.py execute <cid> YES 50

# 4. If comfortable, execute live
python3 run.py execute <cid> YES 50 --live

# 5. Monitor position
python3 run.py positions
```

### Workflow 2: Automated Daily Scanning
```bash
# Set up cron (see Part 7.1)

# Daily check:
python3 run.py daily
python3 run.py monitor

# Weekly review:
python3 run.py calibration
python3 run.py portfolio
```

### Workflow 3: Post-Trade Analysis
```bash
# After a trade closes:
python3 run.py post-mortem <trade_id>

# Check calibration:
python3 run.py calibration

# Review portfolio:
python3 run.py portfolio
```

---

## File Structure

```
polymarket-trader/
├── run.py                    # Main CLI entry point
├── config.py                 # Configuration (edit for settings)
├── .env.example              # Credentials template (copy to .env)
├── .gitignore                # Git ignore rules (don't edit)
├── requirements.txt          # Python dependencies
├── README.md                 # Project overview
├── SETUP.md                  # This file
├── HANDOFF.md                # Evan's detailed guide
├── GITHUB_PUSH_GUIDE.md      # How to push to GitHub
│
├── db.py                     # Database layer (8 tables)
├── pipeline.py               # 6-stage pipeline (the brain)
├── scanner.py                # Market scanner
├── preprocessor.py           # Signal normalization
├── calibration.py            # Brier score + Platt scaling
├── executor.py               # Trade execution
├── journal.py                # Position tracking
├── risk_manager.py            # Half-Kelly sizing
├── api_client.py             # Polymarket API wrapper
├── scraper.py                # Market data scraper
├── mirofish_wrapper.py        # MiroFish simulation (optional)
│
├── signals/                  # Signal modules
│   ├── __init__.py
│   ├── sharp_traders.py      # Whale tracking
│   ├── news.py               # News sentiment
│   ├── base_rate.py          # Historical frequencies
│   ├── cross_platform.py     # Metaculus/Manifold consensus
│   └── sentiment.py          # Social media signals
│
├── dashboard.html            # Browser monitoring dashboard
├── polymarket.db             # SQLite database (local, not in git)
└── .git/                     # Git history (don't edit)
```

---

## Next Steps

1. ✅ Run `python3 run.py status` to verify setup
2. ✅ Run `python3 run.py scan --top 20` to see the pipeline in action
3. ✅ Analyze a market with `python3 run.py analyze "your-question"`
4. ✅ Try a dry-run trade with `python3 run.py execute <cid> YES 50`
5. ⚠️ Only execute live (`--live`) after feeling confident

---

## Support & Questions

- **Architecture:** See README.md
- **Advanced Setup:** See HANDOFF.md (Evan's guide)
- **GitHub Issues:** Create an issue on GitHub for bugs/features
- **Code Questions:** Check module docstrings (top of each .py file)

---

## Final Reminders

- **Never commit `.env`** — it's in .gitignore for a reason
- **Never share private keys** — keep them local only
- **Start small** — use dry-run before live trading
- **Monitor regularly** — check positions daily if you have open trades
- **Learn from outcomes** — review post-mortems to improve

Good luck! 🚀
