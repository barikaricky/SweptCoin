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

import requests as _req
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
    get_paper_balance,
)
from database.db_setup import get_session
from database.models import Trade, ScreenedCoin


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


# ─── Live Price Fetch ─────────────────────────────────────────────────────────

def fetch_live_price(symbol: str) -> float | None:
    """
    Fetch the real-time spot price from Bybit's public ticker endpoint.
    Always uses the real (mainnet) API so paper trades track actual market prices.
    """
    try:
        resp = _req.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=5,
        ).json()
        return float(resp["result"]["list"][0]["lastPrice"])
    except Exception:
        return None


# ─── Performance Report ───────────────────────────────────────────────────────

def print_performance_report(cycle: int):
    """
    Print a full P&L summary after every cycle.
    Shows paper balance, win/loss record, and a trade-by-trade ledger.
    """
    session = get_session()
    try:
        all_trades = session.query(Trade).order_by(Trade.id.asc()).all()
        open_trades  = [t for t in all_trades if t.status == "OPEN"]
        closed_trades = [t for t in all_trades if t.status in ("WIN", "LOSS")]
        wins   = [t for t in closed_trades if t.status == "WIN"]
        losses = [t for t in closed_trades if t.status == "LOSS"]
        total_pnl  = sum(t.pnl_usdt or 0.0 for t in closed_trades)
        win_rate   = (len(wins) / len(closed_trades) * 100) if closed_trades else 0.0
        best_trade = max((t.pnl_usdt or 0.0 for t in closed_trades), default=0.0)
        worst_trade = min((t.pnl_usdt or 0.0 for t in closed_trades), default=0.0)

        bal_now   = get_paper_balance()
        bal_start = config.PAPER_STARTING_BALANCE
        bal_delta = bal_now - bal_start
        arrow = "▲" if bal_delta >= 0 else "▼"

        logger.info("=" * 60)
        logger.info(f"  📊 PERFORMANCE REPORT — Cycle #{cycle}")
        logger.info("=" * 60)
        logger.info(
            f"  Mode           : {'PAPER (Testnet)' if config.PAPER_TRADING else '⚠ LIVE'}"
        )
        logger.info(
            f"  Paper Wallet   : ${bal_start:.2f} start  →  ${bal_now:.2f} now   "
            f"{arrow} {bal_delta:+.4f} USDT"
        )
        logger.info(
            f"  Realised PnL   : ${total_pnl:+.4f} USDT"
        )
        logger.info(
            f"  Trades         : {len(all_trades)} total | "
            f"✅ {len(wins)} wins | ❌ {len(losses)} losses | 🟡 {len(open_trades)} open"
        )
        logger.info(f"  Win Rate       : {win_rate:.1f}%")
        if closed_trades:
            logger.info(f"  Best Trade     : ${best_trade:+.4f} USDT")
            logger.info(f"  Worst Trade    : ${worst_trade:+.4f} USDT")
        logger.info("-" * 60)

        if open_trades:
            logger.info("  OPEN POSITIONS:")
            for t in open_trades:
                trigger = (t.notes or "—").split("|")[0].strip()
                logger.info(
                    f"    #{t.id:<3} {t.symbol:<12} entry=${t.entry_price:.6g} | "
                    f"TP=${t.take_profit:.6g} | SL=${t.stop_loss:.6g} | [{trigger}]"
                )

        if closed_trades:
            logger.info("  CLOSED TRADES:")
            for t in closed_trades:
                pnl_str  = f"${t.pnl_usdt:+.4f}" if t.pnl_usdt is not None else "   —   "
                exit_str = f"${t.exit_price:.6g}" if t.exit_price else "  —  "
                trigger  = (t.notes or "—").split("|")[0].strip()
                icon = "✅" if t.status == "WIN" else "❌"
                logger.info(
                    f"    #{t.id:<3} {icon} {t.symbol:<12} "
                    f"${t.entry_price:.6g} → {exit_str} | PnL={pnl_str} | [{trigger}]"
                )

        logger.info("=" * 60)
    finally:
        session.close()


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

    if not _watchlist:
        logger.info("Watchlist is empty after sentiment filter.")
        return

    # ── Rich coin summary report ──
    logger.info("=" * 60)
    logger.info(f"  WATCHLIST — {len(_watchlist)} coins approved")
    logger.info("=" * 60)
    for coin in _watchlist:
        mcap_m = coin["market_cap_usd"] / 1_000_000
        age_yrs = coin["age_days"] / 365
        partners = coin.get("partners", [])
        partner_str = ", ".join(partners) if partners else "No recent partnership news"
        sentiment_score = coin.get("sentiment_score", 0.0)
        sentiment_dir = coin.get("sentiment_direction", "NEUTRAL")
        logger.info(
            f"  {coin['symbol']}\n"
            f"    Market Cap : ${mcap_m:.1f}M\n"
            f"    Age        : {age_yrs:.1f} years ({coin['age_days']} days)\n"
            f"    Sentiment  : {sentiment_dir} ({sentiment_score:+.3f})\n"
            f"    Partners   : {partner_str}\n"
            f"    P-Score    : {coin['partnership_score']}"
        )
    logger.info("=" * 60)


# ─── Single loop iteration ────────────────────────────────────────────────────

def run_one_cycle(cycle: int = 0):
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

    # Step 3 & 4: Analyse each coin and print full report
    current_prices = {}
    for coin in _watchlist:
        symbol = coin["symbol"]
        sentiment_score = coin.get("sentiment_score", 0.0)
        sentiment_direction = coin.get("sentiment_direction", "NEUTRAL")
        partners = coin.get("partners", [])
        mcap_m = coin["market_cap_usd"] / 1_000_000
        age_yrs = coin["age_days"] / 365

        signal = get_signal(symbol)
        current_price = signal.get("current_price")

        # ── Persist latest signal to DB so the dashboard can display it ──────
        _sig_session = get_session()
        try:
            sc_row = _sig_session.query(ScreenedCoin).filter_by(
                symbol=symbol, is_active=True
            ).order_by(ScreenedCoin.id.desc()).first()
            if sc_row:
                sc_row.last_signal = signal.get("signal", "HOLD")
                sc_row.signal_reason = (signal.get("reason") or "")[:200]
                sc_row.signal_time = datetime.now(timezone.utc)
                _sig_session.commit()
        except Exception as _e:
            logger.debug(f"Signal DB write failed for {symbol}: {_e}")
        finally:
            _sig_session.close()
        direction = signal.get("direction", "WAIT")
        thesis = signal.get("trade_thesis", "")

        # Append sentiment colour to thesis
        if thesis and sentiment_direction != "NEUTRAL":
            thesis += f" News mood is {sentiment_direction} (score {sentiment_score:+.2f})."

        if current_price:
            current_prices[symbol] = current_price

        partner_str = ", ".join(partners) if partners else "none in recent news"
        tp = signal.get("take_profit")
        sl = signal.get("stop_loss")
        support = signal.get("nearest_support")
        resistance = signal.get("nearest_resistance")

        logger.info("-" * 60)
        logger.info(f"  COIN     : {symbol}")
        logger.info(f"  Price    : ${current_price}" if current_price else "  Price    : unknown")
        logger.info(f"  Mkt Cap  : ${mcap_m:.1f}M  |  Age: {age_yrs:.1f} yrs")
        logger.info(f"  Partners : {partner_str}")
        logger.info(f"  Sentiment: {sentiment_direction} ({sentiment_score:+.3f})")
        if support:
            logger.info(f"  Support  : ${support:.6g}")
        if resistance:
            logger.info(f"  Resist   : ${resistance:.6g}")
        if tp and sl:
            logger.info(f"  TP / SL  : ${tp:.6g} / ${sl:.6g}")
        logger.info(f"  SIGNAL   : {signal['signal']} → {direction}")
        logger.info(f"  Analysis : {thesis}")
        logger.info("-" * 60)

        if signal["signal"] == "BUY":
            logger.info(f"  >>> EXECUTING TRADE on {symbol} <<<")
            enter_trade(signal, sentiment_score=sentiment_score)

    # Step 5: Monitor all open positions for TP/SL hits using real-time prices
    if current_prices:
        # Upgrade candle-close prices to live spot prices for accurate TP/SL checks
        live_prices: dict = {}
        for sym in list(current_prices.keys()):
            live = fetch_live_price(sym)
            live_prices[sym] = live if live is not None else current_prices[sym]
        check_open_positions(live_prices)

    # Step 6: Print P&L performance report every cycle
    print_performance_report(cycle)


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
                run_one_cycle(cycle)
            except Exception as e:
                logger.error(f"Unhandled error in cycle #{cycle}: {e}", exc_info=True)

            logger.info(f"Cycle #{cycle} complete. Sleeping {config.LOOP_INTERVAL_SECONDS}s...")
            time.sleep(config.LOOP_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("Shutdown requested. Swept Coin AI stopped.")


if __name__ == "__main__":
    main()
