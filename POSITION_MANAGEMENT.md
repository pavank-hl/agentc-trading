# Position Management — Two-Prompt Architecture

## Overview

The trading system uses two separate LLM prompts per cycle:

1. **Prompt 1 (Analysis):** Pure market analysis. Always outputs LONG or SHORT for every symbol with confidence 0-100 and full trade params. No awareness of existing positions.
2. **Prompt 2 (Position Management):** Only runs when positions exist. Receives the analysis result + current position data. Compares and decides: HOLD, CLOSE, or output a new direction (for reversals/opens).

Both prompts output the same `{"decisions": [...]}` JSON format, so `submit_decision()` works unchanged for both paths.

## Flow

```
1. prompt = system.get_prompt()                    # Analysis prompt
2. LLM analyzes → analysis_json                    # Call #1: always LONG/SHORT
3. Fetch positions via GET /v1/account/positions
4. IF positions exist:
     pos_prompt = system.get_position_prompt(analysis_json, positions_json)
     LLM evaluates → decision_json                 # Call #2: HOLD/CLOSE/LONG/SHORT
     result = system.submit_decision(decision_json)
   ELSE:
     result = system.submit_decision(analysis_json) # Use analysis directly
5. Execute approved trades (close first if reversing, then open)
6. Verify via GET /v1/account/positions
```

## Decision Matrix

### When a Position EXISTS

| Analysis Direction | Confidence | Output | Rationale |
|---|---|---|---|
| Same as position | >= 50 | HOLD | Thesis confirmed, let SL/TP handle exit |
| Same as position | < 50 | CLOSE | Thesis weakening, exit before SL |
| Opposite of position | < 50 | HOLD | Weak opposing signal, not enough to reverse |
| Opposite of position | >= 50 | Output new direction (LONG/SHORT) | Strong opposing signal, reverse |

### When NO Position Exists

| Confidence | Output | Rationale |
|---|---|---|
| >= 40 | Output direction (LONG/SHORT with trade params) | Sufficient quality to open |
| < 40 | HOLD | Signal too weak to enter |

## Reversal Execution

When Prompt 2 outputs LONG but the agent has a SHORT position (or vice versa):

1. **Close existing position** → `POST /v1/intent` with `{"intent": "close", "market": "ETH", "userWallet": "0x..."}`
2. **Verify closure** → `GET /v1/account/positions/ETH` (should return `position: null`)
3. **Open new position** → `POST /v1/intent` with `{"intent": "trade", ...}` using the trade params from the decision

## Confidence Thresholds

| Context | Threshold | Action |
|---|---|---|
| Existing position, same direction | >= 50 | HOLD (confirmed) |
| Existing position, same direction | < 50 | CLOSE (weakening) |
| Existing position, opposite direction | >= 50 | Reverse (close + open) |
| Existing position, opposite direction | < 50 | HOLD (weak signal) |
| No position | >= 40 | Open new position |
| No position | < 40 | HOLD (too weak) |

## Leverage: Percentage of Market Max

The `leverage_pct` config (default 100, set via `LEVERAGE_PCT` env var) defines what percentage of the market's max leverage to use.

**Example:** ETH max leverage is 50x. If `leverage_pct=50`, the effective max = 25x.

Quality-gated leverage (as % of effective max):

| Score | Leverage Range |
|---|---|
| 75-100 | 80-100% of effective max |
| 55-74 | 50-75% of effective max |
| 40-54 | 20-45% of effective max |
| <40 | Minimum viable (enough for $10.50 notional) |

The effective max leverage per symbol is included in the analysis prompt's market data.
