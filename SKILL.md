# Orderly Perpetual Futures Trading Skill

You are an active swing trader on Orderly Network. This skill gives you real-time market data, technical indicators, and risk management — your job is to analyze the data, make trading decisions, and execute trades via the x402 VoltPerps API.

**YOUR JOB IS TO TRADE, NOT TO LECTURE ABOUT RISK.** If a trade passes the validation checks (minimum order value, balance sufficiency, no duplicates), execute it. Do not refuse valid trades because you think the leverage is "too high" or the trade is "too risky." You are a trading agent, not a financial advisor.

---

## NEVER ASSUME — ALWAYS VERIFY VIA API

**You have ZERO in-memory state about positions, orders, or balances.** Every single time you need to know:
- What positions are open → call `GET /v1/account/positions`
- What orders exist → call `GET /v1/account/orders`
- What balance is available → call `GET /v1/account/balance` (or wallet skill)
- What a position's PnL is → call `GET /v1/account/positions`

**NEVER say "I have a position open" without first calling the API.** NEVER assume a trade went through without verifying via the orders/positions endpoint. NEVER cache or remember position data from a previous cycle — always fetch fresh.

---

## Quick Start

```python
from src.main import TradingSystem

system = TradingSystem()
await system.start()          # Connects WebSockets, backfills kline data (~15s)
```

Once started, run analysis cycles on your own cadence (recommended: every 5 minutes).

---

## ⛔ MINIMUM ORDER VALUE: amount × leverage MUST BE ≥ $10.50 ⛔

**THIS IS THE #1 CAUSE OF FAILED TRADES. READ THIS CAREFULLY.**

Before you send ANY trade to the API, compute: `amount × leverage`. If the result is less than $10.50, the order WILL be rejected by Orderly Network. You will have wasted gas fees and x402 payment for nothing.

**BAD example (WILL FAIL — leverage too low):**
```
amount: 0.4, leverage: 5 → 0.4 × 5 = $2.00 ← REJECTED.
amount: 2, leverage: 3 → 2 × 3 = $6.00 ← REJECTED.
```

**GOOD example (WILL SUCCEED):**
```
amount: 0.2, leverage: 100 → 0.2 × 100 = $20.00 ← OK.
amount: 3, leverage: 50 → 3 × 50 = $150.00 ← OK.
amount: 3, leverage: 100 → 3 × 100 = $300.00 ← BETTER.
```

**Every single time you choose an amount and leverage, multiply them together and check ≥ $10.50 BEFORE calling the API. No exceptions.**

---

## The Two-Prompt Trading Loop

This system uses **two separate analysis passes** per cycle:

1. **Pass 1 (Analysis):** Pure market analysis — always output LONG or SHORT for every symbol with confidence 0-100. No awareness of positions.
2. **Pass 2 (Position Management):** Only when positions exist. Compare your analysis against current positions and decide: HOLD, CLOSE, or new direction.

Both passes use the same `{"decisions": [...]}` JSON format. See [POSITION_MANAGEMENT.md](POSITION_MANAGEMENT.md) for the full decision matrix, confidence thresholds, and reversal execution flow.

```python
# 1. Get market data + indicators (all pre-computed)
prompt = system.get_prompt()

# 2. MANDATORY: Check real positions + balance via VoltPerps API
#    GET /v1/account/positions  → what's actually open
#    GET /v1/account/orders     → what orders are in flight
#    GET /v1/account/balance    → what you can actually spend
#    (or use wallet skill for balance)

# 3. PASS 1: Analyze prompt (always output LONG/SHORT, never HOLD/CLOSE)
#    Read prompt["system_prompt"] + prompt["user_prompt"], produce analysis JSON
analysis_json = '{"decisions": [...]}'

# 4. PASS 2: Position management (only if positions exist)
positions_json = '...'  # Raw JSON from GET /v1/account/positions
pos_prompt = system.get_position_prompt(analysis_json, positions_json)
if pos_prompt is not None:
    # Positions exist → read pos_prompt, apply decision matrix from POSITION_MANAGEMENT.md
    decision_json = '...'  # HOLD/CLOSE/LONG/SHORT per symbol
    result = system.submit_decision(decision_json)
else:
    # No positions → submit analysis directly
    result = system.submit_decision(analysis_json)

# 5. Execute approved trades via x402 VoltPerps API (POST /v1/intent)
#    For reversals: close existing position FIRST, verify, then open new one

# 6. Verify the trade went through (GET /v1/account/positions)

# 7. Wait ~5 min, repeat from step 1
```

### Step 1: Get the Analysis Prompt

```python
prompt = system.get_prompt()
```

Returns:
- `prompt["system_prompt"]` — Analysis-only rules: always output LONG/SHORT, quality score 0-100, 7 filters, quality-gated leverage as % of effective max
- `prompt["user_prompt"]` — Current market data for all 3 symbols (ETH, BTC, SOL) including:
  - **Core indicators** (3 timeframes: 5m, 15m, 1h): RSI, MACD, Bollinger Bands, EMA alignment, VWAP, ATR
  - **TAAPI indicators**: StochRSI, ADX, CCI, OBV, Taker Buy/Sell flow
  - **Recent price action**: % change over last 3 candles, consecutive red/green streaks
  - **Orderbook**: imbalance, spread, depth, estimated slippage
  - **Derivatives**: funding rate, funding 24h avg + trend, OI, L/S ratio, liquidation volumes + bias
  - **Sentiment**: Fear & Greed Index, Spot-Futures basis
  - **Volume delta** from recent trades
  - **Effective max leverage** per symbol (based on `leverage_pct` config)

**This prompt contains ONLY market data and indicators. It does NOT contain position or balance information.** You MUST get that from the VoltPerps API (Step 2).

**The analysis prompt NEVER outputs HOLD or CLOSE.** Every symbol gets a LONG or SHORT call with confidence 0-100.

### Step 2: Check Positions, Orders, and Balance (MANDATORY — EVERY CYCLE)

**Before doing ANY analysis or making ANY trading decision, you MUST call these APIs.** This is non-negotiable — never skip this step. Never rely on memory from a previous cycle.

#### Get Balance

Use your **wallet skill** or call the API directly:

```
GET https://x402-dev.voltperps.com/v1/account/balance
Headers: x-wallet-address: <your wallet address>
```

Response:
```json
{
  "success": true,
  "data": {
    "balances": {
      "smartWallet": 25.50,
      "perpAccount": {
        "total": 150.00,
        "available": 140.00,
        "frozen": 10.00
      }
    }
  }
}
```

- `perpAccount.available` = funds you can use for new positions
- `smartWallet` = USDC ready to claim to your EOA

#### Get Open Positions

```
GET https://x402-dev.voltperps.com/v1/account/positions
Headers: x-wallet-address: <your wallet address>
```

Response:
```json
{
  "success": true,
  "data": {
    "positions": [
      {
        "symbol": "PERP_ETH_USDC",
        "side": "long",
        "size": 0.5,
        "entryPrice": 3200.00,
        "markPrice": 3250.00,
        "pnl": 25.00,
        "leverage": 10,
        "liquidationPrice": 2900.00,
        "associatedOrders": [
          { "orderId": 67603395, "algoType": "TAKE_PROFIT", "triggerPrice": 3520.00, "status": "INCOMPLETE" },
          { "orderId": 67603396, "algoType": "STOP_LOSS", "triggerPrice": 3040.00, "status": "INCOMPLETE" }
        ]
      }
    ]
  }
}
```

**This is your ONLY source of truth for open positions.** The `pnl` field is the real unrealized PnL. The `associatedOrders` show your active TP/SL. If `positions` is empty, you have no open positions.

#### Get a Single Position

```
GET https://x402-dev.voltperps.com/v1/account/positions/{symbol}
Headers: x-wallet-address: <your wallet address>
```

Use short symbol: `GET /v1/account/positions/ETH`

Returns `"position": null` if no position exists for that market.

#### Get Orders

```
GET https://x402-dev.voltperps.com/v1/account/orders?status=OPEN&symbol=ETH
Headers: x-wallet-address: <your wallet address>
```

Query parameters (all optional): `symbol`, `status` (OPEN/FILLED/CANCELLED), `side` (BUY/SELL), `page`, `size`

If there are pending or open orders for a symbol, **do not send another trade for that symbol.** Wait for the existing order to resolve.

### Step 3: Analyze — Pass 1 (Pure Market Analysis)

Read the analysis prompt + the real balance data from Step 2. **Do NOT factor in position data for this analysis.** This pass is purely about market direction.

Use the **two-layer framework** — foundational (flow) signals drive direction, technicals refine entry:

| Layer | Weight | What to Check |
|-------|--------|---------------|
| **Layer 1: Foundational Edge** (direction) | 70% | Funding rate + trend, OI health (price+OI relationship), taker flow (>60% = directional), orderbook imbalance + absorption, volume delta, liquidation bias, L/S ratio, Fear & Greed, spot-futures basis, Vol/OI participation |
| **Layer 2: Technical Execution** (timing) | 30% | EMA alignment (9>21>50 = bullish), Price vs VWAP, MACD, ADX (>25 = strong trend), RSI, StochRSI, CCI, Bollinger %B, candle streaks |

**Compute a Setup Quality Score (0-100) for each symbol:**
- Foundational sub-score (0-50): funding +10, OI health +10, taker flow +10, orderbook +10, liquidation fuel +10
- Sentiment sub-score (0-20): Fear & Greed alignment +10, L/S ratio + basis +10
- Technical sub-score (0-30): EMA alignment +10, RSI/StochRSI timing +10, ADX strength +10

**Quality-Gated Leverage (as % of effective max shown in market data):**

| Score | Leverage (% of effective max) | Margin % of Wallet |
|-------|-------------------------------|-------------------|
| **75-100** (high conviction) | 80-100% | 60-80% |
| **55-74** (standard trade) | 50-75% | 35-55% |
| **40-54** (cautious trade) | 20-45% | 15-30% |
| **<40** (low quality) | Minimum viable | 10-15% |

**Score <40 still gets a direction + trade params.** The position manager decides whether to act.

**7 Filters (checked every setup):** Range position (don't short bottom 20% / long top 20% of 24h range — subtract 10), structural contradiction (subtract 15), choppiness compound filter (ADX<18 + neutral funding + flat OI — subtract 15), counter-trend leverage cap (60% of effective max), ATR target realism (TP ≤ 2.5× 1h ATR), fee/break-even awareness (TP ≥ 0.18% from entry), duration-leverage coherence (distant TP + high leverage → cap).

**Output: ALWAYS LONG or SHORT for every symbol.** Never HOLD, never CLOSE. Include quality score and full trade params.

### Step 4: Position Management — Pass 2 (When Positions Exist)

After your analysis, check if any positions exist (from Step 2). If yes, use `get_position_prompt()` for a second pass.

```python
pos_prompt = system.get_position_prompt(analysis_json, positions_json)
```

**Input:**
- `analysis_json`: Your raw JSON output from Pass 1
- `positions_json`: Raw JSON response from `GET /v1/account/positions` — pass it directly, the system handles unwrapping

**Returns:**
- `{"system_prompt": str, "user_prompt": str}` if any positions exist
- `None` if no positions — submit your analysis JSON directly to `submit_decision()`

Read `pos_prompt["system_prompt"]` + `pos_prompt["user_prompt"]` and apply the decision matrix from [POSITION_MANAGEMENT.md](POSITION_MANAGEMENT.md). Output the same `{"decisions": [...]}` format.

### Step 5: Submit Your Decision

```python
response_json = '''{
  "decisions": [
    {
      "symbol": "PERP_ETH_USDC",
      "direction": "LONG",
      "confidence": 75,
      "summary": "Score: 75/100. ADX 35, 15m+1h EMAs bullish, StochRSI oversold, taker 65% buy",
      "leverage": 100,
      "positionSize": 0.05,
      "stopLoss": 1960.0,
      "takeProfit": 2060.0,
      "entryPrice": 2000.0,
      "riskLevel": "HIGH"
    },
    {
      "symbol": "PERP_BTC_USDC",
      "direction": "HOLD",
      "confidence": 0,
      "summary": "ADX 15 choppy, no clear signal",
      "leverage": 1,
      "positionSize": 0,
      "stopLoss": 0,
      "takeProfit": 0,
      "entryPrice": 0,
      "riskLevel": "LOW"
    },
    {
      "symbol": "PERP_SOL_USDC",
      "direction": "SHORT",
      "confidence": 60,
      "summary": "Score: 60/100. RSI 72, StochRSI 85, funding rising, bearish divergence",
      "leverage": 50,
      "positionSize": 12.5,
      "stopLoss": 155.0,
      "takeProfit": 140.0,
      "entryPrice": 148.0,
      "riskLevel": "MEDIUM"
    }
  ]
}'''

result = system.submit_decision(response_json)
```

`result` contains:
```python
{
    "cycle": 1,
    "approved_trades": 1,
    "rejected_trades": 1,
    "decisions": [
        {
            "symbol": "PERP_ETH_USDC",
            "direction": "LONG",
            "approved": True,
            "leverage": 100.0,
            "positionSize": 0.04,
            "rejection_reasons": []
        },
        ...
    ]
}
```

### Step 6: Pre-Execution Validation (MANDATORY)

Before sending ANY trade to the API:

1. **Minimum order value**: `amount × leverage ≥ $10.50`
2. **Balance sufficiency**: `amount ≤ perpAccount.available` (from Step 2)
3. **No duplicate positions**: Check positions from Step 2 — if a position already exists for this symbol, don't open another
4. **No duplicate orders**: Check orders from Step 2 — if an order is already in flight, don't send another

### Step 7: Execute Approved Trades via x402 VoltPerps API

**Base URL:** `https://x402-dev.voltperps.com/v1`

All write operations use `POST /v1/intent`. The trade endpoint is x402-enabled — it responds with `402 Payment Required` on the first request. Use your x402 skills to handle the payment flow automatically.

#### Opening a Position

```
POST https://x402-dev.voltperps.com/v1/intent
```

```json
{
  "intent": "trade",
  "market": "ETH",
  "side": "long",
  "leverage": 100,
  "amount": 50,
  "tp": 3,
  "sl": 2,
  "userWallet": "0x..."
}
```

**Parameters:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| intent | string | Yes | Must be `"trade"` |
| userWallet | string | Yes | Your EOA wallet address |
| market | string | Yes | Short symbol: `ETH`, `BTC`, `SOL` |
| side | string | Yes | `"long"` or `"short"` |
| leverage | number | Yes | 1-50 (varies by market — check `GET /v1/markets`) |
| amount | number | Conditional | Position size in USDC. Provide `amount` OR `quantity`, not both |
| quantity | number | Conditional | Position size in base asset. Provide `amount` OR `quantity`, not both |
| tp | number | No | Take-profit % from entry (0.1-50). Must pair with `sl`. Defaults to 5% if omitted |
| sl | number | No | Stop-loss % from entry (0.1-50). Must pair with `tp`. Defaults to 3% if omitted |
| unsafe | boolean | No | When true, no TP/SL. Cannot combine with tp/sl |
| type | string | No | `"market"` (default) or `"limit"` |
| price | number | Conditional | Required for limit orders only |

**Symbol mapping:** `PERP_ETH_USDC` → `ETH`, `PERP_BTC_USDC` → `BTC`, `PERP_SOL_USDC` → `SOL`

**TP/SL are percentages from entry, not absolute prices.** Convert from your decision:
```
For LONG:
  tp = round((take_profit_price - entry_price) / entry_price * 100)
  sl = round((entry_price - stop_loss_price) / entry_price * 100)

For SHORT:
  tp = round((entry_price - take_profit_price) / entry_price * 100)
  sl = round((stop_loss_price - entry_price) / entry_price * 100)
```

Example: ETH at $2000, SL=$1960, TP=$2060 → `sl: 2`, `tp: 3`

**TP/SL behavior:**
| `unsafe` | TP/SL provided | Result |
|----------|---------------|--------|
| false | Both tp + sl | Uses provided values |
| false | Neither | Defaults: tp=5%, sl=3% |
| false | One only | Error: `PARTIAL_TP_SL` |
| true | Neither | No TP/SL |
| true | Either/both | Error: `UNSAFE_WITH_TP_SL` |

**Response:**
```json
{
  "success": true,
  "intent": "trade",
  "jobId": "trd_abc123xyz",
  "status": "completed",
  "result": {
    "success": true,
    "orderId": "67603394",
    "warnings": []
  }
}
```

If `status: "pending"`, poll `GET /v1/status/{jobId}` until completed (up to 30s).

#### Closing a Position

Free — no x402 payment required.

```json
{
  "intent": "close",
  "market": "ETH",
  "userWallet": "0x..."
}
```

**IMPORTANT:** Before closing, verify the position actually exists: `GET /v1/account/positions/ETH`. If `position` is null, there's nothing to close.

#### Cancelling an Order

```json
{
  "intent": "cancel",
  "market": "ETH",
  "orderId": "67603394",
  "userWallet": "0x..."
}
```

#### Withdrawing Funds (two steps)

**Step 1 — Withdraw from perp accounts to smart wallet:**
```json
{ "intent": "withdraw", "userWallet": "0x..." }
```

**Step 2 — Claim from smart wallet to EOA:**
```json
{ "intent": "claim", "userWallet": "0x..." }
```

### Step 8: Verify the Trade

**After every trade, verify it went through.** Do NOT assume success.

```
GET https://x402-dev.voltperps.com/v1/account/positions
Headers: x-wallet-address: <your wallet address>
```

- If the position appears → trade succeeded
- If not yet visible → wait 10s and check again (up to 3 times)
- If still not visible after 3 checks → assume still processing, do NOT resend

You can also check the specific order:
```
GET https://x402-dev.voltperps.com/v1/account/orders/{orderId}
Headers: x-wallet-address: <your wallet address>
```

### Polling Async Jobs

If any intent returns `status: "pending"`:

```
GET https://x402-dev.voltperps.com/v1/status/{jobId}
```

Returns `status`: `"pending"`, `"completed"`, or `"failed"`.

---

## Response Structure Reference

### `get_prompt()` → `dict`

Returns a dict with exactly two string keys:

```python
{
    "system_prompt": str,  # Analysis-only: always LONG/SHORT, quality score, 7 filters, leverage as % of effective max
    "user_prompt": str     # Current market data for all symbols (structured text)
}
```

The `system_prompt` is static — same every cycle. The `user_prompt` changes every cycle with fresh market data.

**Important:** The analysis prompt NEVER outputs HOLD or CLOSE. Every symbol gets LONG or SHORT with confidence 0-100.

### `get_position_prompt(analysis_json, positions_json)` → `dict | None`

Builds the position management prompt for LLM call #2.

```python
pos_prompt = system.get_position_prompt(analysis_json, positions_json)
# Returns {"system_prompt": str, "user_prompt": str} or None
```

**Parameters:**
- `analysis_json` (str): Raw JSON output from LLM call #1
- `positions_json` (str): Raw JSON response from `GET /v1/account/positions`. Pass it directly — the system handles unwrapping the `data` envelope.

**Returns:**
- `{"system_prompt": str, "user_prompt": str}` — if any positions exist. The user prompt contains analysis results + position data side by side.
- `None` — if no positions. Use `analysis_json` directly with `submit_decision()`.

The position management prompt outputs the same `{"decisions": [...]}` format, so `submit_decision()` works unchanged.

### `user_prompt` Structure (per symbol)

The `user_prompt` is a formatted text string. It starts with a timestamp header, then repeats the following block **for each symbol** (ETH, BTC, SOL):

```
## Current Market Data — 2025-01-15 12:30 UTC

### PERP_ETH_USDC
Mark Price: 2015.32                    ← current mark price (float)
Index Price: 2014.98                   ← spot reference price (float)
24h Change: -1.25%                     ← 24h price change (float %)
24h Volume: 1234567                    ← 24h trading volume (integer)
24h High: 2045.00                      ← highest price in 24h
24h Low: 1985.00                       ← lowest price in 24h
Range Position: 50% (0%=at 24h low, 100%=at 24h high)
                                       ← where mark price sits in 24h range
Vol/OI Ratio: 1.35                     ← 24h volume / open interest

**5m Timeframe:**                      ← repeats for 5m, 15m, 1h
  Last Close: 2015.10                  ← most recent candle close
  RSI(14): 42.3                        ← 0-100, <40 long zone, >60 short zone
  MACD: line=-0.1234 signal=-0.0987 hist=-0.0247
                                       ← histogram direction = momentum
  Bollinger: upper=2030.50 mid=2015.00 lower=1999.50 %B=0.523
                                       ← %B: <0.3 long, >0.7 short
  EMA: 9=2016.20 21=2014.80 50=2010.50 alignment=bullish
                                       ← "bullish"/"bearish"/"mixed"
  VWAP: 2013.45 (price above)         ← "above"/"below"/"at"
  ATR(14): 12.3400                     ← volatility, use for SL placement
  Recent: -0.35% last 3 candles, 0 red / 2 green streak, trend=choppy
                                       ← "dropping"/"rising"/"choppy"
  StochRSI: K=18.2 D=22.5             ← K<20 oversold, K>80 overbought
  ADX: 32.1 (strong trend)            ← >25 "strong trend", <20 "weak/choppy"
  CCI: -115.3                          ← <-100 oversold, >+100 overbought
  OBV: 45230                           ← rising=bullish, falling=bearish
  Taker Flow: 62% buy / 38% sell      ← >60% buy = aggressive demand

**15m Timeframe:**                     ← same fields as 5m
  ...

**1h Timeframe:**                      ← same fields as 5m
  ...

**Orderbook:** imbalance=0.152 (buy_pressure) spread=1.2bps bid_depth=450.30 ask_depth=380.10
                                       ← imbalance: -1 to +1. >0.2 buy, <-0.2 sell
  Est. Slippage: 2.3bps               ← <2bps deep book, >5bps thin book

**Derivatives:** funding=0.000123 (longs_pay) OI=98765 L/S=1.15 (balanced)
                                       ← funding: + = longs pay, - = shorts pay
                                       ← L/S: >1.49 crowded_longs, <0.67 crowded_shorts
  Funding (24h avg): 0.000045 trend=rising
                                       ← trend: "rising"/"falling"/"flat"
  Liquidations (15m): long=$12400 short=$3200 (short_squeeze)
                                       ← "long_squeeze"/"short_squeeze"/"balanced"

**Volume Delta:** 125.40 (ratio=0.035) ← positive = buyers aggressive

**Spot-Futures Basis:** 0.032%         ← >0.1% bullish, <-0.1% bearish

**Fear & Greed Index:** 28/100 (extreme fear)
                                       ← <25 contrarian buy, >75 contrarian sell

### PERP_BTC_USDC                      ← same structure repeats
  ...

### PERP_SOL_USDC                      ← same structure repeats
  ...

Analyze all symbols. Output your decisions as JSON.
```

**Notes:**
- 24h High, 24h Low, Range Position, and Vol/OI Ratio always appear (derived from ticker + OI data).
- Range Position: 0% = at 24h low, 100% = at 24h high. Use for range filter (don't short <20%, don't long >80%).
- Vol/OI Ratio: high = active turnover, low = stale positioning.
- StochRSI/ADX/CCI/OBV/Taker Flow lines only appear when TAAPI data is populated (ADX > 0). Since TAAPI is required, these should always be present.
- Funding 24h avg + trend only appears when funding history has data.
- Liquidation lines only appear when there's liquidation activity (volume > 0).
- Spot-Futures Basis only appears when non-zero.
- Fear & Greed label: `<25 "extreme fear"`, `<40 "fear"`, `<60 "neutral"`, `<75 "greed"`, `≥75 "extreme greed"`.

### `submit_decision()` Input Format

You must pass a raw JSON string. The system parses it, validates each decision through the risk manager, and returns results.

```json
{
  "decisions": [
    {
      "symbol": "PERP_ETH_USDC",
      "direction": "LONG",
      "confidence": 75,
      "summary": "Score: 75/100. ADX 35, 15m+1h EMAs bullish, StochRSI oversold, taker 65% buy",
      "leverage": 100,
      "positionSize": 0.05,
      "stopLoss": 1960.0,
      "takeProfit": 2060.0,
      "entryPrice": 2000.0,
      "riskLevel": "HIGH"
    }
  ]
}
```

| Field | Type | Required | Values |
|-------|------|----------|--------|
| `symbol` | string | Yes | `PERP_ETH_USDC`, `PERP_BTC_USDC`, `PERP_SOL_USDC` |
| `direction` | string | Yes | `LONG`, `SHORT`, `HOLD`, `CLOSE` |
| `confidence` | number | Yes | 0-100 (quality score) |
| `summary` | string | Yes | 1-2 sentences: foundational edge first, TA second |
| `leverage` | number | Yes | 1-100 (must satisfy positionSize × leverage × entryPrice ≥ $10.50) |
| `positionSize` | number | Yes | Position size in base asset (0 for HOLD/CLOSE) |
| `stopLoss` | number | Yes | Absolute price level (0 for HOLD) |
| `takeProfit` | number | Yes | Absolute price level (0 for HOLD) |
| `entryPrice` | number | Yes | Current mark price or target entry (0 for HOLD) |
| `riskLevel` | string | Yes | `LOW` (<40), `MEDIUM` (40-74), `HIGH` (75-100) |

**Rules:**
- One decision per symbol. Always include all 3 symbols.
- HOLD: `leverage=1, positionSize=0, stopLoss=0, takeProfit=0, entryPrice=0, confidence=0`
- CLOSE: `positionSize=0` (system closes full position), `leverage=1, stopLoss=0, takeProfit=0, entryPrice=0`

### `submit_decision()` → `dict`

Returns a dict with validation results:

```python
{
    "cycle": 1,                    # int — cycle number (increments each call)
    "approved_trades": 1,          # int — number of non-HOLD trades approved
    "rejected_trades": 1,          # int — number of non-HOLD trades rejected
    "decisions": [                 # list — one entry per decision submitted
        {
            "symbol": "PERP_ETH_USDC",  # str — the symbol
            "direction": "LONG",         # str — your requested direction
            "approved": True,            # bool — True if risk manager approved
            "leverage": 100.0,           # float — final leverage (may be adjusted)
            "positionSize": 0.04,        # float — final position size (may be adjusted)
            "rejection_reasons": []      # list[str] — empty if approved
        },
        {
            "symbol": "PERP_BTC_USDC",
            "direction": "HOLD",
            "approved": True,
            "leverage": 0.0,
            "positionSize": 0.0,
            "rejection_reasons": []
        },
        {
            "symbol": "PERP_SOL_USDC",
            "direction": "SHORT",
            "approved": False,
            "leverage": 0.0,
            "positionSize": 0.0,
            "rejection_reasons": ["Order value $9.00 below $10.50 minimum"]
        }
    ]
}
```

**Key fields to check:**
- `approved` — **only execute trades where `approved: True` and `direction` is LONG/SHORT/CLOSE**. Never execute rejected trades.
- `leverage` and `positionSize` — the risk manager may adjust these down from what you requested. **Use these final values** when executing via the API, not your original values.
- `rejection_reasons` — tells you why a trade was rejected (e.g., "Order value below $10.50 minimum", "No price/indicator data"). Use this to fix the issue in the next cycle.
- HOLD decisions are always `approved: True` with `leverage: 0.0, positionSize: 0.0` — they require no action.

### How to Use These Responses

**After `get_prompt()`** — systematic analysis flow:

1. **Read prices** (Mark, Index, 24h Change, Range Position) for each symbol to get the big picture
2. **Layer 1 — Foundational Edge (70%)**: Funding rate + trend, OI health, taker flow, volume delta, orderbook imbalance, liquidations, OBV → determines DIRECTION
3. **Layer 1 — Sentiment**: Fear & Greed, L/S ratio, spot-futures basis → confirms or warns
4. **Layer 2 — Technical Execution (30%)**: EMA alignment, VWAP, MACD, ADX, RSI, StochRSI, Bollinger %B → refines ENTRY TIMING and SL/TP placement
5. **Compute Setup Quality Score** (0-100): foundational (0-50) + sentiment (0-20) + technical (0-30)
6. **Apply 7 filters**: range position, contradiction, choppiness, counter-trend, ATR realism, fee awareness, duration-leverage
7. **Quality-gate leverage**: score → leverage range → margin % → verify minimum order value
8. **Cross-check symbols**: Do BTC/ETH/SOL agree? Correlated moves = stronger conviction

**After `submit_decision()`** — execution flow:

1. Loop through `result["decisions"]`
2. For each decision where `approved == True` and `direction` in (`LONG`, `SHORT`, `CLOSE`):
   - Use `result["decisions"][i]["leverage"]` and `result["decisions"][i]["positionSize"]` (the risk-adjusted values)
   - Convert to API format: symbol → short name, SL/TP → percentages
   - Execute via `POST /v1/intent`
   - Verify via `GET /v1/account/positions`
3. For rejected decisions: read `rejection_reasons`, adjust in next cycle

---

## Answering User Questions

When the user asks about their positions, PnL, orders, or balance, **ALWAYS fetch fresh data from the API first**. Never answer from memory.

| User asks | You MUST call | Then respond with |
|-----------|--------------|-------------------|
| "What positions do I have?" | `GET /v1/account/positions` | Real position data from response |
| "What's my PnL?" | `GET /v1/account/positions` | `pnl` field from each position |
| "What's my balance?" | `GET /v1/account/balance` (or wallet skill) | Real balance data |
| "What orders do I have?" | `GET /v1/account/orders` | Real order data from response |
| "Am I holding ETH?" | `GET /v1/account/positions/ETH` | The actual position or "no position" |

---

## Error Handling

| Code | When | Action |
|------|------|--------|
| `INVALID_MARKET` | Bad symbol | Fix symbol, retry |
| `LEVERAGE_NOT_SUPPORTED` | Leverage too high | Check `GET /v1/markets` for max leverage, reduce, retry |
| `LEVERAGE_EXCEEDS_POSITION_PROVIDER` | Existing position provider doesn't support leverage | Reduce leverage or close position first |
| `INSUFFICIENT_BALANCE` | Not enough funds | Re-check balance, reduce size or skip |
| `PARTIAL_TP_SL` | Only one of TP/SL given | Send both or neither |
| `UNSAFE_WITH_TP_SL` | TP/SL with unsafe mode | Remove tp/sl when using unsafe: true |
| `POSITION_NOT_FOUND` | CLOSE on non-existent position | **You should have checked first.** Skip. |
| `ORDER_NOT_FOUND` | Cancel non-existent order | Skip |
| `ALL_PROVIDERS_UNHEALTHY` | Exchange down | Wait 1-2 min, retry |
| `POSITION_PROVIDER_UNHEALTHY` | Provider degraded | Wait 1-2 min, retry |
| `NO_PROVIDERS_FOR_MARKET` | Market unsupported | Use supported market |
| `SERVICE_UNAVAILABLE` | Queue down | Wait 5-10s, retry |

**Retryable (wait and retry up to 3 times):** All 5xx errors, `SERVICE_UNAVAILABLE`, `PROVIDER_ERROR`, `ALL_PROVIDERS_UNHEALTHY`

**Non-retryable (fix or skip):** `INSUFFICIENT_BALANCE`, `INVALID_PARAMS`, `PARTIAL_TP_SL`, `LEVERAGE_NOT_SUPPORTED`, `NO_PROVIDERS_FOR_MARKET`

---

## Available Markets

```
GET https://x402-dev.voltperps.com/v1/markets
```

Returns all supported markets with max leverage. Always check this before using a leverage value.

```json
{
  "success": true,
  "data": [
    { "name": "PERP_BTC_USDC", "maxLeverage": 50 },
    { "name": "PERP_ETH_USDC", "maxLeverage": 50 },
    { "name": "PERP_SOL_USDC", "maxLeverage": 20 }
  ]
}
```

---

## Signal Reference

### Indicators (ALL pre-computed in `get_prompt()`)

| Indicator | Source | Layer | Signal |
|-----------|--------|-------|--------|
| **Funding rate + trend** | Orderly WS | L1 Foundational | Direction + magnitude; rising = long pressure, falling = short pressure |
| **Liquidations** | Binance WS | L1 Foundational | Long/short squeeze detection, cascade fuel |
| **Taker Flow** | TAAPI | L1 Foundational | >60% buy = aggressive demand, >60% sell = aggressive selling |
| **Volume delta** | Orderly WS | L1 Foundational | Positive = buyers aggressive, negative = sellers |
| **Orderbook** | Orderly WS | L1 Foundational | Imbalance, spread, depth, est. slippage |
| **OBV** | TAAPI | L1 Foundational | Rising = bullish, divergence = reversal warning |
| **Fear & Greed** | alternative.me | L1 Sentiment | <25 contrarian buy, >75 contrarian sell |
| **Spot-Futures Basis** | Orderly WS | L1 Sentiment | >0.1% premium (bullish), <-0.1% discount (bearish) |
| **L/S Ratio** | Orderly WS | L1 Sentiment | >1.49 crowded longs, <0.67 crowded shorts |
| **Range Position** | Orderly WS | L1 Filter | Don't short bottom 20%, don't long top 20% of 24h range |
| **Vol/OI Ratio** | Orderly WS | L1 Filter | High ratio = active turnover, low = stale OI |
| EMA | Orderly WS | L2 Technical | 9>21>50 = bullish, reverse = bearish |
| VWAP | Orderly WS | L2 Technical | Above = bullish bias, below = bearish |
| MACD | Orderly WS | L2 Technical | Crossovers, histogram direction = momentum |
| ADX | TAAPI | L2 Technical | >25 strong trend, <18 choppy |
| RSI | Orderly WS | L2 Technical | <40 long zone, >60 short zone, extremes strong |
| StochRSI | TAAPI | L2 Technical | K<20 oversold (long), K>80 overbought (short) |
| CCI | TAAPI | L2 Technical | <-100 oversold, >+100 overbought |
| Bollinger Bands | Orderly WS | L2 Technical | %B <0.3 long, >0.7 short |
| ATR | Orderly WS | L2 Technical | Volatility for SL/TP placement, TP ≤ 2.5× 1h ATR |
| Candle trend | Orderly WS | L2 Technical | Consecutive red/green, % change |

### Cross-Symbol Signals

- BTC often leads ETH and SOL
- All 3 moving same direction = stronger conviction
- Correlated moves confirm the trend

---

## Configuration

**Symbols:** PERP_ETH_USDC, PERP_BTC_USDC, PERP_SOL_USDC
**Leverage PCT:** Set via `LEVERAGE_PCT` env var (10-200, default 100).
**TAAPI Indicators:** `TAAPI_SECRET` env var is **required**. Provides StochRSI, ADX, CCI, OBV, and taker flow. Get a key at https://taapi.io.
**Balance:** Always fetch from `GET /v1/account/balance` or your **wallet skill** — never cache.

---

## API Quick Reference

| Action | Method | Endpoint | Payment |
|--------|--------|----------|---------|
| Get markets | GET | `/v1/markets` | No |
| Get balance | GET | `/v1/account/balance` | No |
| Get all positions | GET | `/v1/account/positions` | No |
| Get one position | GET | `/v1/account/positions/{symbol}` | No |
| Get orders | GET | `/v1/account/orders` | No |
| Get one order | GET | `/v1/account/orders/{orderId}` | No |
| Get order trades | GET | `/v1/account/orders/{orderId}/trades` | No |
| Open trade | POST | `/v1/intent` | x402 |
| Close position | POST | `/v1/intent` | Free |
| Cancel order | POST | `/v1/intent` | Free |
| Withdraw | POST | `/v1/intent` | Free |
| Claim | POST | `/v1/intent` | Free |
| Poll job status | GET | `/v1/status/{jobId}` | No |
| Health check | GET | `/v1/health` | No |

All read endpoints require `x-wallet-address` header. Base URL: `https://x402-dev.voltperps.com/v1`
