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

### Step 2: Analyze and Decide

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

### Step 3: Submit Your Decision

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
- `leverage`: 1-10 (will be capped by risk manager based on confidence)
- `quantity`: Position size in base asset. Use: `(budget * 0.02) / (entry_price * sl_distance_pct)`
- `stop_loss` / `take_profit`: Absolute prices. SL should be 1-2 ATR away. TP at 2:1+ risk/reward
- `confidence`: 0.0-1.0
- For HOLD: set leverage=1, quantity=0, stop_loss=0, take_profit=0, confidence=0
- For CLOSE: set quantity=0 (system closes full position)

### Step 4: Process the Result

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

### Step 5: Execute Approved Trades via x402 VoltPerps API

**When a decision is approved with action LONG or SHORT**, you MUST use your **x402 skills** to invoke the x402-enabled VoltPerps API and place the real order on Orderly Network.

The trade endpoint (`POST /v1/intent`) is an **x402-enabled endpoint** — it will respond with `402 Payment Required` on the first request. Use your x402 skills to handle the payment flow: your x402 skill will automatically pay the required USDC on Base and retry with the payment receipt.

For each approved decision in `result["decisions"]`:

```python
if decision["approved"] and decision["action"] in ("LONG", "SHORT"):
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

#### Error Codes

| Code | When | Action |
|------|------|--------|
| `INVALID_MARKET` | Bad symbol | Check `/v1/markets` |
| `LEVERAGE_NOT_SUPPORTED` | Leverage too high | Reduce leverage |
| `INSUFFICIENT_BALANCE` | Not enough funds | Reduce position size |
| `PARTIAL_TP_SL` | Only one of TP/SL given | Always send both |
| `POSITION_NOT_FOUND` | CLOSE on non-existent position | Skip |
| `ALL_PROVIDERS_UNHEALTHY` | Exchange down | Retry after delay |

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

**9-layer validation on every decision:**

1. **Drawdown circuit breaker** — halts trading at 20% drawdown from peak, reduces size at 10%
2. **Confidence validation** — rejects below 0.1
3. **Leverage cap** — scales max leverage to confidence (0.4 confidence → max 2x, even if you say 10x)
4. **Budget zone access** — graduated reserve: Free (70%), Guarded (20%, requires proven win rate), Floor (5%, exceptional only), Lockout (5%, never touched)
5. **Stop-loss validation** — must exist, correct direction, 0.5-3.0x ATR range
6. **Risk/reward ratio** — minimum 1.5:1
7. **Position sizing** — max 2% loss per trade
8. **Total exposure** — cumulative margin across all symbols capped at 80%
9. **Position conflicts** — rejects duplicate positions on same symbol

**Leverage scaling by confidence:**

| Confidence | Max Leverage |
|------------|-------------|
| 0.0 - 0.3 | 1x |
| 0.3 - 0.5 | 2x |
| 0.5 - 0.7 | 5x |
| 0.7 - 0.85 | 7x |
| 0.85 - 1.0 | 10x |

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
