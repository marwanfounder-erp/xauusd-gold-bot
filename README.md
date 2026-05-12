# XAUUSD Gold Bot

London + NY Breakout strategy for XAUUSD (Gold). Completely separate from the EURUSD bot.

## Strategy

| Parameter | Value |
|-----------|-------|
| Symbol | XAUUSD |
| Timeframe | H1 |
| Asian Range | $3 – $15 |
| Breakout Buffer | $0.20 |
| RSI Buy | > 60 |
| RSI Sell | < 40 |
| EMA Trend Filter | 20-period |
| Risk/Trade | 1% |
| RR Ratio | 1:2 |
| Sessions | London (07–10 UTC) + NY (13–16 UTC) |

## Project Structure

```
xauusd-gold-bot/
├── main.py               # entry point
├── config.py             # settings
├── bot/
│   ├── strategy.py       # signal logic
│   ├── risk_manager.py   # lot size, guards
│   ├── executor.py       # order execution
│   ├── news_filter.py    # ForexFactory filter
│   ├── notifier.py       # Telegram alerts
│   ├── data_feed.py      # MT5 live feed
│   ├── paper_feed.py     # yfinance paper feed
│   ├── database.py       # Neon PostgreSQL
│   └── dashboard_server.py  # web dashboard port 8081
├── backtest/
│   └── backtest_engine.py
└── logs/
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Copy and fill .env
cp .env.example .env

# Run backtest (recommended first)
python backtest/backtest_engine.py

# Paper trading
python main.py --paper

# Live trading (requires MT5)
python main.py --live
```

## Dashboard

Opens at `http://localhost:8081` — gold-themed, auto-refreshes every 10 seconds.

## Deployment (Railway)

1. Create a **new** Railway service (separate from EURUSD bot)
2. Set all env vars from `.env.example`
3. Deploy — starts in `--paper` mode automatically
4. Dashboard URL: your Railway service URL

## Gold-specific sizing

```
pip = $0.01
pip_value = $1.00 per lot per pip
lot = risk_amount / (sl_pips × pip_value)
```

## Magic ID: 303030 (different from EURUSD bot 202020)
