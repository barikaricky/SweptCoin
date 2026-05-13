"""
engines/technical.py — Technical Analysis Engine.

Responsibilities:
  - Download and store OHLCV candle data from Bybit (4-hour candles)
  - Detect Support and Resistance using pivot analysis (confirmed clusters)
  - Compute EMA, RSI, MACD in pure pandas/numpy (Python 3.14 compatible)
  - Produce BUY / HOLD signal via three independent triggers:
      A) Support Bounce  — near confirmed support + RSI oversold + volume spike
      B) EMA Crossover   — EMA9 > EMA21 (fresh cross) + above EMA50 + volume spike
      C) MACD Crossover  — MACD line > signal line (fresh cross) + above EMA50
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


def download_history(symbol: str, interval: str = config.CANDLE_INTERVAL):
    """Download the last HISTORY_DAYS of 4H candles and save to DB (incremental)."""
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

def load_candles(symbol: str, interval: str = config.CANDLE_INTERVAL, days: int = config.HISTORY_DAYS) -> pd.DataFrame:
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


def _cluster_levels(prices: List[float], tolerance_pct: float = 0.02) -> List[float]:
    """
    Cluster nearby price levels. Returns confirmed levels (hit >= PIVOT_BOUNCE_COUNT times).
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


# ─── Pure-Pandas Indicators ───────────────────────────────────────────────────

def _calc_ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average — pure pandas."""
    return series.ewm(span=period, adjust=False).mean()


def _calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """
    Relative Strength Index using Wilder smoothing (ewm com = period-1).
    Returns values 0–100. NaN values filled with 50 (neutral).
    """
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)


def _calc_macd(series: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    """
    MACD indicator — returns (macd_line, signal_line, histogram) as pd.Series.
    """
    ema_fast = _calc_ema(series, fast)
    ema_slow = _calc_ema(series, slow)
    macd = ema_fast - ema_slow
    signal = _calc_ema(macd, sig)
    hist = macd - signal
    return macd, signal, hist


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
    Main entry point. Downloads fresh 4-hour candles, computes EMA/RSI/MACD,
    and returns a trading signal using one of three independent buy triggers:

      A) Support Bounce  — price within SUPPORT_PROXIMITY_PCT of confirmed support
                           + RSI < RSI_OVERSOLD + volume spike
      B) EMA Crossover   — EMA(FAST) just crossed above EMA(SLOW)
                           + price > EMA(TREND) + RSI < 60 + volume spike
      C) MACD Crossover  — MACD line just crossed above signal line
                           + price > EMA(TREND) + RSI < 60

    TP  = nearest resistance above price (fallback: +5%).
    SL  = entry × (1 - STOP_LOSS_PCT).
    Trade only fires if R/R ≥ 1.0.
    """
    download_history(symbol, config.CANDLE_INTERVAL)
    df = load_candles(symbol, config.CANDLE_INTERVAL)

    _no_data = {
        "symbol": symbol, "signal": "HOLD", "direction": "WAIT",
        "current_price": None, "entry_price": None,
        "take_profit": None, "stop_loss": None,
        "nearest_support": None, "nearest_resistance": None,
        "volume_spike": False, "reason": "Insufficient data",
        "trade_thesis": "No thesis — insufficient data.",
    }

    if df.empty or len(df) < 50:
        return {**_no_data, "reason": "Need 50+ candles"}

    current_price = float(df["close"].iloc[-1])
    close = df["close"]

    # ── Compute indicators ──
    ema9      = _calc_ema(close, config.EMA_FAST)
    ema21     = _calc_ema(close, config.EMA_SLOW)
    ema50     = _calc_ema(close, config.EMA_TREND)
    rsi       = _calc_rsi(close, config.RSI_PERIOD)
    macd_line, sig_line, _ = _calc_macd(close)

    rsi_now    = float(rsi.iloc[-1])
    ema9_now   = float(ema9.iloc[-1])
    ema21_now  = float(ema21.iloc[-1])
    ema50_now  = float(ema50.iloc[-1])
    macd_now   = float(macd_line.iloc[-1])
    sig_now    = float(sig_line.iloc[-1])
    ema9_prev  = float(ema9.iloc[-2])
    ema21_prev = float(ema21.iloc[-2])
    macd_prev  = float(macd_line.iloc[-2])
    sig_prev   = float(sig_line.iloc[-2])

    vol_spike   = has_volume_spike(df)
    above_ema50 = current_price > ema50_now

    # ── S/R levels ──
    supports    = get_support_levels(df)
    resistances = get_resistance_levels(df)
    supports_below    = [s for s in supports    if s < current_price]
    resistances_above = [r for r in resistances if r > current_price]
    nearest_support    = max(supports_below)    if supports_below    else None
    nearest_resistance = min(resistances_above) if resistances_above else None

    # TP = nearest resistance; fallback = current price +5%
    tp_target = nearest_resistance if nearest_resistance else round(current_price * 1.05, 8)

    # ── Nested helpers (Python closure — share outer scope) ──────────────────

    def _buy(trigger: str, detail: str) -> Dict:
        entry = current_price
        tp    = tp_target
        sl    = round(entry * (1 - config.STOP_LOSS_PCT), 8)
        if entry <= sl:
            return _hold("SL calculation error")
        rr = (tp - entry) / (entry - sl)
        if rr < 1.0:
            return _hold(f"R/R {rr:.2f} < 1.0 on {trigger}")
        upside = (tp - entry) / entry * 100
        reason = f"{trigger} | RSI={rsi_now:.1f} | vol={vol_spike} | R/R={rr:.2f}"
        thesis = (
            f"BUY \u2014 {trigger}. {detail} "
            f"RSI={rsi_now:.1f}, EMA9/21/50={ema9_now:.4g}/{ema21_now:.4g}/{ema50_now:.4g}. "
            f"TP=${tp:.6g} (+{upside:.1f}%), SL=${sl:.6g} (-{config.STOP_LOSS_PCT*100:.1f}%), "
            f"R/R={rr:.2f}x."
        )
        logger.info(f"Signal [{symbol}] BUY (LONG) | price={current_price} | {reason}")
        return {
            "symbol": symbol, "signal": "BUY", "direction": "LONG",
            "current_price": current_price, "entry_price": entry,
            "take_profit": tp, "stop_loss": sl,
            "nearest_support": nearest_support, "nearest_resistance": nearest_resistance,
            "volume_spike": vol_spike, "reason": reason, "trade_thesis": thesis,
        }

    def _hold(reason: str) -> Dict:
        parts = []
        if nearest_support:
            prox = abs(current_price - nearest_support) / nearest_support * 100
            parts.append(
                f"support={nearest_support:.4g} ({prox:.1f}% away, "
                f"need <{config.SUPPORT_PROXIMITY_PCT * 100:.0f}%)"
            )
        else:
            parts.append("no confirmed support")
        parts.append(f"RSI={rsi_now:.1f}")
        parts.append(f"EMA9={ema9_now:.4g} vs EMA21={ema21_now:.4g}")
        parts.append(f"MACD={macd_now:.4g} vs sig={sig_now:.4g}")
        parts.append(f"vol_spike={vol_spike}")
        full_reason = f"{reason} | " + " | ".join(parts[:3])
        thesis = (
            f"No trade on {symbol}. {reason}. "
            f"Conditions: {'; '.join(parts)}. "
            f"Wait for support bounce, EMA cross, or MACD cross."
        )
        logger.info(f"Signal [{symbol}] HOLD (WAIT) | price={current_price} | {full_reason}")
        return {
            "symbol": symbol, "signal": "HOLD", "direction": "WAIT",
            "current_price": current_price, "entry_price": None,
            "take_profit": None, "stop_loss": None,
            "nearest_support": nearest_support, "nearest_resistance": nearest_resistance,
            "volume_spike": vol_spike, "reason": full_reason, "trade_thesis": thesis,
        }

    # ── Trigger A: Support Bounce ──────────────────────────────────────────────
    # Near confirmed support + RSI not overbought. Volume is a bonus, not required.
    if nearest_support is not None:
        prox = abs(current_price - nearest_support) / nearest_support
        if prox <= config.SUPPORT_PROXIMITY_PCT and rsi_now < config.RSI_OVERSOLD:
            vol_note = "Volume spike confirms entry." if vol_spike else "Low volume — tight SL advised."
            return _buy(
                "Support Bounce",
                f"Price {current_price:.4g} is within {prox * 100:.1f}% of confirmed support "
                f"{nearest_support:.4g} (held {config.PIVOT_BOUNCE_COUNT}+ times). "
                f"RSI={rsi_now:.1f} — not overbought. {vol_note}",
            )

    # ── Trigger B: EMA9/21 Crossover ──────────────────────────────────────────
    # Fresh cross above EMA21 with macro trend (EMA50) pointing up. No vol spike required.
    ema_crossed = (ema9_prev <= ema21_prev) and (ema9_now > ema21_now)
    if ema_crossed and above_ema50 and rsi_now < 65:
        return _buy(
            "EMA9/21 Crossover",
            f"EMA9 ({ema9_now:.4g}) just crossed above EMA21 ({ema21_now:.4g}). "
            f"Price above EMA50 ({ema50_now:.4g}) confirms the macro trend is up. "
            f"RSI={rsi_now:.1f}.",
        )

    # ── Trigger C: MACD Crossover ─────────────────────────────────────────────
    # MACD line crosses above signal — momentum shift. No EMA50 filter needed.
    macd_crossed = (macd_prev <= sig_prev) and (macd_now > sig_now)
    if macd_crossed and rsi_now < 65:
        return _buy(
            "MACD Crossover",
            f"MACD line ({macd_now:.4g}) just crossed above signal ({sig_now:.4g}) — "
            f"momentum is turning bullish. RSI={rsi_now:.1f}.",
        )

    # ── Trigger D: EMA Alignment (Uptrend Riding) ─────────────────────────────
    # All three EMAs bullishly stacked (9 > 21 > 50) + RSI in healthy zone + MACD positive.
    # Catches established uptrends that the crossover triggers already missed.
    ema_aligned = (ema9_now > ema21_now) and (ema21_now > ema50_now)
    if ema_aligned and 45 <= rsi_now <= 65 and macd_now > 0:
        return _buy(
            "EMA Alignment (Uptrend)",
            f"EMA9 ({ema9_now:.4g}) > EMA21 ({ema21_now:.4g}) > EMA50 ({ema50_now:.4g}) — "
            f"all EMAs bullishly stacked. RSI={rsi_now:.1f} in healthy range. "
            f"MACD={macd_now:.4g} positive — momentum with the trend.",
        )

    # ── No trigger fired ──────────────────────────────────────────────────────
    return _hold("no buy trigger (checked: Support Bounce, EMA Cross, MACD Cross, EMA Alignment)")
