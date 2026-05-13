"""
config.py — All tunable settings for Swept Coin AI.
Change values here without touching engine code.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ─── API Credentials (loaded from .env) ───────────────────────────────────────
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")       # Optional: CoinGecko Pro
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# ─── Bot Mode ─────────────────────────────────────────────────────────────────
# Set to True to log trades without sending real orders to Bybit
PAPER_TRADING = True
PAPER_STARTING_BALANCE = 50.0           # Virtual USDT wallet for paper trading

# ─── Screener Settings ────────────────────────────────────────────────────────
MIN_MARKET_CAP_USD = 300_000_000         # $300M minimum — liquid, established coins
MAX_MARKET_CAP_USD = 10_000_000_000      # $10B maximum — avoids BTC/ETH/BNB (too slow)
MIN_COIN_AGE_DAYS = 60                   # Coin must be at least 60 days old
MAX_WATCHLIST_SIZE = 10                  # Max coins tracked simultaneously

PARTNERSHIP_KEYWORDS = [
    "partnered with", "partnership", "integration", "collaborat",
    "powered by", "backed by", "supported by", "alliance",
    "google", "microsoft", "amazon", "aws", "apple", "coinbase",
    "binance", "visa", "mastercard", "paypal", "blackrock",
]

# ─── Sentiment Settings ───────────────────────────────────────────────────────
# Only block coins with actively BEARISH news (< -0.15).
# Neutral (0.0) or no-news coins (score=0.0) are treated as fine to trade.
MIN_SENTIMENT_SCORE = -0.15              # Only block BEARISH coins; NEUTRAL passes
SENTIMENT_NEWS_LIMIT = 20                # Number of recent articles to analyse per coin

# ─── Technical Analysis Settings ─────────────────────────────────────────────
# Bybit candle interval: "1"=1m  "60"=1H  "240"=4H  "D"=daily
CANDLE_INTERVAL = "240"                 # 4-hour candles — best signal clarity
HISTORY_DAYS = 60                       # Days of OHLCV history to keep

# Support / Resistance
PIVOT_BOUNCE_COUNT = 2                  # Touches to confirm a level (2 = reliable on 4H)
SUPPORT_PROXIMITY_PCT = 0.03            # Enter when price is within 3% of support
RESISTANCE_PROXIMITY_PCT = 0.03         # TP placed near resistance

# Volume spike detection
VOLUME_SPIKE_MULTIPLIER = 1.5           # Volume must be 1.5x rolling average
VOLUME_ROLLING_WINDOW = 20              # Rolling average window (candles)

# Momentum Indicators (pure pandas — no external TA library required)
RSI_PERIOD = 14
RSI_OVERSOLD = 52                       # RSI below this = not overbought, valid near-support entry
EMA_FAST = 9                            # Fast EMA for crossover signal
EMA_SLOW = 21                           # Slow EMA for crossover signal
EMA_TREND = 50                          # Trend filter — only buy above EMA50

# ─── Risk & Execution Settings ────────────────────────────────────────────────
MAX_POSITION_SIZE_USDT = 10.0           # Maximum USDT per single trade
STOP_LOSS_PCT = 0.02                    # 2% stop-loss below entry price
TRAILING_STOP_PCT = 0.015               # Trailing stop — raises floor as price rises
MAX_OPEN_POSITIONS = 3                  # Never hold more than 3 coins at once
MAX_ACCOUNT_RISK_PCT = 0.05             # Never risk more than 5% of total balance
CONSECUTIVE_LOSS_HALT = 3              # Halt bot after this many losses in a row

# ─── Database ─────────────────────────────────────────────────────────────────
DATABASE_URL = "sqlite:///sweptcoin.db"

# ─── Loop Settings ────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = 900             # Main loop every 15 min — aligned with 4H candle cycle
SCREENER_REFRESH_HOURS = 4              # Re-run screener every 4 hours

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE = "logs/trades.log"
LOG_LEVEL = "INFO"
