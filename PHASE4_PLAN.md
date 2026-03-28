# Phase 4: Signal Fusion — Implementation Plan

## What We Have (Already Built)

| Signal | File | Weight | Status |
|--------|------|--------|--------|
| Sharp Traders (7 whale wallets) | `signals/sharp_traders.py` | 0.30 | ✅ Working |
| News Sentiment (NewsAPI) | `signals/news.py` | 0.25 | ✅ Working (needs API key) |
| Base Rate (historical) | `signals/base_rate.py` | 0.25 | ✅ Working |
| Cross-Platform (Metaculus/Manifold) | `signals/cross_platform.py` | 0.15 | ✅ Working |
| MiroFish Swarm Simulation | `mirofish_wrapper.py` | 60/40 blend | ✅ Working (Groq) |

Current fusion: simple weighted average in `preprocessor.py` with fixed weights.

## What Phase 4 Builds

### Task 1: Create `signal_fusion.py` (New File)
**Purpose:** Replace the simple weighted average with an adaptive fusion engine.

```
Input:  5 raw signal scores + market metadata
Output: fused_probability, confidence_score, signal_report
```

**Key features:**
- Dynamic weight adjustment per market type
- Time-decay for all signals (not just news)
- Confidence-weighted blending with MiroFish
- Disagreement detection and handling

### Task 2: Market-Type Weight Profiles
Different markets need different signal weights:

| Market Type | Sharp Traders | News | Base Rate | Cross-Platform | MiroFish |
|-------------|:---:|:---:|:---:|:---:|:---:|
| Politics | 0.30 | 0.20 | 0.20 | 0.10 | 0.20 |
| Crypto | 0.20 | 0.30 | 0.15 | 0.15 | 0.20 |
| Sports | 0.15 | 0.15 | 0.35 | 0.15 | 0.20 |
| Science/Tech | 0.25 | 0.20 | 0.20 | 0.15 | 0.20 |
| Default | 0.25 | 0.20 | 0.20 | 0.15 | 0.20 |

### Task 3: Time-Decay for All Signals
Currently only news has time decay. Add decay to:
- Sharp trader positions (when was the trade placed?)
- Base rate comparables (how old are the resolved markets?)
- Cross-platform prices (when was the last update?)
- MiroFish results (cache expiry after X hours)

### Task 4: Adaptive MiroFish Blending
Current: fixed 60% MiroFish / 40% signal aggregate.

New logic:
- If MiroFish and signals **agree** (within 10%): weight MiroFish higher (70/30)
- If they **disagree moderately** (10-25%): equal weight (50/50)
- If they **strongly disagree** (>25%): weight signals higher (40/60) + flag for review
- If MiroFish confidence is low: reduce its weight proportionally

### Task 5: Enhanced Confidence Scoring
Replace the current 3-bucket system (HIGH/MEDIUM/LOW) with a continuous 0-1 score:

```python
confidence = (
    signal_agreement_score * 0.3 +    # How much signals agree
    data_completeness * 0.25 +          # How many signals have data
    sample_quality * 0.25 +             # Quality of underlying data
    historical_accuracy * 0.20          # Past accuracy of this signal combo
)
```

### Task 6: Signal Quality Feedback Loop
Track which signals were right/wrong per market type:
- After market resolves, score each signal's prediction
- Compute per-signal Brier scores by category
- Gradually adjust weights based on track record
- Store in SQLite via existing `db.py`

### Task 7: Update Pipeline Integration
Modify `pipeline.py` to use new `signal_fusion.py`:
- Replace `preprocessor.preprocess_signals()` call with `signal_fusion.fuse()`
- Pass market metadata (category, time_to_resolution) to fusion engine
- Update DecisionGate to use continuous confidence score
- Preserve backward compatibility (old preprocessor still works as fallback)

## Implementation Order

```
Step 1: signal_fusion.py — core fusion engine with market-type profiles
Step 2: Update preprocessor.py — add time-decay to all signals
Step 3: Adaptive MiroFish blending in signal_fusion.py
Step 4: Enhanced confidence scoring (continuous 0-1)
Step 5: Pipeline integration — wire signal_fusion into pipeline.py
Step 6: Signal feedback tables in db.py
Step 7: End-to-end test with sample markets
```

## Files to Create/Modify

| Action | File | What Changes |
|--------|------|-------------|
| **CREATE** | `signal_fusion.py` | New fusion engine (~300 lines) |
| MODIFY | `preprocessor.py` | Add time-decay to all signals |
| MODIFY | `pipeline.py` | Wire in signal_fusion, update DecisionGate |
| MODIFY | `db.py` | Add signal_accuracy tracking table |
| MODIFY | `config.py` | Add fusion config params |
| **CREATE** | `tests/test_fusion.py` | Unit tests for fusion logic |

## After Phase 4 → Phase 5 (Testing)

Once fusion is built, testing involves:
1. Run predictions on 50+ resolved markets from the database
2. Compare fused predictions vs actual outcomes (Brier scores)
3. Tune weights based on results
4. A/B test: old simple average vs new fusion engine
5. Paper trade for 1-2 weeks before live trading

## API Keys Needed

- **Groq** (LLM for MiroFish): ✅ Have it
- **Zep** (Knowledge graph): ✅ Have it
- **NewsAPI**: ❌ Need to get (free tier: 100 requests/day at newsapi.org)
- **Polymarket CLOB API**: ❌ Need for live trading (Phase 6)

## Estimated Effort

- Phase 4 coding: ~3-4 hours
- Phase 5 testing: ~2-3 hours
- Phase 6 live setup: ~1-2 hours
