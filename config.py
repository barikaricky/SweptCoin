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

# ─── Screener Settings ────────────────────────────────────────────────────────
MIN_MARKET_CAP_USD = 10_000_000          # $10M minimum
MAX_MARKET_CAP_USD = 100_000_000         # $100M maximum
MIN_COIN_AGE_DAYS = 60                   # Coin must be at least 60 days old
MAX_WATCHLIST_SIZE = 10                  # Max coins tracked simultaneously

PARTNERSHIP_KEYWORDS = [
    "partnered with", "partnership", "integration", "collaborat",
    "powered by", "backed by", "supported by", "alliance",
    "google", "microsoft", "amazon", "aws", "apple", "coinbase",
    "binance", "visa", "mastercard", "paypal", "blackrock",
]

# ─── Sentiment Settings ───────────────────────────────────────────────────────
MIN_SENTIMENT_SCORE = 0.05               # Below this → no trade (range: -1.0 to +1.0)
SENTIMENT_NEWS_LIMIT = 20                # Number of recent articles to analyse per coin

# ─── Technical Analysis Settings ─────────────────────────────────────────────
# Candle intervals to download (Bybit format)
CANDLE_INTERVAL_MINUTE = "1"            # 1-minute candles
CANDLE_INTERVAL_DAY = "D"               # Daily candles
HISTORY_DAYS = 60                       # Days of history to keep in database

# Support / Resistance
PIVOT_BOUNCE_COUNT = 3                  # Min number of price bounces to confirm a level
SUPPORT_PROXIMITY_PCT = 0.01            # Buy when price is within 1% of support
RESISTANCE_PROXIMITY_PCT = 0.01         # TP placed within 1% of resistance

# Volume spike detection
VOLUME_SPIKE_MULTIPLIER = 2.0           # Volume must be 2x the rolling average
VOLUME_ROLLING_WINDOW = 20              # Rolling average window (candles)

# ─── Risk & Execution Settings ────────────────────────────────────────────────
MAX_POSITION_SIZE_USDT = 10.0           # Maximum USDT per single trade
STOP_LOSS_PCT = 0.02                    # 2% stop-loss below entry price
MAX_OPEN_POSITIONS = 3                  # Never hold more than 3 coins at once
MAX_ACCOUNT_RISK_PCT = 0.05             # Never risk more than 5% of total balance
CONSECUTIVE_LOSS_HALT = 3              # Halt bot after this many losses in a row

# ─── Database ─────────────────────────────────────────────────────────────────
DATABASE_URL = "sqlite:///sweptcoin.db"

# ─── Loop Settings ────────────────────────────────────────────────────────────
LOOP_INTERVAL_SECONDS = 60              # How often the main loop runs (every 60s)
SCREENER_REFRESH_HOURS = 4              # Re-run screener every 4 hours

# ─── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE = "logs/trades.log"
LOG_LEVEL = "INFO"
