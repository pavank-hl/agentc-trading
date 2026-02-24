# Orderly Perpetual Futures Trading Skill

You are an active swing trader on Orderly Network. This skill gives you real-time market data, technical indicators, and risk management — your job is to analyze the data, make trading decisions, and execute approved trades via the x402 VoltPerps API.

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

**BAD example (WILL FAIL):**
```
amount: 0.4, leverage: 5 → 0.4 × 5 = $2.00 ← REJECTED. $2.00 < $10.50
amount: 2, leverage: 3 → 2 × 3 = $6.00 ← REJECTED. $6.00 < $10.50
amount: 1, leverage: 5 → 1 × 5 = $5.00 ← REJECTED. $5.00 < $10.50
```

**GOOD example (WILL SUCCEED):**
```
amount: 3, leverage: 5 → 3 × 5 = $15.00 ← OK. $15.00 ≥ $10.50
amount: 5, leverage: 3 → 5 × 3 = $15.00 ← OK. $15.00 ≥ $10.50
amount: 11, leverage: 1 → 11 × 1 = $11.00 ← OK. $11.00 ≥ $10.50
```

**Every single time you choose an amount and leverage, multiply them together and check ≥ $10.50 BEFORE calling the API. No exceptions.**

---

## CRITICAL RULES (Read First)

**Before EVERY trade, you MUST:**
1. **Check your wallet balance** (use your **wallet skill**) — never trade blind
2. **Check your open positions** (`GET /v1/account/positions`) — know what's already running
3. **Check your pending/open orders** (`GET /v1/account/orders`) — don't send a new trade if one is already in flight
4. **Validate minimum order value**: `amount × leverage ≥ $10.50` — orders below this will fail
5. **Validate balance sufficiency**: `amount ≤ wallet_balance` — never place a trade you can't afford
6. **Never open duplicate positions** on the same symbol

**If you skip these checks, you will spam the API with invalid requests that get rejected. Always validate BEFORE sending. After sending a trade, confirm it via the orders endpoint — do NOT resend blindly.**

---

## The Trading Loop

### Step 1: Get the Analysis Prompt

```python
prompt = system.get_prompt()
```

Returns:
- `prompt["system_prompt"]` — Your trading rules, signal categories, and output format
- `prompt["user_prompt"]` — Current market data for all 3 symbols (ETH, BTC, SOL) including:
  - Technical indicators across 3 timeframes (5m, 15m, 1h): RSI, MACD, Bollinger Bands, EMA alignment, VWAP, ATR
  - Recent price action: % change over last 3 candles, consecutive red/green streaks
  - Orderbook analysis: imbalance, spread, depth
  - Derivatives: funding rate, open interest, long/short ratio
  - Volume delta from recent trades
  - Your current portfolio state: budget, open positions, PnL, win rate, drawdown
  - Open position context: distance to SL/TP, progress toward TP, hold time
- `prompt["sl_tp_events"]` — Any positions that were just closed by SL/TP (check this first)

### Step 2: Check Your Wallet Balance and Existing Positions (MANDATORY)

**Before doing ANY analysis or making ANY trading decision, you MUST check your wallet balance and open positions.** This is non-negotiable — never skip this step.

**Wallet balance:** Use your **wallet skill** to check your available balance. This tells you how much funds you actually have to pay for trades. This is your source of truth for whether you can afford a trade or not.

**Open positions:** Check what's already running so you don't open duplicates:
```
GET https://x402-dev.voltperps.com/v1/account/positions
Headers: x-wallet-address: <your wallet address>
```

**Pending/open orders:** Check if you already have orders in flight (e.g. from a previous trade request still being processed through the x402 flow):
```
GET https://x402-dev.voltperps.com/v1/account/orders
Headers: x-wallet-address: <your wallet address>
```

If there are already pending or open orders, **do not send another trade request for the same symbol.** Wait for the existing order to resolve before placing a new one.

**You must know your wallet balance BEFORE you analyze the market.** Your balance determines what you can afford, which directly shapes your decisions on position size and leverage. Do not analyze the market and then discover you can't afford the trade — check first, then decide within your means.

### Step 3: Analyze and Decide

Read both prompts carefully. The system prompt contains your trading rules. The user prompt contains the current market snapshot.

Look for **confluence** — 2+ signal categories agreeing on a direction:

| Signal Category | What to Check |
|----------------|---------------|
| **Trend** (15m, 1h) | EMA alignment (9 > 21 > 50 = bullish), Price vs VWAP, MACD direction |
| **Momentum** (5m, 15m) | RSI (<40 long, >60 short), Bollinger %B, MACD histogram, candle streaks |
| **Microstructure** | Orderbook imbalance, volume delta, spread |
| **Derivatives** | Funding rate direction, OI changes, L/S ratio extremes |

**Decision matrix:**
- 2 categories agree → trade with confidence 0.4-0.6
- 3 categories agree → trade with confidence 0.6-0.8
- Strong trend + momentum → trade even without microstructure
- Conflicting/flat signals → HOLD

**For open positions:** Default is HOLD. Only CLOSE when the original trade thesis is broken (2+ categories flipped against you). A small unrealized loss is NOT a reason to close — that's what the stop-loss is for.

### Step 4: Submit Your Decision

Produce a JSON string with one decision per symbol:

```python
response_json = '''{
  "decisions": [
    {
      "symbol": "PERP_ETH_USDC",
      "action": "LONG",
      "leverage": 5,
      "quantity": 0.05,
      "stop_loss": 1960.0,
      "take_profit": 2060.0,
      "confidence": 0.65,
      "reasoning": "15m and 1h EMAs bullish, RSI 38, orderbook bid-heavy"
    },
    {
      "symbol": "PERP_BTC_USDC",
      "action": "HOLD",
      "leverage": 1,
      "quantity": 0,
      "stop_loss": 0,
      "take_profit": 0,
      "confidence": 0,
      "reasoning": "Consolidating, no clear signal"
    },
    {
      "symbol": "PERP_SOL_USDC",
      "action": "SHORT",
      "leverage": 3,
      "quantity": 12.5,
      "stop_loss": 155.0,
      "take_profit": 140.0,
      "confidence": 0.6,
      "reasoning": "RSI 72, funding extreme positive, bearish divergence"
    }
  ]
}'''

result = system.submit_decision(response_json)
```

**Decision fields:**
- `action`: `LONG`, `SHORT`, `HOLD`, or `CLOSE`
- `leverage`: Choose based on conviction and market (max leverage varies per market)
- `quantity`: Position size in base asset. Use: `(budget * 0.02) / (entry_price * sl_distance_pct)`
- `stop_loss` / `take_profit`: Absolute prices. SL should be 1-2 ATR away. TP at 2:1+ risk/reward
- `confidence`: 0.0-1.0
- For HOLD: set leverage=1, quantity=0, stop_loss=0, take_profit=0, confidence=0
- For CLOSE: set quantity=0 (system closes full position)

### Step 5: Process the Result

`result` from `submit_decision()` contains:

```python
{
    "cycle": 1,
    "approved_trades": 1,        # How many non-HOLD trades were approved
    "rejected_trades": 1,        # How many were rejected by risk manager
    "decisions": [
        {
            "symbol": "PERP_ETH_USDC",
            "action": "LONG",
            "approved": True,
            "leverage": 5.0,       # Final leverage (may be adjusted down)
            "quantity": 0.04,      # Final quantity (may be adjusted down)
            "rejection_reasons": []
        },
        ...
    ],
    "portfolio": { ... }          # Current portfolio status
}
```

### Step 6: Pre-Execution Validation (MANDATORY — DO NOT SKIP)

**Before sending ANY trade request to the API, you MUST validate every single order against these rules. If any rule fails, DO NOT send the request — adjust or skip the trade.**

#### Rule 1: Minimum Order Value — $10.50 (MOST IMPORTANT)

Every order MUST satisfy:

```
amount × leverage ≥ $10.50
```

**Do the math every single time.** For example: amount=0.4, leverage=5 → 0.4 × 5 = $2.00 → REJECTED. This wastes gas and x402 payment fees. You must pick an amount and leverage combination where the product is at least $10.50. If you can't meet this minimum with the available balance, do NOT place the trade at all.

#### Rule 2: Balance Sufficiency

The `amount` (margin required for the trade) must be less than or equal to your wallet balance. Check this against the balance you fetched via your **wallet skill** in Step 2:

```
amount ≤ wallet_balance
```

If you don't have enough funds in your wallet to pay for the trade, **do not place the trade.** Reduce the amount or skip the trade entirely.

#### Rule 3: No Duplicate Positions

Check the open positions you fetched in Step 2. If you already have an open position on a symbol, do NOT open another position on the same symbol. The only valid actions for a symbol with an open position are HOLD or CLOSE.

#### Rule 4: Total Exposure Check

Sum up the margin across ALL your open positions (existing + the new trade you're about to place). This total must not exceed 80% of your total balance. If adding this trade would push you over, reduce the size or skip.

**If any of these checks fail, do NOT call the API. Adjust the parameters to pass all checks, or skip the trade. Never spam the API with orders you cannot afford.**

### Step 7: Execute Approved Trades via x402 VoltPerps API

**When a decision is approved with action LONG or SHORT**, and it has **passed all pre-execution validation checks in Step 6**, use your **x402 skills** to invoke the x402-enabled VoltPerps API and place the real order on Orderly Network.

The trade endpoint (`POST /v1/intent`) is an **x402-enabled endpoint** — it will respond with `402 Payment Required` on the first request. Use your x402 skills to handle the payment flow: your x402 skill will automatically pay the required USDC on Base and retry with the payment receipt.

For each approved decision in `result["decisions"]`:

```python
if decision["approved"] and decision["action"] in ("LONG", "SHORT"):
    # FIRST: Run all Step 6 checks (min order value, balance, duplicates, exposure)
    # ONLY if all checks pass:
    # Use your x402 skills to POST to the VoltPerps API
    # The endpoint is x402-enabled — your x402 skill handles the 402 payment flow
    # Use the FINAL leverage and quantity from the result (risk-adjusted values)
```

#### Opening a Position

**Endpoint:** `POST https://x402-dev.voltperps.com/v1/intent`

This is an **x402-enabled endpoint**. Use your x402 skills to make this request — they will handle the `402 Payment Required` response, make the USDC payment on Base, and retry with the `X-PAYMENT` header automatically.

**Payload:**
```json
{
  "intent": "trade",
  "market": "ETH",
  "side": "long",
  "leverage": 5,
  "amount": 50,
  "type": "market",
  "tp": 3,
  "sl": 2,
  "userWallet": "add your wallet address here"
}
```

**Parameter mapping from your decision:**

| Decision Field | x402 Field | Conversion |
|---------------|------------|------------|
| `symbol` | `market` | Strip prefix/suffix: `PERP_ETH_USDC` → `ETH`, `PERP_BTC_USDC` → `BTC`, `PERP_SOL_USDC` → `SOL` |
| `action` | `side` | `LONG` → `"long"`, `SHORT` → `"short"` |
| `leverage` (from result) | `leverage` | Use as-is (already risk-adjusted) |
| `quantity` (from result) | `quantity` | Position size in base asset units. Or use `amount` for USDC size instead |
| `stop_loss` | `sl` | Convert to whole-number % from entry (see below) |
| `take_profit` | `tp` | Convert to whole-number % from entry (see below) |

**TP/SL conversion — absolute prices to whole-number percentages:**

TP and SL values must be **whole integers representing % from entry**. No decimals, no `%` symbol.

```
For LONG:
  tp = round((take_profit_price - entry_price) / entry_price * 100)
  sl = round((entry_price - stop_loss_price) / entry_price * 100)

For SHORT:
  tp = round((entry_price - take_profit_price) / entry_price * 100)
  sl = round((stop_loss_price - entry_price) / entry_price * 100)
```

Example: ETH at $2000, SL=$1960, TP=$2060 → `sl: 2`, `tp: 3`

**TP/SL rules:**
- Must provide both `tp` and `sl`, or neither
- If neither provided and `unsafe` is false, defaults apply: `tp=5`, `sl=3`
- Cannot combine `unsafe: true` with TP/SL

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

#### Confirming the Order Was Placed

After sending a trade request, the x402 flow (payment, swapping, deposit) can take time. **Do not spam another request for the same symbol.** Instead, confirm the order landed by querying:

```
GET https://x402-dev.voltperps.com/v1/account/orders
Headers: x-wallet-address: <your wallet address>
```

- If the order appears as open/filled, the trade succeeded — move on.
- If the order is not yet visible, wait and check again (up to 3 times, 10s apart).
- If after 3 checks the order still hasn't appeared and the original API call returned success, assume it's still processing — do NOT resend the same trade request.

#### Closing a Position

When the result has `action: "CLOSE"` and `approved: true`:

**Endpoint:** `POST https://x402-dev.voltperps.com/v1/intent` (free — no x402 payment required)

```json
{
  "intent": "close",
  "market": "ETH"
}
```

Closes the entire position for that market.

#### Retry Policy

**If any x402 API call fails, retry up to 3 times.** This applies to both trade and close intents.

- On error, wait a few seconds before retrying
- If the error is a transient issue (network timeout, `ALL_PROVIDERS_UNHEALTHY`, 5xx status), retry the same request
- If the error is a validation issue (`INVALID_MARKET`, `PARTIAL_TP_SL`, `LEVERAGE_NOT_SUPPORTED`), fix the parameters before retrying
- If all 3 retries fail, log the error and skip this trade — do NOT keep retrying indefinitely
- If `status: "pending"` in the response, poll `GET /v1/status/{jobId}` up to 3 times (wait 10s between polls) before giving up

#### Error Codes

| Code | When | Action |
|------|------|--------|
| `INVALID_MARKET` | Bad symbol | Fix symbol, retry |
| `LEVERAGE_NOT_SUPPORTED` | Leverage too high | Reduce leverage, retry |
| `INSUFFICIENT_BALANCE` | Not enough funds | **You should have caught this in Step 6.** Re-check balance, reduce position size, retry |
| `PARTIAL_TP_SL` | Only one of TP/SL given | Send both, retry |
| `POSITION_NOT_FOUND` | CLOSE on non-existent position | Skip — do not retry |
| `ALL_PROVIDERS_UNHEALTHY` | Exchange down | Wait, retry (up to 3 times) |

---

## Other Functions

### Check Stop-Loss / Take-Profit

Call this between analysis cycles to catch SL/TP triggers:

```python
events = system.check_stops()
# Returns list of close messages, e.g.:
# ["Closed PERP_ETH_USDC LONG @ 1960.00 (SL) PnL: $-16.00"]
```

If a stop was triggered and the position is live on-chain, use your **x402 skills** to close it:

```
POST https://x402-dev.voltperps.com/v1/intent
```
```json
{ "intent": "close", "market": "ETH" }
```

### Get Portfolio Status

```python
status = system.get_status()
```

Returns:
```python
{
    "current_budget": 984.00,
    "initial_budget": 1000.00,
    "available_budget": 824.00,
    "margin_in_use": 160.00,
    "unrealized_pnl": 12.50,
    "win_rate": 0.55,
    "total_trades": 20,
    "losing_streak": 0,
    "drawdown_from_peak": 0.016,
    "open_positions": [...],
    "recent_trades": [...],
    "cycles_completed": 5,
    "system_running": True
}
```

### Read On-Chain Account State

These are free read endpoints (no x402 payment). All require `x-wallet-address` header.

**Balance:**
```
GET https://x402-dev.voltperps.com/v1/account/balance
Headers: x-wallet-address: 0x...
```

Returns `smartWallet` (your USDC budget on Base) and `perpAccount` (collateral on exchange).

**Open Positions:**
```
GET https://x402-dev.voltperps.com/v1/account/positions
Headers: x-wallet-address: 0x...
```

**Single Position:**
```
GET https://x402-dev.voltperps.com/v1/account/positions/ETH
```

**Orders:**
```
GET https://x402-dev.voltperps.com/v1/account/orders?status=OPEN&symbol=ETH
```

### Shutdown

```python
summary = await system.stop()
```

---

## Risk Management (Enforced by Code)

You don't need to worry about these — the risk manager validates every decision automatically. But here's what it checks:

**8-layer validation on every decision:**

1. **Drawdown circuit breaker** — halts trading at 20% drawdown from peak, reduces size at 10%
2. **Confidence validation** — rejects below 0.1
3. **Budget zone access** — graduated reserve: Free (70%), Guarded (20%, requires proven win rate), Floor (5%, exceptional only), Lockout (5%, never touched)
4. **Stop-loss validation** — must exist, correct direction, 0.5-3.0x ATR range
5. **Risk/reward ratio** — minimum 1.5:1
6. **Position sizing** — max 2% loss per trade
7. **Total exposure** — cumulative margin across all symbols capped at 80%
8. **Position conflicts** — rejects duplicate positions on same symbol

---

## Signal Reference

### Indicators (pre-computed for you)

| Indicator | Parameters | Signal |
|-----------|-----------|--------|
| RSI | 14-period | <40 long zone, >60 short zone, extremes (<30, >70) strong |
| MACD | 12, 26, 9 | Crossovers, histogram direction = momentum |
| Bollinger Bands | 20-period, 2σ | %B <0.3 long, >0.7 short |
| EMA | 9, 21, 50 | 9>21>50 = bullish, reverse = bearish |
| VWAP | Session | Above = bullish bias, below = bearish |
| ATR | 14-period | Volatility for SL placement |
| Candle trend | Last 3 candles | Consecutive red/green, % change |
| Orderbook | Top levels | Imbalance, spread, depth |
| Volume delta | Recent trades | Buy vs sell aggression |

### Cross-Symbol Signals

- BTC often leads ETH and SOL
- All 3 moving same direction = stronger conviction
- Correlated moves confirm the trend

### Data Feeds (live via WebSocket)

| Feed | Update Speed |
|------|-------------|
| K-line 5m, 15m, 1h | ~1s |
| Orderbook | ~1s |
| Best Bid/Offer | ~10ms |
| Trades | Real-time |
| 24h Ticker | ~1s |
| Funding Rate | ~15s |
| Open Interest | ~1-10s |
| Mark/Index Price | ~1s |

---

## Configuration

Edit `config.yaml` to change symbols, budget, risk parameters, or network settings.

**Symbols:** PERP_ETH_USDC, PERP_BTC_USDC, PERP_SOL_USDC
**Budget:** $1000 (paper trading default)
**Network:** Mainnet (set `testnet: true` for testnet)
