# Trading Strategy Design Document

## Overview

This system connects to Orderly Network's perpetual futures market via WebSocket APIs, computes technical indicators in code, and sends a structured "market snapshot" to an LLM (Grok, Claude, GPT, or any local model) which outputs a trading decision as structured JSON. A risk manager in code validates every decision before execution.

```
[Orderly WebSocket] → [DataCollector] → [Indicators Engine] → [LLM Prompt] → [LLM Decision] → [Risk Manager] → [Execute or Reject]
```

---

## The Strategy: Multi-Timeframe Confluence Swing Trading

### Core Idea

The LLM acts as a **discretionary trader** that receives the same data a human swing trader would look at on their screen — but structured and pre-computed. The LLM's job is to find **confluence**: multiple independent signals agreeing on a direction.

The LLM is an **active swing trader**, not a passive observer. Its job is to find trades, not reasons to avoid them. A trade is taken when **2+ signal categories** agree on a direction:

### Signal Categories

#### 1. Trend (from 1h and 15m timeframes)
- **EMA alignment**: 9 > 21 > 50 = bullish, 50 > 21 > 9 = bearish
- **Price vs VWAP**: Above VWAP = bullish bias, below = bearish
- **MACD**: Bullish/bearish crossovers and histogram direction

#### 2. Momentum (from 5m and 15m timeframes)
- **RSI**: <40 favors long, >60 favors short. Extremes (<30, >70) are strong signals.
- **Bollinger %B**: <0.3 = long zone, >0.7 = short zone
- **MACD histogram**: Building = momentum, fading = weakening
- **Recent candle trend**: 3+ consecutive red candles = actively dropping (bearish), 3+ green = actively rising (bullish)
- **Recent % change**: Actual price movement over last 3 candles — detects sharp moves that lagging indicators miss

#### 3. Market Microstructure
- **Orderbook imbalance**: Bid-heavy (positive) = buy pressure, ask-heavy (negative) = sell pressure
- **Spread**: Tight spread = liquid/confident market, wide = uncertainty
- **Volume delta**: Positive = buyers aggressive, negative = sellers aggressive

#### 4. Derivatives Sentiment
- **Funding rate**: Positive (longs pay) = crowded longs. Negative = crowded shorts.
- **Open interest**: Rising OI + rising price = new money entering (trend strength). Falling OI + rising price = short covering (weak rally).
- **Long/short ratio**: Extreme ratios (>1.5 or <0.67) = contrarian signal.

### Decision Matrix

| Categories Agreeing | Signal Strength | Action | Confidence |
|---------------------|-----------------|--------|------------|
| 2 categories | Moderate signals | Trade | 0.4-0.6 |
| 3 categories | Clear alignment | Trade | 0.6-0.8 |
| Strong trend + momentum | Even without microstructure | Trade | 0.5-0.7 |
| All 3 symbols same direction | Cross-asset confirmation | Higher conviction | +0.1 |
| Genuinely conflicting/flat | No agreement | HOLD | 0.0 |

### Entry Rules
1. Price should be near a technical level (EMA, Bollinger band, VWAP)
2. At least 2 signal categories must agree on direction
3. Risk/reward ratio must be >= 1.5:1 (ideally 2:1+)
4. Stop-loss 1-2 ATR from entry at a logical technical level
5. Position sized so max loss = ~2% of available budget

### Exit Rules — "Let Winners Run"

**The default for open positions is HOLD.** The SL and TP levels are the trade plan.

**Code-enforced exits** (automatic, no LLM involved):
1. **Stop loss**: Hard SL always set. Code checks every cycle and closes if hit.
2. **Take profit**: Hard TP always set. Code checks every cycle and closes if hit.

**LLM-triggered exits** (CLOSE action — used sparingly):
Only when the **original trade thesis is broken**:
- 2+ signal categories have flipped against the position direction
- EMA alignment has reversed (was bullish, now bearish)
- Clear reversal pattern confirmed by trend indicators

**Do NOT close because:**
- Unrealized PnL is slightly negative — that's what the stop-loss is for
- Uncertain — uncertainty means HOLD, not CLOSE
- Only 1 category weakened while others still support
- Position just opened and hasn't had time to work

**Position context provided to LLM:**
- Distance to SL and TP as percentages
- Progress toward TP (0% = at entry, 100% = at TP)
- How long the position has been held
- This helps the LLM make informed hold/close decisions rather than reacting to raw uPnL

---

## Risk Management — "Never Go to Zero"

### The Three Safety Nets

**Safety Net 1: Graduated Reserve System**

Instead of locking away a flat %, the budget is split into zones with escalating access requirements:

```
|████████████████████░░░░░░░░░░░░░|
 Free Zone (70%)  Guarded (20%)  Floor (10%)
 Normal rules     Conditional     Earned access only
```

**Free Zone (70% of budget)** — Always available. Normal trading rules apply.

**Guarded Reserve (next 20%)** — Accessible only when the strategy has proven itself:
- Win rate > 45% over last 20 trades
- NOT in a losing streak (last 3 trades can't all be losses)
- LLM confidence > 0.75
- Leverage capped at 3x (regardless of confidence scaling)
- R:R must be >= 2:1

**Hard Floor (last 10%)** — Accessible only under exceptional conditions:
- Win rate > 60% over last 30+ trades
- LLM confidence > 0.9
- R:R must be > 3:1
- Even then, only 50% of this floor can be used (5% of budget is truly untouchable)

Example with $1000 budget:

| Zone | Amount | Access Conditions |
|------|--------|-------------------|
| Free | $700 | Always |
| Guarded | $200 | Proven win rate, no losing streak, high confidence |
| Hard floor | $50 (of $100) | Exceptional setup only |
| True lockout | $50 | Never. Survives everything. |

**Why graduated?** A flat reserve wastes capital when the strategy is working. The guarded zone unlocks when you've *earned* it through demonstrated performance, and locks back down when you're losing — preventing the worst-case scenario of compounding losses with reserve capital.

**Safety Net 2: Per-Trade Loss Cap (2%)**
- Maximum loss on any single trade = 2% of available budget (not total budget)
- Position size is calculated backwards from stop-loss distance:
  - If SL is 2% away from entry, position size = budget_risk / (price * 0.02)
  - If SL is 5% away, position size is proportionally smaller
- This means: after 10 consecutive max-loss trades, you still have 81.7% of available budget

**Safety Net 3: Drawdown Circuit Breaker**
- At 10% drawdown from peak: position sizes automatically cut by half
- At 20% drawdown from peak: ALL trading halts until manual restart
- This prevents the "tilt" scenario where losses beget larger losses

### Leverage Scaling by Confidence

The LLM outputs a confidence score (0.0 to 1.0). The code maps this to max allowed leverage:

| Confidence | Max Leverage | Rationale |
|------------|-------------|-----------|
| 0.0 - 0.3 | 1x | Basically spot. Very uncertain. |
| 0.3 - 0.5 | 2x | Low conviction. Small exposure. |
| 0.5 - 0.7 | 5x | Moderate conviction. Standard swing trade. |
| 0.7 - 0.85 | 70% of max | High conviction. Strong confluence. |
| 0.85 - 1.0 | Full max (10x) | Very high conviction. All signals aligned. |

If the LLM says "LONG 10x" but confidence is 0.4, the code caps it at 2x. The LLM cannot override this.

### Position Sizing Example

Budget: $1000, Reserve: $200, Available: $800

LLM decision: LONG ETH at $3000, SL at $2940 (2% below), confidence 0.6

1. Max leverage at 0.6 confidence = 5x
2. SL distance = $60 (2%)
3. Max loss = 2% of $800 = $16
4. Max position size = $16 / $60 = 0.267 ETH
5. Notional = 0.267 * $3000 = $800
6. Margin required = $800 / 5x = $160
7. Budget after: $800 - $160 = $640 available

If ETH hits SL: lose $16 (2% of available). Budget becomes $784.
If ETH hits TP (say $3120, 4% up): gain $32 (R:R = 2:1). Budget becomes $832.

---

## Data Pipeline Detail

### What We Subscribe To

| Feed | Topic Format | Update Speed | Used For |
|------|-------------|-------------|----------|
| K-line 5m | `{symbol}@kline_5m` | 1s | Short-term momentum indicators |
| K-line 15m | `{symbol}@kline_15m` | 1s | Primary swing trade signals |
| K-line 1h | `{symbol}@kline_1h` | 1s | Trend direction and major levels |
| Orderbook | `{symbol}@orderbook` | 1s | Depth, imbalance, support/resistance walls |
| BBO | `{symbol}@bbo` | 10ms | Best bid/ask, spread |
| Trades | `{symbol}@trade` | Real-time | Buy/sell volume delta |
| 24h Ticker | `{symbol}@ticker` | 1s | Daily OHLCV context |
| Funding Rate | `{symbol}@estfundingrate` | 15s | Derivatives sentiment |
| Open Interest | `{symbol}@openinterest` | 1s-10s | Position accumulation/unwinding |
| Traders OI | `traders_open_interests` | 1min | Long/short ratio |
| Mark Price | `{symbol}@markprice` | 1s | Fair value, SL/TP monitoring |
| Index Price | `SPOT_{base}_USDC@indexprice` | 1s | Spot reference (mark vs index divergence) |
| Liquidations | `liquidation` | Real-time | Cascade risk detection |

### Indicators Computed in Code

| Indicator | Parameters | Signal |
|-----------|-----------|--------|
| RSI | 14-period | Oversold/overbought |
| MACD | 12, 26, 9 | Trend + momentum crossovers |
| Bollinger Bands | 20-period, 2 std | Volatility + mean reversion levels |
| EMA | 9, 21, 50 | Trend direction and dynamic support/resistance |
| VWAP | Session | Fair value, institutional bias |
| ATR | 14-period | Volatility for SL placement |
| Recent price action | Last 3 candles | % change, consecutive red/green streak, candle trend |
| Orderbook imbalance | Top 5/10 levels | Short-term directional pressure |
| Volume delta | Recent trades | Buy vs sell aggression |

---

## LLM Interaction

### Provider: OpenRouter (single adapter for all models)

One API, every model. Switch by changing the `model` field in config:

| Model | OpenRouter ID | Use Case |
|-------|--------------|----------|
| Grok 3 Mini (default) | `x-ai/grok-3-mini` | Primary. Returns readable reasoning chain. |
| Claude Sonnet | `anthropic/claude-sonnet-4-20250514` | Fallback / comparison |
| GPT-4o | `openai/gpt-4o` | Fallback / comparison |
| DeepSeek V3 | `deepseek/deepseek-chat` | Cheap alternative |

Default model: **`x-ai/grok-3-mini`** with `reasoning_effort: "high"`
- Returns full `reasoning_content` — we store it alongside every trade decision for audit
- Cheaper than grok-4 but with visible thought process
- Can upgrade to grok-4 if decision quality needs improvement

### Multi-Symbol Support

The system watches **3 symbols simultaneously**: PERP_ETH_USDC, PERP_BTC_USDC, PERP_SOL_USDC

Each symbol has its own:
- DataCollector (independent WS subscriptions)
- KlineBuffers (per timeframe per symbol)
- Indicator computation

Every 5 minutes, the LLM receives a snapshot of ALL 3 symbols and decides:
- Which symbol(s) to trade (can be multiple)
- Direction, leverage, quantity for each
- Can also factor cross-symbol correlations (e.g., BTC leading ETH)

### What the LLM Receives (every 5 minutes)

A structured text block containing:
1. All computed indicators for ETH, BTC, and SOL across 3 timeframes (5m, 15m, 1h)
2. Recent price action per timeframe (% change over last 3 candles, consecutive red/green streak, candle trend direction)
3. Orderbook analysis per symbol (imbalance, spread, depth)
4. Funding rate analysis per symbol (direction, magnitude)
5. Open interest analysis per symbol (OI level, long/short ratio, sentiment)
6. Volume delta from recent trades per symbol
7. Current portfolio state (budget, open positions across all symbols, PnL, win rate, drawdown, recent trade history)
8. Open position context: distance to SL/TP as %, progress toward TP, hold time
9. Risk constraints it must respect

### What the LLM Outputs

```json
{
  "decisions": [
    {
      "symbol": "PERP_ETH_USDC",
      "action": "LONG",
      "leverage": 5,
      "quantity": 0.267,
      "stop_loss": 2940.0,
      "take_profit": 3120.0,
      "confidence": 0.72,
      "reasoning": "15m and 1h EMAs bullish aligned..."
    },
    {
      "symbol": "PERP_BTC_USDC",
      "action": "HOLD",
      "leverage": 1,
      "quantity": 0,
      "stop_loss": 0,
      "take_profit": 0,
      "confidence": 0,
      "reasoning": "BTC consolidating, no clear signal."
    },
    {
      "symbol": "PERP_SOL_USDC",
      "action": "SHORT",
      "leverage": 3,
      "quantity": 12.5,
      "stop_loss": 155.0,
      "take_profit": 140.0,
      "confidence": 0.65,
      "reasoning": "SOL overbought on 15m RSI, funding extreme..."
    }
  ]
}
```

Each decision is independently validated by the risk manager. Total exposure across all symbols is checked against the budget.

### What the Risk Manager Does With It

9-layer validation for each decision:
1. **Drawdown circuit breaker** — halt at 20%, reduce at 10%
2. **Confidence validation** — rejects below 0.1
3. **Leverage cap by confidence** — scales max leverage to confidence level
4. **Budget zone access** — graduated reserve check (free/guarded/floor)
5. **Stop-loss validation** — must exist, correct direction, 0.5-3.0x ATR range
6. **Risk/reward ratio** — minimum 1.5:1 (higher for guarded zone access)
7. **Position sizing** — 2% max loss rule, adjusted for drawdown
8. **Total exposure check** — cumulative margin across all symbols capped at 80%
9. **Position conflict** — rejects duplicate same-direction or opposite-direction on same symbol
Final: APPROVED (possibly with adjusted qty/leverage) or REJECTED with reasons

### Reasoning Storage

Every analysis cycle stores:
```
{
  timestamp: ...,
  grok_reasoning_content: "...",    // Full Grok thinking process
  grok_final_output: {...},         // The JSON decisions
  risk_manager_adjustments: {...},  // What was modified/rejected
  portfolio_state_before: {...},    // Snapshot before execution
  portfolio_state_after: {...},     // Snapshot after execution
}
```
This creates a full audit trail for reviewing decision quality.

---

## Trade Execution — x402 VoltPerps API

Trade execution goes through the **VoltPerps x402-enabled API**. This is a paid API where write operations (opening trades) require x402 micropayments on Base (USDC). The agent already has an x402 client skill that handles the payment flow automatically.

**Base URL:** `https://x402-dev.voltperps.com/v1`

### Decision → API Mapping

After the risk manager approves a decision, it must be translated into an x402 API call:

| LLM Decision | x402 Intent | API Call |
|--------------|-------------|----------|
| LONG | `trade` | `POST /v1/intent` with `side: "long"` |
| SHORT | `trade` | `POST /v1/intent` with `side: "short"` |
| CLOSE | `close` | `POST /v1/intent` with `intent: "close"` |
| HOLD | — | No API call |

### Opening a Position (trade intent)

**Endpoint:** `POST /v1/intent` (x402-enabled — requires payment via x402 client)

**Headers:**
```
Content-Type: application/json
x-wallet-address: <user_wallet_address>
X-PAYMENT: <x402_payment_receipt>   ← handled by agent's x402 client skill
```

**Payload format:**
```json
{
  "intent": "trade",
  "market": "ETH",
  "side": "long",
  "leverage": 5,
  "amount": 50,
  "type": "market",
  "tp": 3,
  "sl": 2
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `intent` | string | Yes | Always `"trade"` |
| `market` | string | Yes | Short symbol: `ETH`, `BTC`, `SOL` (NOT `PERP_ETH_USDC`) |
| `side` | string | Yes | `"long"` or `"short"` |
| `leverage` | number | Yes | 1-50 (varies by market) |
| `amount` | number | Conditional | Position size in **USDC** (use `amount` OR `quantity`, not both) |
| `quantity` | number | Conditional | Position size in **base asset** (e.g., ETH units) |
| `type` | string | No | `"market"` (default) or `"limit"` |
| `price` | number | Conditional | Required for limit orders only |
| `tp` | integer | No | Take-profit as **% from entry**. Whole numbers only: `1`, `2`, `3`, `5`, etc. No decimals, no `%` symbol. |
| `sl` | integer | No | Stop-loss as **% from entry**. Whole numbers only: `1`, `2`, `3`, `5`, etc. No decimals, no `%` symbol. |
| `unsafe` | boolean | No | If `true`, no TP/SL applied |

**TP/SL values are whole-number percentages, not absolute prices.** Just the number — no decimals, no `%` symbol.

Examples: `"tp": 3` means take-profit at 3% from entry. `"sl": 2` means stop-loss at 2% from entry.

Conversion from absolute prices (round to nearest whole number):
```
For LONG:
  tp = round((take_profit_price - entry_price) / entry_price * 100)
  sl = round((entry_price - stop_loss_price) / entry_price * 100)

For SHORT:
  tp = round((entry_price - take_profit_price) / entry_price * 100)
  sl = round((stop_loss_price - entry_price) / entry_price * 100)
```

**TP/SL rules:**
- Must provide both `tp` and `sl`, or neither (partial = error `PARTIAL_TP_SL`)
- If neither provided and `unsafe` is false, defaults apply: `tp=5`, `sl=3`
- Cannot combine `unsafe: true` with TP/SL (error `UNSAFE_WITH_TP_SL`)

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

**Cost:** Position size + ~1.2% AnySpend swap fee + 0.1-0.3% provider deposit fee. Example: $50 trade ≈ $50.70 total.

### Closing a Position (close intent)

**Endpoint:** `POST /v1/intent` (free — no x402 payment required)

```json
{
  "intent": "close",
  "market": "ETH"
}
```

Closes the **entire** position for that market. No partial closes.

### Example: Full Decision-to-Execution Flow

LLM outputs:
```json
{
  "symbol": "PERP_ETH_USDC",
  "action": "LONG",
  "leverage": 5,
  "quantity": 0.05,
  "stop_loss": 1960.0,
  "take_profit": 2060.0,
  "confidence": 0.6
}
```

Risk manager approves with adjusted quantity. Current ETH price: $2000.

Convert to x402 API call:
```json
{
  "intent": "trade",
  "market": "ETH",
  "side": "long",
  "leverage": 5,
  "quantity": 0.05,
  "type": "market",
  "tp": 3,
  "sl": 2
}
```
- `tp`: round((2060 - 2000) / 2000 * 100) = **3**
- `sl`: round((2000 - 1960) / 2000 * 100) = **2**

The agent's x402 client skill handles:
1. Sending the initial `POST /v1/intent` request
2. Receiving the `402 Payment Required` response
3. Making the USDC payment on Base
4. Retrying with the `X-PAYMENT` header
5. Polling job status if pending

### Reading Account State

These are free read endpoints (no x402 payment). All require `x-wallet-address` header.

#### GET /v1/account/balance — Budget & Collateral

```
GET /v1/account/balance
Headers: x-wallet-address: 0x...
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

**Important — two different balances:**

| Field | What It Is | Used For |
|-------|-----------|----------|
| `smartWallet` | **Your budget** — USDC in your wallet on Base. This is the money available to open new trades via x402 payments. | Budget tracking, position sizing, risk management |
| `perpAccount.total` | **Trading collateral** — funds already deposited into the perp exchange. This is NOT your budget. | Margin info only |
| `perpAccount.available` | Collateral not locked in positions | Margin info only |
| `perpAccount.frozen` | Collateral locked in pending withdrawals | Margin info only |

The **budget for the strategy** = `smartWallet` balance. When you open a trade via x402, USDC is paid from the wallet, swapped cross-chain, and deposited as collateral automatically. The `perpAccount` balance reflects what's already on the exchange — it's managed by the trading provider, not by you directly.

#### GET /v1/account/positions — Open Positions

```
GET /v1/account/positions
Headers: x-wallet-address: 0x...
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
        "marginRequirements": {
          "initial": 16.00,
          "maintenance": 8.00
        },
        "associatedOrders": [
          {
            "orderId": 67603395,
            "algoType": "TAKE_PROFIT",
            "triggerPrice": 3520.00,
            "status": "INCOMPLETE"
          },
          {
            "orderId": 67603396,
            "algoType": "STOP_LOSS",
            "triggerPrice": 3040.00,
            "status": "INCOMPLETE"
          }
        ]
      }
    ]
  }
}
```

Key fields: `side`, `size`, `entryPrice`, `markPrice`, `pnl` (unrealized), `leverage`, `liquidationPrice`, and associated TP/SL orders.

#### GET /v1/account/positions/{symbol} — Single Position

Use short symbol: `GET /v1/account/positions/ETH`

Returns `"position": null` if no position exists for that market.

#### GET /v1/account/orders — Order History

```
GET /v1/account/orders?status=OPEN&symbol=ETH&page=1&size=20
Headers: x-wallet-address: 0x...
```

Query params: `symbol`, `status` (OPEN/FILLED/CANCELLED), `side` (BUY/SELL), `page`, `size`.

#### GET /v1/markets — Available Markets

```
GET /v1/markets
```

Response:
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

### Error Codes to Handle

| Code | When | Action |
|------|------|--------|
| `INVALID_MARKET` | Bad symbol | Check `/v1/markets` |
| `LEVERAGE_NOT_SUPPORTED` | Leverage too high | Reduce leverage |
| `INSUFFICIENT_BALANCE` | Not enough funds | Reduce position size |
| `PARTIAL_TP_SL` | Only one of TP/SL given | Always send both |
| `POSITION_NOT_FOUND` | CLOSE on non-existent position | Skip |
| `ALL_PROVIDERS_UNHEALTHY` | Exchange down | Retry after delay |

---

## Backtesting

Historical kline data is available via REST API:
- `GET /v1/tv/kline_history` — up to 1000 candles per request, paginated via from/to timestamps
- Supports: 1m, 5m, 15m, 30m, 1h, 4h, 12h, 1d, 1w, 1mon
- Requires Orderly account authentication (orderly-key, orderly-signature headers)

Backtesting module will replay historical data through the same strategy engine to evaluate prompt/model quality before risking real money. Deferred until auth is set up.

---

## Decided Parameters

| Parameter | Value |
|-----------|-------|
| Symbols | PERP_ETH_USDC, PERP_BTC_USDC, PERP_SOL_USDC |
| Analysis interval | 5 minutes |
| Max concurrent positions | Uncapped (risk manager enforces total exposure) |
| LLM provider | OpenRouter (single adapter for all models) |
| Default model | x-ai/grok-3-mini with reasoning_effort: high |
| Reasoning storage | Full reasoning_content stored per cycle |
| Execution | x402 VoltPerps API via agent's x402 client skill |
| Base URL | https://x402-dev.voltperps.com/v1 |
