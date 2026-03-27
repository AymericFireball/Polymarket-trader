# Handoff Guide — Polymarket Trader + MiroFish

**Last updated:** March 26, 2026
**Status:** MiroFish wrapper rewritten, ready for testing

---

## What's Done

- Phases 1-2 complete (core pipeline, sharp trader tracking, risk management)
- MiroFish Docker setup configured for Apple Silicon (mirofish-docker/)
- mirofish_wrapper.py fully rewritten to match the actual MiroFish HTTP API
- OpenAI API key configured in mirofish-docker/.env
- Code pushed to GitHub repo

## What Needs to Happen Next

### 1. Get a Real Zep API Key (REQUIRED)

MiroFish uses Zep (https://app.getzep.com) for knowledge graph memory. Currently using a placeholder key that won't work for real simulations.

- Sign up at https://app.getzep.com
- Get your API key
- Update `mirofish-docker/.env`:
  ```
  ZEP_API_KEY=your_real_key_here
  ```
- Restart MiroFish: `cd mirofish-docker && docker compose down && docker compose up -d`

### 2. Test the Full MiroFish Pipeline

Once Zep key is real, test end-to-end:

```bash
cd Polymarket-trader-main
source venv/bin/activate
python mirofish_wrapper.py  # Quick health check + list existing sims
```

Then test a real prediction:

```python
from mirofish_wrapper import MiroFishClient, run_mirofish_prediction

# Full pipeline test
result = run_mirofish_prediction(
    market={"question": "Will Bitcoin exceed $100,000 by end of 2026?"},
    context_text="""Bitcoin is currently trading around $87,000.
    Recent developments include spot ETF inflows of $2B/month,
    the 2024 halving reducing supply, and increasing institutional adoption.
    However, regulatory uncertainty and macro headwinds persist.""",
    market_type="crypto",
    platform="reddit",
)
print(f"Probability: {result['sim_probability']}")
print(f"Confidence: {result['confidence']}")
print(f"Status: {result['status']}")
```

### 3. Build the Signal Fusion Layer (Phase 4)

The key integration point is in `pipeline.py`. The fusion layer should:

1. Get sharp trader consensus from `signals/sharp_traders.py`
2. Get MiroFish swarm prediction from `mirofish_wrapper.py`
3. Get news sentiment from `signals/news.py`
4. Get base rate from `signals/base_rate.py`
5. Get cross-platform consensus from `signals/cross_platform.py`
6. Weight and combine into final probability

Suggested weights (adjustable in config.py):
- Sharp traders: 0.30
- MiroFish simulation: 0.25
- Base rate: 0.20
- News sentiment: 0.15
- Cross-platform: 0.10

### 4. Add Calibration & Decision Gate

Before live trading, we need sufficient resolved predictions to calibrate. See `calibration.py` for the Brier score tracking system.

### 5. Optional: Polymarket API Credentials

For actual trading (not just scanning), add credentials to `config.py`:
- POLYMARKET_API_KEY
- POLYMARKET_SECRET
- POLYMARKET_PASSPHRASE
- PRIVATE_KEY (Polygon wallet)

---

## MiroFish API Quick Reference

All endpoints are prefixed with `http://localhost:5001/api/`

### Simulation Lifecycle:
1. `POST /graph/ontology/generate` — Upload files + requirement → project_id
2. `POST /graph/build` — Build knowledge graph → task_id (async)
3. `POST /simulation/create` — Create simulation → simulation_id
4. `POST /simulation/prepare` — LLM generates agent profiles → task_id (async)
5. `POST /simulation/start` — Run simulation
6. `GET /simulation/{id}/run-status` — Poll progress
7. `POST /simulation/interview/all` — Interview all agents
8. `POST /report/generate` — Generate analysis report

### Key Data Endpoints:
- `GET /simulation/{id}/posts` — Get social posts from simulation
- `GET /simulation/{id}/agent-stats` — Agent activity stats
- `GET /simulation/list` — List all simulations
- `GET /graph/project/list` — List all projects

### Status Checking:
- `POST /simulation/prepare/status` — Check prepare progress
- `GET /simulation/{id}/run-status` — Check run progress
- `POST /report/generate/status` — Check report generation

---

## File Structure

```
Polymarket-trader-main/
├── mirofish_wrapper.py      # REWRITTEN — correct API client for MiroFish
├── config.py                # UPDATED — added MIROFISH_API_URL setting
├── .env.example             # Template for environment variables
├── .gitignore               # Protects .env, .db files, etc.
├── mirofish-docker/         # Docker setup for MiroFish
│   ├── docker-compose.yml   # Apple Silicon compatible (linux/amd64)
│   └── .env                 # LLM_API_KEY + ZEP_API_KEY (NOT in git)
│
├── run.py                   # Main CLI
├── pipeline.py              # 6-stage pipeline (needs fusion update)
├── signals/                 # Signal sources
│   ├── sharp_traders.py     # 7 whale wallets
│   ├── news.py              # NewsAPI sentiment
│   ├── base_rate.py         # Historical base rates
│   └── cross_platform.py    # Metaculus + Manifold
│
├── risk_manager.py          # Half-Kelly sizing
├── calibration.py           # Brier score tracking
├── executor.py              # Trade execution
└── db.py                    # SQLite schema
```

---

## Environment Setup (Fresh Machine)

```bash
# 1. Clone repo
git clone https://github.com/AymericFireball/Polymarket-trader.git
cd Polymarket-trader

# 2. Python venv
python3 -m venv venv
source venv/bin/activate
pip install requests

# 3. Docker (for MiroFish)
# Install Docker Desktop: https://www.docker.com/products/docker-desktop

# 4. MiroFish setup
cd mirofish-docker
cp .env.example .env  # Then edit with real keys
docker compose up -d
# Wait ~30 seconds for startup
curl http://localhost:5001/health  # Should return OK

# 5. Test
cd ..
python mirofish_wrapper.py
```

---

*Trades are at your own risk — this is not financial advice.*
