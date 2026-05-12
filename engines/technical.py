"""
engines/technical.py — Technical Analysis Engine.

Responsibilities:
  - Download and store OHLCV candle data from Bybit
  - Detect Support and Resistance levels using pivot point analysis
  - Detect volume spikes
  - Produce a BUY / HOLD signal with calculated TP and SL prices
"""

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional, Tuple

import pandas as pd
import numpy as np
import requests
from loguru import logger

import config
from database.db_setup import get_session
from database.models import PriceCandle

# Re-export config constants so backtest.py can import them from here
VOLUME_SPIKE_MULTIPLIER_CFG = config.VOLUME_SPIKE_MULTIPLIER


# ─── Data Download ────────────────────────────────────────────────────────────

def _bybit_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> List[List]:
    """
    Fetch kline (OHLCV) data from Bybit V5 API.
    Returns raw list of [startTime, open, high, low, close, volume, turnover].
    Bybit returns max 1000 candles per request.
    """
    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": interval,
        "start": start_ms,
        "end": end_ms,
        "limit": 1000,
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", {}).get("list", [])
    except Exception as e:
        logger.error(f"Bybit kline fetch failed [{symbol} {interval}]: {e}")
        return []


def download_history(symbol: str, interval: str = config.CANDLE_INTERVAL_MINUTE):
    """
    Download the last HISTORY_DAYS of candles for a symbol and save to DB.
    Only fetches candles not already stored (incremental update).
    """
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=config.HISTORY_DAYS)

        # Find the latest candle already in the DB
        latest = (
            session.query(PriceCandle)
            .filter_by(symbol=symbol, interval=interval)
            .order_by(PriceCandle.open_time.desc())
            .first()
        )
        if latest:
            fetch_from = latest.open_time + timedelta(minutes=1)
        else:
            fetch_from = start

        start_ms = int(fetch_from.timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)

        if start_ms >= end_ms:
            logger.debug(f"[{symbol}] Candle data up to date.")
            return

        logger.info(f"Downloading {symbol} [{interval}] from {fetch_from.date()} ...")

        # Bybit returns max 1000 at a time; paginate
        all_rows = []
        current_end = end_ms
        while True:
            rows = _bybit_klines(symbol, interval, start_ms, current_end)
            if not rows:
                break
            all_rows.extend(rows)
            # Rows are returned newest-first; oldest row determines next page end
            oldest_ts = int(rows[-1][0])
            if oldest_ts <= start_ms or len(rows) < 1000:
                break
            current_end = oldest_ts - 1

        if not all_rows:
            logger.warning(f"No candle data returned for {symbol}")
            return

        # Deduplicate and save
        existing_times = {
            r[0]
            for r in session.query(PriceCandle.open_time)
            .filter_by(symbol=symbol, interval=interval)
            .all()
        }
        new_candles = []
        for row in all_rows:
            ts = datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
            if ts not in existing_times:
                new_candles.append(PriceCandle(
                    symbol=symbol,
                    interval=interval,
                    open_time=ts,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                ))
        if new_candles:
            session.bulk_save_objects(new_candles)
            session.commit()
            logger.info(f"Saved {len(new_candles)} new candles for {symbol} [{interval}]")
    except Exception as e:
        session.rollback()
        logger.error(f"download_history error [{symbol}]: {e}")
    finally:
        session.close()


# ─── Load Data ────────────────────────────────────────────────────────────────

def load_candles(symbol: str, interval: str, days: int = config.HISTORY_DAYS) -> pd.DataFrame:
    """Load stored candles into a pandas DataFrame sorted oldest-first."""
    session = get_session()
    try:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = (
            session.query(PriceCandle)
            .filter(
                PriceCandle.symbol == symbol,
                PriceCandle.interval == interval,
                PriceCandle.open_time >= since,
            )
            .order_by(PriceCandle.open_time.asc())
            .all()
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([{
            "time": r.open_time,
            "open": r.open,
            "high": r.high,
            "low": r.low,
            "close": r.close,
            "volume": r.volume,
        } for r in rows])
        df.set_index("time", inplace=True)
        return df
    finally:
        session.close()


# ─── Support & Resistance ─────────────────────────────────────────────────────

def _find_pivot_lows(df: pd.DataFrame, window: int = 5) -> List[float]:
    """
    Identify pivot lows: candles where the low is the lowest in a ±window range.
    These form candidate Support levels.
    """
    lows = df["low"].values
    pivots = []
    for i in range(window, len(lows) - window):
        segment = lows[i - window: i + window + 1]
        if lows[i] == min(segment):
            pivots.append(lows[i])
    return pivots


def _find_pivot_highs(df: pd.DataFrame, window: int = 5) -> List[float]:
    """
    Identify pivot highs: candles where the high is the highest in a ±window range.
    These form candidate Resistance levels.
    """
    highs = df["high"].values
    pivots = []
    for i in range(window, len(highs) - window):
        segment = highs[i - window: i + window + 1]
        if highs[i] == max(segment):
            pivots.append(highs[i])
    return pivots


def _cluster_levels(prices: List[float], tolerance_pct: float = 0.015) -> List[float]:
    """
    Cluster nearby price levels. If two pivots are within tolerance_pct of each
    other, they are considered the same level (averaged). Returns confirmed levels
    sorted ascending.
    """
    if not prices:
        return []
    prices_sorted = sorted(prices)
    clusters = [[prices_sorted[0]]]
    for price in prices_sorted[1:]:
        ref = np.mean(clusters[-1])
        if abs(price - ref) / ref <= tolerance_pct:
            clusters[-1].append(price)
        else:
            clusters.append([price])
    # Only return clusters hit at least PIVOT_BOUNCE_COUNT times
    confirmed = [
        round(np.mean(c), 8)
        for c in clusters
        if len(c) >= config.PIVOT_BOUNCE_COUNT
    ]
    return sorted(confirmed)


def get_support_levels(df: pd.DataFrame) -> List[float]:
    pivots = _find_pivot_lows(df)
    return _cluster_levels(pivots)


def get_resistance_levels(df: pd.DataFrame) -> List[float]:
    pivots = _find_pivot_highs(df)
    return _cluster_levels(pivots)


# ─── Volume Spike ─────────────────────────────────────────────────────────────

def has_volume_spike(df: pd.DataFrame) -> bool:
    """
    Returns True if the most recent candle's volume is >= VOLUME_SPIKE_MULTIPLIER
    times the rolling average of the last VOLUME_ROLLING_WINDOW candles.
    """
    if len(df) < config.VOLUME_ROLLING_WINDOW + 1:
        return False
    rolling_avg = df["volume"].iloc[-(config.VOLUME_ROLLING_WINDOW + 1):-1].mean()
    latest_vol = df["volume"].iloc[-1]
    return latest_vol >= (rolling_avg * config.VOLUME_SPIKE_MULTIPLIER)


# ─── Signal Generation ────────────────────────────────────────────────────────

def get_signal(symbol: str) -> Dict:
    """
    Main entry point. Downloads fresh data, computes S/R levels, and returns
    a trading signal.

    Returns:
        {
            "symbol": str,
            "signal": "BUY" | "HOLD",
            "direction": "LONG" | "WAIT",
            "current_price": float,
            "entry_price": float | None,
            "take_profit": float | None,
            "stop_loss": float | None,
            "nearest_support": float | None,
            "nearest_resistance": float | None,
            "volume_spike": bool,
            "reason": str,
            "trade_thesis": str,   # human-readable case for the trade
        }
    """
    # Ensure we have fresh data
    download_history(symbol, config.CANDLE_INTERVAL_MINUTE)
    download_history(symbol, config.CANDLE_INTERVAL_DAY)

    # Use daily candles for S/R detection (cleaner pivots)
    df_day = load_candles(symbol, config.CANDLE_INTERVAL_DAY)
    # Use 1-min candles for volume spike detection
    df_min = load_candles(symbol, config.CANDLE_INTERVAL_MINUTE, days=1)

    no_signal = {
        "symbol": symbol, "signal": "HOLD", "direction": "WAIT",
        "current_price": None,
        "entry_price": None, "take_profit": None, "stop_loss": None,
        "nearest_support": None, "nearest_resistance": None,
        "volume_spike": False, "reason": "Insufficient data",
        "trade_thesis": "No thesis — insufficient data to analyse.",
    }

    if df_day.empty or len(df_day) < 10:
        return {**no_signal, "reason": "Not enough daily candle data"}

    current_price = df_day["close"].iloc[-1]

    supports = get_support_levels(df_day)
    resistances = get_resistance_levels(df_day)

    # Find nearest support below current price
    supports_below = [s for s in supports if s < current_price]
    nearest_support = max(supports_below) if supports_below else None

    # Find nearest resistance above current price
    resistances_above = [r for r in resistances if r > current_price]
    nearest_resistance = min(resistances_above) if resistances_above else None

    vol_spike = has_volume_spike(df_min) if not df_min.empty else False

    # ── BUY condition ──
    if nearest_support is not None:
        proximity = abs(current_price - nearest_support) / nearest_support
        near_support = proximity <= config.SUPPORT_PROXIMITY_PCT
    else:
        near_support = False

    if near_support and vol_spike and nearest_resistance is not None:
        take_profit = nearest_resistance
        stop_loss = round(current_price * (1 - config.STOP_LOSS_PCT), 8)
        rr_ratio = (take_profit - current_price) / (current_price - stop_loss)

        if rr_ratio < 1.0:
            reason = f"R/R ratio {rr_ratio:.2f} too low (need >= 1.0)"
            signal = "HOLD"
            direction = "WAIT"
            take_profit = None
            stop_loss = None
            trade_thesis = (
                f"Price is near the {nearest_support:.6g} support zone but the "
                f"reward-to-risk ratio is only {rr_ratio:.2f} (minimum 1.0 required). "
                f"The next resistance target at {nearest_resistance:.6g} is too close to justify entry. "
                f"Wait for a deeper pullback or a higher resistance level to improve the setup."
            )
        else:
            signal = "BUY"
            direction = "LONG"
            upside_pct = (take_profit - current_price) / current_price * 100
            downside_pct = config.STOP_LOSS_PCT * 100
            reason = (
                f"Price {current_price} within {proximity*100:.2f}% of support "
                f"{nearest_support} with volume spike. R/R={rr_ratio:.2f}"
            )
            trade_thesis = (
                f"GO LONG on {symbol}. "
                f"Price ({current_price:.6g}) is bouncing off a confirmed support zone at "
                f"{nearest_support:.6g} ({proximity*100:.2f}% away), which has held "
                f"{config.PIVOT_BOUNCE_COUNT}+ times historically. "
                f"A volume spike confirms buying pressure is entering. "
                f"Target the next resistance at {take_profit:.6g} for a "
                f"{upside_pct:.1f}% gain, with a hard stop at {stop_loss:.6g} "
                f"({downside_pct:.1f}% risk). Reward-to-risk ratio: {rr_ratio:.2f}x."
            )
    else:
        signal = "HOLD"
        direction = "WAIT"
        take_profit = None
        stop_loss = None
        parts = []
        if not near_support:
            nearest_str = f"{nearest_support:.6g}" if nearest_support else "none found"
            parts.append(f"price is not near a support zone (nearest={nearest_str})")
        if not vol_spike:
            parts.append("no volume spike — buyers not confirmed yet")
        if nearest_resistance is None:
            parts.append("no resistance level found for take-profit target")
        reason = "; ".join(parts) if parts else "Conditions not met"
        trade_thesis = (
            f"No trade on {symbol} right now. Reason: {reason}. "
            f"Wait for price to approach a support zone with a volume surge before entering."
        )

    logger.info(f"Signal [{symbol}] {signal} ({direction}) | price={current_price} | {reason}")

    return {
        "symbol": symbol,
        "signal": signal,
        "direction": direction,
        "current_price": current_price,
        "entry_price": current_price if signal == "BUY" else None,
        "take_profit": take_profit,
        "stop_loss": stop_loss,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
        "volume_spike": vol_spike,
        "reason": reason,
        "trade_thesis": trade_thesis,
    }
