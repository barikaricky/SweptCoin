"""
backtesting/backtest.py — Historical Strategy Backtester.

Replays stored candle data minute-by-minute and simulates trades using the
same TA logic as the live bot. Produces a performance report at the end.

Usage:
    python -m backtesting.backtest
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict

import pandas as pd
from loguru import logger

import config
from database.db_setup import init_db, get_session
from database.models import PriceCandle
from engines.technical import (
    get_support_levels,
    get_resistance_levels,
    _cluster_levels,
    _find_pivot_lows,
    _find_pivot_highs,
    VOLUME_SPIKE_MULTIPLIER_CFG,
)


# ─── Load all history for a symbol ────────────────────────────────────────────

def load_full_history(symbol: str, interval: str) -> pd.DataFrame:
    session = get_session()
    try:
        rows = (
            session.query(PriceCandle)
            .filter_by(symbol=symbol, interval=interval)
            .order_by(PriceCandle.open_time.asc())
            .all()
        )
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([{
            "time": r.open_time,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        } for r in rows]).set_index("time")
    finally:
        session.close()


# ─── Backtest core ────────────────────────────────────────────────────────────

class BacktestResult:
    def __init__(self):
        self.trades: List[Dict] = []

    def add(self, trade: Dict):
        self.trades.append(trade)

    def summary(self) -> Dict:
        if not self.trades:
            return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0.0, "total_pnl": 0.0}
        wins = [t for t in self.trades if t["outcome"] == "WIN"]
        losses = [t for t in self.trades if t["outcome"] == "LOSS"]
        total_pnl = sum(t["pnl_usdt"] for t in self.trades)
        win_rate = len(wins) / len(self.trades) * 100
        return {
            "total": len(self.trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "total_pnl_usdt": round(total_pnl, 4),
            "avg_win_usdt": round(sum(t["pnl_usdt"] for t in wins) / max(len(wins), 1), 4),
            "avg_loss_usdt": round(sum(t["pnl_usdt"] for t in losses) / max(len(losses), 1), 4),
        }

    def print_report(self):
        s = self.summary()
        print("\n" + "=" * 50)
        print("  SWEPT COIN AI — BACKTEST REPORT")
        print("=" * 50)
        print(f"  Total Trades : {s['total']}")
        print(f"  Wins         : {s['wins']}")
        print(f"  Losses       : {s['losses']}")
        print(f"  Win Rate     : {s['win_rate']}%")
        print(f"  Total PnL    : ${s.get('total_pnl_usdt', 0):+.4f} USDT")
        print(f"  Avg Win      : ${s.get('avg_win_usdt', 0):+.4f} USDT")
        print(f"  Avg Loss     : ${s.get('avg_loss_usdt', 0):+.4f} USDT")
        print("=" * 50 + "\n")


def run_backtest(symbol: str, usdt_per_trade: float = 10.0) -> BacktestResult:
    """
    Replay the full stored history for a symbol and simulate all trades.
    Uses a rolling window to avoid look-ahead bias.
    """
    logger.info(f"Starting backtest for {symbol}...")

    df_day = load_full_history(symbol, config.CANDLE_INTERVAL)
    df_min = load_full_history(symbol, config.CANDLE_INTERVAL)

    result = BacktestResult()

    if df_day.empty or len(df_day) < 20:
        logger.warning(f"Not enough daily data to backtest {symbol}")
        return result

    if df_min.empty:
        logger.warning(f"No minute data for {symbol}")
        return result

    # We need at least 20 daily candles as a warm-up window before starting
    WARMUP = 20
    open_trade = None

    for i in range(WARMUP, len(df_min)):
        current_time = df_min.index[i]
        current_price = df_min["close"].iloc[i]

        # ── Manage open trade ──
        if open_trade:
            if current_price >= open_trade["take_profit"]:
                pnl = (open_trade["take_profit"] - open_trade["entry_price"]) * (usdt_per_trade / open_trade["entry_price"])
                result.add({**open_trade, "exit_price": open_trade["take_profit"],
                             "exit_time": current_time, "outcome": "WIN", "pnl_usdt": round(pnl, 4)})
                open_trade = None
                continue
            elif current_price <= open_trade["stop_loss"]:
                pnl = (open_trade["stop_loss"] - open_trade["entry_price"]) * (usdt_per_trade / open_trade["entry_price"])
                result.add({**open_trade, "exit_price": open_trade["stop_loss"],
                             "exit_time": current_time, "outcome": "LOSS", "pnl_usdt": round(pnl, 4)})
                open_trade = None
                continue

        # ── Don't open a new trade if one is already open ──
        if open_trade:
            continue

        # ── Build rolling daily window (no look-ahead) ──
        daily_up_to_now = df_day[df_day.index <= current_time]
        if len(daily_up_to_now) < WARMUP:
            continue

        supports = get_support_levels(daily_up_to_now)
        resistances = get_resistance_levels(daily_up_to_now)

        supports_below = [s for s in supports if s < current_price]
        resistances_above = [r for r in resistances if r > current_price]

        if not supports_below or not resistances_above:
            continue

        nearest_support = max(supports_below)
        nearest_resistance = min(resistances_above)

        # ── Volume spike check (rolling window) ──
        vol_window = df_min["volume"].iloc[max(0, i - config.VOLUME_ROLLING_WINDOW): i]
        if len(vol_window) < 5:
            continue
        avg_vol = vol_window.mean()
        current_vol = df_min["volume"].iloc[i]
        vol_spike = current_vol >= (avg_vol * config.VOLUME_SPIKE_MULTIPLIER)

        # ── BUY condition ──
        proximity = abs(current_price - nearest_support) / nearest_support
        near_support = proximity <= config.SUPPORT_PROXIMITY_PCT

        if near_support and vol_spike:
            stop_loss = round(current_price * (1 - config.STOP_LOSS_PCT), 8)
            take_profit = nearest_resistance
            rr = (take_profit - current_price) / (current_price - stop_loss)
            if rr >= 1.0:
                open_trade = {
                    "symbol": symbol,
                    "entry_price": current_price,
                    "entry_time": current_time,
                    "take_profit": take_profit,
                    "stop_loss": stop_loss,
                }

    # If a trade is still open at end of data, close at last price
    if open_trade and len(df_min) > 0:
        last_price = df_min["close"].iloc[-1]
        pnl = (last_price - open_trade["entry_price"]) * (usdt_per_trade / open_trade["entry_price"])
        outcome = "WIN" if last_price > open_trade["entry_price"] else "LOSS"
        result.add({**open_trade, "exit_price": last_price,
                    "exit_time": df_min.index[-1], "outcome": outcome, "pnl_usdt": round(pnl, 4)})

    result.print_report()
    return result


def run_backtest_all(symbols: List[str]) -> Dict[str, BacktestResult]:
    """Run backtest across multiple symbols and print a combined summary."""
    all_results = {}
    for symbol in symbols:
        all_results[symbol] = run_backtest(symbol)

    # Combined stats
    all_trades = [t for r in all_results.values() for t in r.trades]
    if all_trades:
        wins = sum(1 for t in all_trades if t["outcome"] == "WIN")
        total = len(all_trades)
        total_pnl = sum(t["pnl_usdt"] for t in all_trades)
        print(f"\nCOMBINED RESULT | {total} trades | {wins/total*100:.1f}% win rate | ${total_pnl:+.4f} PnL\n")

    return all_results


if __name__ == "__main__":
    init_db()
    # Run against whatever coins are stored in the DB
    from database.db_setup import get_session
    from database.models import ScreenedCoin
    session = get_session()
    try:
        coins = session.query(ScreenedCoin).filter_by(is_active=True).all()
        symbols = [c.symbol for c in coins]
    finally:
        session.close()

    if not symbols:
        print("No screened coins found. Run main.py first to populate the watchlist.")
    else:
        run_backtest_all(symbols)
