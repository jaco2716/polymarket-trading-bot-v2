# Polymarket Trading Bot v2

Automated prediction market trading bot for [Polymarket](https://polymarket.com) with AI-powered analysis and whale copy-trading.

## Architecture

```
main.py                    Entry point + scan orchestrator
├── clients/               API wrappers (CLOB, Anthropic, HTTP)
├── services/              Core logic (execution, resolution, market discovery)
├── strategies/            Trade signal generation (Haiku AI, whale copy)
└── utils/                 Helpers (logging, fees, DB, news, filters)
```

## Strategies

1. **Haiku Analysis** — Claude Haiku evaluates markets against recent news for mispricing
2. **Whale Copy** — Mirrors positions from top weekly profitable wallets on Polymarket

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY
python main.py
deactivate              # Exit venv when done
```

## Modes

Switch between modes by setting `STRATEGY_MODE` in `.env`:

| Mode | Description | Real Money | Budget File |
|------|-------------|-----------|-------------|
| `compete` | Paper trades; both strategies run independently | No | budget.json |
| `shadow` | Hypothetical trades only; lower thresholds for data collection | No | shadow_budget.json |
| `live` | Real CLOB orders on Polygon mainnet | **Yes** | live_budget.json |

### Live Mode

```bash
# In .env:
STRATEGY_MODE="live"
POLYMARKET_PRIVATE_KEY="0x..."
LIVE_DRY_RUN="true"          # Start with dry-run to verify
# MAX_LIVE_TRADE_USDC="50"   # Hard cap per trade
# LIVE_MAX_OPEN_TRADES="5"   # Max concurrent positions
```

Set `LIVE_DRY_RUN="false"` to place real orders. Requires an EOA wallet with USDC on Polygon.

### Shadow Mode

Shadow mode uses lower confidence thresholds to collect training data. Set `SHADOW_SCAN_INTERVAL` to scan faster:

```bash
STRATEGY_MODE="shadow"
SHADOW_SCAN_INTERVAL="900"   # 15 min (~$12/mo in API costs)
```

## Configuration

All settings are environment variables documented in `.env.example`. Key parameters:

- `TRADE_SIZE_PCT` — Fraction of budget per trade (default 3%)
- `HAIKU_MIN_CONF` — Minimum Haiku confidence for live/paper trades (default 0.65)
- `WHALE_MIN_SIZE` — Minimum USD position to copy from whales (default $500)
- `SCAN_INTERVAL` — Seconds between scans (default 3600)

## Dashboard

The dashboard reads from the same SQLite database (`paper_trades.db`):

```bash
python dashboard/dashboard.py
```
