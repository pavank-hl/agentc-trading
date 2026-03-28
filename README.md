# Orderly Trader

Perpetual futures trading system on Orderly Network with a collector daemon and a single CLI analysis surface. The daemon keeps market data fresh, while `python -m src.cli analyze ...` is the supported way for an LLM to prepare prompts, validate responses, and record monitoring events before executing trades via the x402 VoltPerps API.

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
- **Collector + CLI split**: daemon keeps data fresh, CLI performs every analysis/submit step
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

The supported runtime path is:

```bash
# 1. Run the collector daemon once
.venv/bin/python daemon.py

# 2. Create an analysis session from the latest daemon snapshot
.venv/bin/python -m src.cli analyze prepare

# 3. If positions exist, generate a position-management prompt
.venv/bin/python -m src.cli analyze prepare-position \
  --session-file logs/analysis_sessions/<id>.json \
  --analysis-file analysis.json \
  --positions-file positions.json

# 4. Submit the final response for validation + monitoring
.venv/bin/python -m src.cli analyze submit \
  --session-file logs/analysis_sessions/<id>.json \
  --response-file decision.json
```

See **[SKILL.md](SKILL.md)** for the full skill document with trading rules, signal analysis, and x402 API execution format.

## Configuration

Edit `config.yaml`:

```yaml
symbols:
  - PERP_ETH_USDC
  - PERP_BTC_USDC
  - PERP_SOL_USDC

risk:
  max_loss_per_trade_pct: 0.02
  max_total_exposure_pct: 0.80
  min_sl_atr_multiple: 0.5
  max_sl_atr_multiple: 3.0

rest_base_url: https://api-evm.orderly.org
orderly_account_id: "your_orderly_account_id_here"

log_level: INFO
store_reasoning: true
```

## Environment Variables

Create an env file from the example:

```bash
cp .env.example .env
```

Important values:

- `TAAPI_SECRET` — required for indicator enrichment
- `VOLT_API_URL` — base URL of the `volt-minions` API
- `BOT_MONITORING_API_KEY` — bot-only key used to ingest decision events
- `USER_ID` — user id attached to monitoring events
- `AGENT_NAME` — agent label attached to monitoring events

## Running Tests

```bash
python -m pytest tests/ -v      # All 48 tests
python -m pytest tests/ -q      # Quick run
```

## Key Design Decisions

| Decision | Why |
|----------|-----|
| Collector daemon + CLI | One cached market-data loop, one supported analysis/submit interface |
| Pure numpy for indicators | 10x lighter than pandas for fixed-size buffers |
| Multi-symbol single prompt | LLM sees cross-symbol correlations |
| Graduated reserve system | Capital-efficient. Reserve unlocks based on proven performance. |
| Error → HOLD | System never acts when confused |

## Documentation

- **[SKILL.md](SKILL.md)** — Full skill document: trading rules, signal analysis, x402 API execution
- **[prompt_template.md](prompt_template.md)** — Standalone LLM prompt (can be used directly with any LLM)
