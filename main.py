"""
main.py — Master Loop for Swept Coin AI.

Runs 24/7 following the strategy workflow:
  1. Scan    → Screener finds qualifying coins
  2. Verify  → Sentiment engine approves them
  3. Track   → TA engine reads chart signals
  4. Execute → Execution engine buys/manages positions
  5. Repeat  → Sleep and restart cycle
"""

import time
import sys
from datetime import datetime, timezone

from loguru import logger

import config
from database.db_setup import init_db
from engines.screener import run_screener
from engines.sentiment import filter_by_sentiment
from engines.technical import get_signal
from engines.execution import (
    enter_trade,
    check_open_positions,
    is_trading_halted,
)


# ─── Logging setup ────────────────────────────────────────────────────────────

def setup_logging():
    logger.remove()
    logger.add(
        sys.stdout,
        level=config.LOG_LEVEL,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )
    logger.add(
        config.LOG_FILE,
        level="INFO",
        rotation="10 MB",
        retention="30 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
    )


# ─── Screener refresh logic ───────────────────────────────────────────────────

_watchlist: list = []
_last_screener_run: datetime = None


def _should_refresh_screener() -> bool:
    if not _watchlist or _last_screener_run is None:
        return True
    elapsed_hours = (datetime.now(timezone.utc) - _last_screener_run).total_seconds() / 3600
    return elapsed_hours >= config.SCREENER_REFRESH_HOURS


def refresh_watchlist():
    global _watchlist, _last_screener_run
    logger.info("Refreshing watchlist via Screener...")
    raw_coins = run_screener()
    approved = filter_by_sentiment(raw_coins)
    _watchlist = approved
    _last_screener_run = datetime.now(timezone.utc)
    symbols = [c["symbol"] for c in _watchlist]
    logger.info(f"Watchlist updated: {symbols}")


# ─── Single loop iteration ────────────────────────────────────────────────────

def run_one_cycle():
    """Execute one full scan-verify-track-execute cycle."""

    if is_trading_halted():
        logger.warning("Trading is HALTED due to consecutive losses. Waiting for manual reset.")
        return

    # Step 1 & 2: Refresh screener + sentiment if needed
    if _should_refresh_screener():
        refresh_watchlist()

    if not _watchlist:
        logger.info("Watchlist is empty. Waiting for next screener run.")
        return

    # Step 3 & 4: Check each coin's chart signal
    current_prices = {}
    for coin in _watchlist:
        symbol = coin["symbol"]
        sentiment_score = coin.get("sentiment_score", 0.0)

        signal = get_signal(symbol)
        current_price = signal.get("current_price")
        direction = signal.get("direction", "WAIT")
        thesis = signal.get("trade_thesis", "")

        # Enrich thesis with sentiment context
        sentiment_direction = coin.get("sentiment_direction", "NEUTRAL")
        if thesis and sentiment_direction != "NEUTRAL":
            thesis += f" News sentiment is {sentiment_direction} (score {sentiment_score:+.2f})."

        if current_price:
            current_prices[symbol] = current_price

        # Always log direction + thesis so every coin gives a clear verdict
        logger.info(
            f"[{symbol}] {signal['signal']} ({direction}) | "
            f"price={current_price} | sentiment={sentiment_score:+.2f}"
        )
        if thesis:
            logger.info(f"  Thesis: {thesis}")

        if signal["signal"] == "BUY":
            logger.info(f"  >>> Entering trade on {symbol}...")
            enter_trade(signal, sentiment_score=sentiment_score)

    # Step 5: Monitor all open positions for TP/SL hits
    if current_prices:
        check_open_positions(current_prices)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("  Swept Coin AI — Starting Up")
    logger.info(f"  Mode: {'PAPER TRADING' if config.PAPER_TRADING else '>>> LIVE TRADING <<<'}")
    logger.info(f"  Bybit: {'Testnet' if config.BYBIT_TESTNET else 'MAINNET'}")
    logger.info("=" * 60)

    # Initialise database tables
    init_db()
    logger.info("Database initialised.")

    if not config.BYBIT_API_KEY or not config.BYBIT_API_SECRET:
        logger.warning("No Bybit API keys found in .env — running in data-only mode.")

    cycle = 0
    try:
        while True:
            cycle += 1
            logger.info(f"--- Cycle #{cycle} | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} ---")
            try:
                run_one_cycle()
            except Exception as e:
                logger.error(f"Unhandled error in cycle #{cycle}: {e}", exc_info=True)

            logger.info(f"Cycle #{cycle} complete. Sleeping {config.LOOP_INTERVAL_SECONDS}s...")
            time.sleep(config.LOOP_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("Shutdown requested. Swept Coin AI stopped.")


if __name__ == "__main__":
    main()
