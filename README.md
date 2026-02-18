# Orderly Trader

LLM-powered perpetual futures trading system on Orderly Network. Connects to real-time WebSocket market data, computes technical indicators, sends structured snapshots to an LLM (Grok via OpenRouter), and executes validated trading decisions.

## Architecture

```
Orderly WS (x3 symbols) → DataCollectors → MarketSnapshots → Indicators → LLM → TradeDecisions → RiskManager → Execute/Reject
```

- **3 symbols**: PERP_ETH_USDC, PERP_BTC_USDC, PERP_SOL_USDC
- **3 timeframes**: 5m, 15m, 1h
- **Analysis every 5 minutes**: all symbols in one LLM call for cross-asset correlation
- **9-layer risk manager** with graduated reserve system has absolute veto power

## Project Structure

```
orderly-trader/
├── config.yaml                 # Runtime configuration (symbols, model, risk params)
├── prompt_template.md          # Standalone LLM prompt (works with any LLM)
├── STRATEGY.md                 # Full strategy design document
├── pyproject.toml
├── src/
│   ├── main.py                 # Async main loop
│   ├── collector.py            # Per-symbol WebSocket data collector
│   ├── indicators.py           # RSI, MACD, BB, EMA, VWAP, ATR (pure numpy)
│   ├── strategy.py             # Multi-symbol prompt builder + LLM response parser
│   ├── risk_manager.py         # Graduated reserve + 9-layer validation
│   ├── models/
│   │   ├── market.py           # KlineBuffer, OrderbookSnapshot, MarketSnapshot
│   │   ├── decision.py         # TradeDecision, ValidatedDecision, AnalysisCycle
│   │   ├── position.py         # Position, PortfolioState
│   │   └── config.py           # Pydantic config validation
│   └── adapters/
│       ├── base.py             # Abstract LLM adapter interface
│       └── openrouter_adapter.py  # OpenRouter API client (any model)
├── tests/
│   ├── test_indicators.py      # 20 tests
│   ├── test_risk_manager.py    # 16 tests
│   └── test_decision_parsing.py # 12 tests
└── logs/
    └── cycles_YYYYMMDD.jsonl   # Full audit trail per day
```

## Prerequisites

- Python 3.11+
- An [OpenRouter](https://openrouter.ai/) API key
- An Orderly Network account ID (for WebSocket data feeds)

## Setup

```bash
# Clone and enter the project
cd orderly-trader

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"
```

## Configuration

Edit `config.yaml`:

```yaml
symbols:
  - PERP_ETH_USDC
  - PERP_BTC_USDC
  - PERP_SOL_USDC

analysis_interval_seconds: 300  # 5 minutes
initial_budget: 1000.0
paper_trading: true

openrouter:
  # api_key: set via OPENROUTER_API_KEY environment variable
  base_url: https://openrouter.ai/api/v1
  model: x-ai/grok-3-mini
  reasoning_effort: high
  max_tokens: 4096
  temperature: 0.2

risk:
  max_loss_per_trade_pct: 0.02    # 2% per trade
  max_total_exposure_pct: 0.80    # 80% max exposure
  drawdown_reduce_pct: 0.10      # Reduce size at 10% drawdown
  drawdown_halt_pct: 0.20        # Halt trading at 20% drawdown

testnet: false
orderly_account_id: "your_orderly_account_id_here"

log_level: INFO
store_reasoning: true
```

### Environment Variables

```bash
export OPENROUTER_API_KEY="sk-or-..."
```

## Running

```bash
# Start the trading system
OPENROUTER_API_KEY=sk-or-... python -m src.main
```

The system will:
1. Backfill historical klines via REST API
2. Connect to Orderly WebSocket for real-time data (3 symbols)
3. Every 5 minutes: compute indicators → call LLM → validate → execute
4. Check SL/TP on all positions every cycle
5. Log full audit trail to `logs/cycles_YYYYMMDD.jsonl`

### Log Output

```
INFO  Backfilling PERP_ETH_USDC...
INFO  WebSocket connected for PERP_ETH_USDC
INFO  === Analysis Cycle 1 ===
INFO  PERP_ETH_USDC LONG: Bullish EMA alignment... (approved=True, lev=5.0, qty=0.0380)
INFO  PERP_BTC_USDC HOLD: Mixed signals... (approved=True, lev=1.0, qty=0.0000)
INFO  Opened PERP_ETH_USDC LONG @ 2018.92 qty=0.0380 lev=5.0x margin=$153.44
```

## Running Tests

```bash
# All 48 tests
python -m pytest tests/ -v

# Specific test file
python -m pytest tests/test_risk_manager.py -v

# Quick run
python -m pytest tests/ -q
```

## Switching LLM Models

Change the `model` field in `config.yaml` to any OpenRouter-supported model:

```yaml
openrouter:
  model: x-ai/grok-3-mini          # Default — returns reasoning chain
  # model: anthropic/claude-sonnet-4-20250514
  # model: openai/gpt-4o
  # model: deepseek/deepseek-chat
```

No code changes needed. The prompt in `prompt_template.md` works with any LLM.

## Key Design Decisions

| Decision | Why |
|----------|-----|
| OpenRouter (single adapter) | One API for every model. Switch via config. |
| Grok 3 Mini + reasoning | Returns readable reasoning chain for audit |
| Pure numpy for indicators | 10x lighter than pandas for fixed-size buffers |
| Multi-symbol single LLM call | LLM sees cross-symbol correlations |
| Graduated reserve system | Capital-efficient. Reserve unlocks based on proven performance. |
| Error → HOLD | System never acts when confused |

## Documentation

- **[STRATEGY.md](STRATEGY.md)** — Full strategy design: signal categories, decision matrix, risk management, x402 API execution format
- **[prompt_template.md](prompt_template.md)** — Standalone LLM prompt (can be used directly with any LLM)
