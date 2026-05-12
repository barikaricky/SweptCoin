# Swept Coin AI

An autonomous cryptocurrency trading bot for Bybit that combines **Technical Analysis**, **Fundamental Analysis**, and **Sentiment Analysis** to trade small-cap USDT pairs. Built in Python.

---

## How It Works

The bot runs a continuous loop 24/7 across four specialised engines:

```
Screener Engine
    → Filters Bybit coins by age (>60 days) and market cap ($10M–$100M)
    → Scores partnership activity from recent news

Sentiment Engine
    → Reads news via CryptoPanic / NewsAPI
    → Blocks any coin with a negative market mood (VADER scoring)

Technical Engine
    → Identifies Support and Resistance levels from 60 days of price history
    → Only triggers a BUY when price is at support AND volume spikes

Execution Engine
    → Places BUY orders on Bybit (paper or live)
    → Immediately sets Take-Profit at nearest resistance
    → Sets Stop-Loss 2% below entry
    → Halts automatically after 3 consecutive losses
```

---

## Project Structure

```
SweptCoin/
├── .env                    ← Your API keys (never commit this)
├── .env.example            ← Template to copy
├── config.py               ← All tunable settings
├── main.py                 ← Master 24/7 loop
├── requirements.txt
├── engines/
│   ├── screener.py         ← Coin discovery (Bybit + CoinGecko)
│   ├── sentiment.py        ← News sentiment scoring (VADER)
│   ├── technical.py        ← Support/Resistance + volume signal
│   └── execution.py        ← Order placement and risk management
├── database/
│   ├── db_setup.py         ← SQLite initialisation
│   └── models.py           ← PriceCandle, Trade, ScreenedCoin tables
├── backtesting/
│   └── backtest.py         ← Replay 60-day history, measure win rate
├── logs/
│   └── trades.log          ← Every trade decision logged here
└── tests/
```

---

## Setup

### 1. Clone and enter the project

```bash
git clone <your-repo-url>
cd "SweptCoin"
```

### 2. Create and activate a virtual environment

**Git Bash / MINGW64 (Windows):**
```bash
python -m venv venv
source venv/Scripts/activate
```

**PowerShell (Windows):**
```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

**macOS / Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install --prefer-binary -r requirements.txt
```

> The `--prefer-binary` flag tells pip to use pre-built wheels instead of compiling from source. This avoids build tool errors on Windows.

### 4. Configure API keys

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `BYBIT_API_KEY` | [Bybit Testnet](https://testnet.bybit.com/user/api-management) |
| `BYBIT_API_SECRET` | Same page |
| `BYBIT_TESTNET` | Set `true` until ready for live trading |
| `CRYPTOPANIC_API_KEY` | [cryptopanic.com/developers/api](https://cryptopanic.com/developers/api/) |
| `NEWSAPI_KEY` | [newsapi.org](https://newsapi.org/) |
| `COINGECKO_API_KEY` | Optional — free tier works without it |

---

## Running the Bot

### Paper trading (safe, default)

`PAPER_TRADING = True` is already set in `config.py`. No real orders are sent.

```bash
python main.py
```

All decisions are logged to `logs/trades.log`.

### Backtesting

First run `main.py` for at least one cycle to populate the database with screened coins and price history. Then:

```bash
python -m backtesting.backtest
```

This replays 60 days of stored data and prints a win-rate report.

### Live trading

Only proceed after:
- Backtesting shows a consistent **60%+ win rate**
- Two weeks of paper trading confirms the strategy
- You have reviewed `logs/trades.log` and are satisfied

Then in `config.py`:
```python
PAPER_TRADING = False
BYBIT_TESTNET = False  # Switch to mainnet
MAX_POSITION_SIZE_USDT = 10.0  # Start small — max $10 per trade
```

---

## Configuration Reference

All settings live in `config.py`. Key values:

| Setting | Default | Description |
|---|---|---|
| `PAPER_TRADING` | `True` | Paper mode — no real orders |
| `MIN_MARKET_CAP_USD` | `10_000_000` | Minimum $10M market cap |
| `MAX_MARKET_CAP_USD` | `100_000_000` | Maximum $100M market cap |
| `MIN_COIN_AGE_DAYS` | `60` | Coin must be older than 60 days |
| `MIN_SENTIMENT_SCORE` | `0.05` | Block trades below this news score |
| `STOP_LOSS_PCT` | `0.02` | 2% stop-loss below entry |
| `MAX_POSITION_SIZE_USDT` | `10.0` | Max USDT per single trade |
| `MAX_OPEN_POSITIONS` | `3` | Never hold more than 3 coins at once |
| `CONSECUTIVE_LOSS_HALT` | `3` | Auto-halt after 3 losses in a row |
| `LOOP_INTERVAL_SECONDS` | `60` | Main loop frequency |

---

## Development Phases

| Phase | Status | Description |
|---|---|---|
| 1 | ✅ Done | Environment setup, folder structure, dependencies |
| 2 | ✅ Done | Screener engine — filters coins by age, cap, partnerships |
| 3 | 🔄 Next | Data gathering — download 60 days of minute candles to DB |
| 4 | ⬜ | Backtest — prove win rate on historical data |
| 5 | ⬜ | Paper trading — 2-week dry run on live market |
| 6 | ⬜ | Live deployment — start with $50 USDT |

---

## Security

- **Never commit `.env`** — it is in `.gitignore`
- API keys are read only from environment variables, never hardcoded
- Start with **read-only** Bybit API keys during phases 1–3
- Enable **IP whitelisting** on your Bybit API key for live trading

---

## License

MIT
