# Orderly Trader

Perpetual futures trading system on Orderly Network with a callable Python API. Connects to real-time WebSocket market data, computes technical indicators, and exposes functions for an LLM to analyze markets and execute validated trading decisions via the x402 VoltPerps API.

## Architecture

```
Orderly WS (x3 symbols) → DataCollectors → MarketSnapshots → Indicators → LLM Prompt
                                                                              ↓
                                                              LLM analyzes + produces JSON
                                                                              ↓
                                                          RiskManager validates → Execute/Reject
                                                                              ↓
                                                          x402 VoltPerps API → Real order on Orderly
```

- **3 symbols**: PERP_ETH_USDC, PERP_BTC_USDC, PERP_SOL_USDC
- **3 timeframes**: 5m, 15m, 1h
- **LLM-controlled cadence**: call `get_prompt()` whenever you want a new analysis
- **9-layer risk manager** with graduated reserve system has absolute veto power

## Project Structure

```
orderly-trader/
├── config.yaml                 # Runtime configuration (symbols, risk params)
├── SKILL.md                    # Full skill document: how the LLM uses this system
├── prompt_template.md          # Standalone LLM prompt (works with any LLM)
├── pyproject.toml
├── src/
│   ├── main.py                 # TradingSystem class (callable API)
│   ├── collector.py            # Per-symbol WebSocket data collector
│   ├── indicators.py           # RSI, MACD, BB, EMA, VWAP, ATR (pure numpy)
│   ├── strategy.py             # Multi-symbol prompt builder + response parser
│   ├── risk_manager.py         # Graduated reserve + 9-layer validation
│   ├── models/
│   │   ├── market.py           # KlineBuffer, OrderbookSnapshot, MarketSnapshot
│   │   ├── decision.py         # TradeDecision, ValidatedDecision, AnalysisCycle
│   │   ├── position.py         # Position, PortfolioState
│   │   └── config.py           # Pydantic config validation
│   └── adapters/
│       └── base.py             # LLMResponse data structure
├── tests/
│   ├── test_indicators.py      # 20 tests
│   ├── test_risk_manager.py    # 16 tests
│   └── test_decision_parsing.py # 12 tests
└── logs/
    └── cycles_YYYYMMDD.jsonl   # Full audit trail per day
```

## Prerequisites

- Python 3.11+
- An Orderly Network account ID (for WebSocket data feeds)

## Setup

```bash
cd orderly-trader
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

The system exposes a `TradingSystem` class with callable methods:

```python
from src.main import TradingSystem

system = TradingSystem()
await system.start()           # Connects WebSockets, backfills kline data

# Each analysis cycle:
prompt = system.get_prompt()   # Returns {system_prompt, user_prompt, sl_tp_events}
# LLM reads the prompt, analyzes, produces JSON decision
result = system.submit_decision('{"decisions": [...]}')

# Anytime:
system.check_stops()           # Check SL/TP on open positions
system.get_status()            # Portfolio summary

await system.stop()            # Shutdown
```

See **[SKILL.md](SKILL.md)** for the full skill document with trading rules, signal analysis, and x402 API execution format.

## Configuration

Edit `config.yaml`:

```yaml
symbols:
  - PERP_ETH_USDC
  - PERP_BTC_USDC
  - PERP_SOL_USDC

initial_budget: 1000.0
paper_trading: true

risk:
  max_loss_per_trade_pct: 0.02
  max_total_exposure_pct: 0.80
  drawdown_reduce_pct: 0.10
  drawdown_halt_pct: 0.20

testnet: false
orderly_account_id: "your_orderly_account_id_here"

log_level: INFO
store_reasoning: true
```

## Running Tests

```bash
python -m pytest tests/ -v      # All 48 tests
python -m pytest tests/ -q      # Quick run
```

## Key Design Decisions

| Decision | Why |
|----------|-----|
| Callable API (not autonomous loop) | LLM controls the cadence and makes decisions directly |
| Pure numpy for indicators | 10x lighter than pandas for fixed-size buffers |
| Multi-symbol single prompt | LLM sees cross-symbol correlations |
| Graduated reserve system | Capital-efficient. Reserve unlocks based on proven performance. |
| Error → HOLD | System never acts when confused |

## Documentation

- **[SKILL.md](SKILL.md)** — Full skill document: trading rules, signal analysis, x402 API execution
- **[prompt_template.md](prompt_template.md)** — Standalone LLM prompt (can be used directly with any LLM)
