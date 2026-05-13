"""
engines/execution.py — Execution & Risk Engine.

Responsibilities:
  - Place BUY orders on Bybit (real or paper)
  - Set Take-Profit and Stop-Loss orders immediately after entry
  - Track open positions
  - Halt trading after consecutive losses
  - Log every action to the trades database and log file
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from loguru import logger

import config
from database.db_setup import get_session
from database.models import Trade

# Bybit SDK — only imported when not paper trading
_bybit_client = None

# ─── Paper trading balance (virtual wallet) ───────────────────────────────────
_paper_balance: float = config.PAPER_STARTING_BALANCE


def get_paper_balance() -> float:
    """Return the current simulated USDT balance for paper trading."""
    return round(_paper_balance, 4)


def _get_bybit_client():
    """Lazy-load the Bybit HTTP client."""
    global _bybit_client
    if _bybit_client is None:
        from pybit.unified_trading import HTTP
        _bybit_client = HTTP(
            testnet=config.BYBIT_TESTNET,
            api_key=config.BYBIT_API_KEY,
            api_secret=config.BYBIT_API_SECRET,
        )
    return _bybit_client


# ─── Consecutive loss tracker (in-memory) ────────────────────────────────────

_consecutive_losses = 0
_trading_halted = False


def reset_loss_counter():
    global _consecutive_losses, _trading_halted
    _consecutive_losses = 0
    _trading_halted = False


def is_trading_halted() -> bool:
    return _trading_halted


# ─── Account info ─────────────────────────────────────────────────────────────

def get_usdt_balance() -> float:
    """Return current USDT balance. In paper mode returns the tracked virtual balance."""
    if config.PAPER_TRADING:
        return _paper_balance
    try:
        client = _get_bybit_client()
        resp = client.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        balance = (
            resp["result"]["list"][0]["coin"][0]["availableToWithdraw"]
        )
        return float(balance)
    except Exception as e:
        logger.error(f"Failed to fetch USDT balance: {e}")
        return 0.0


def count_open_positions() -> int:
    """Return number of currently open trades in the database."""
    session = get_session()
    try:
        return session.query(Trade).filter_by(status="OPEN").count()
    finally:
        session.close()


# ─── Order placement ──────────────────────────────────────────────────────────

def _calculate_quantity(symbol: str, entry_price: float, usdt_amount: float) -> float:
    """Calculate coin quantity from a USDT amount at the given entry price."""
    if entry_price <= 0:
        return 0.0
    return round(usdt_amount / entry_price, 6)


def _place_real_order(symbol: str, side: str, qty: float, order_type: str = "Market", price: float = None) -> Optional[str]:
    """Place an order on Bybit. Returns the order ID or None on failure."""
    try:
        client = _get_bybit_client()
        params = {
            "category": "spot",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
        }
        if order_type == "Limit" and price:
            params["price"] = str(price)
        resp = client.place_order(**params)
        order_id = resp["result"]["orderId"]
        logger.info(f"Order placed: {side} {qty} {symbol} | ID={order_id}")
        return order_id
    except Exception as e:
        logger.error(f"Order placement failed [{symbol} {side}]: {e}")
        return None


# ─── Risk checks ──────────────────────────────────────────────────────────────

def _pre_trade_checks(signal: Dict) -> Tuple[bool, str]:
    """
    Validate all risk rules before entering a trade.
    Returns (approved: bool, reason: str).
    """
    if _trading_halted:
        return False, f"Trading halted after {config.CONSECUTIVE_LOSS_HALT} consecutive losses"

    open_count = count_open_positions()
    if open_count >= config.MAX_OPEN_POSITIONS:
        return False, f"Max open positions reached ({open_count}/{config.MAX_OPEN_POSITIONS})"

    balance = get_usdt_balance()
    if balance <= 0:
        return False, "Zero USDT balance"

    max_risk = balance * config.MAX_ACCOUNT_RISK_PCT
    trade_size = min(config.MAX_POSITION_SIZE_USDT, max_risk)
    if trade_size < 1.0:
        return False, f"Trade size too small (${trade_size:.2f})"

    return True, f"Pre-trade checks passed. Size=${trade_size:.2f}"


# ─── Enter trade ──────────────────────────────────────────────────────────────

def enter_trade(signal: Dict, sentiment_score: float = 0.0) -> Optional[Trade]:
    """
    Execute a BUY based on a TA signal dict from technical.get_signal().
    Logs to DB. In paper mode, no real order is sent.

    Returns the Trade record or None if blocked.
    """
    symbol = signal["symbol"]
    entry_price = signal["entry_price"]
    take_profit = signal["take_profit"]
    stop_loss = signal["stop_loss"]

    approved, reason = _pre_trade_checks(signal)
    if not approved:
        logger.warning(f"Trade blocked [{symbol}]: {reason}")
        return None

    balance = get_usdt_balance()
    max_risk = balance * config.MAX_ACCOUNT_RISK_PCT
    usdt_amount = min(config.MAX_POSITION_SIZE_USDT, max_risk)
    qty = _calculate_quantity(symbol, entry_price, usdt_amount)

    if config.PAPER_TRADING:
        global _paper_balance
        _paper_balance = round(_paper_balance - usdt_amount, 4)
        logger.info(
            f"[PAPER] BUY {symbol} | entry={entry_price} | qty={qty} | "
            f"TP={take_profit} | SL={stop_loss} | ${usdt_amount:.2f} USDT "
            f"| balance=${_paper_balance:.2f}"
        )
    else:
        order_id = _place_real_order(symbol, "Buy", qty)
        if not order_id:
            return None
        logger.info(f"[LIVE] BUY executed {symbol} qty={qty} @ {entry_price}")

    # Persist to database
    session = get_session()
    try:
        trade = Trade(
            symbol=symbol,
            entry_price=entry_price,
            take_profit=take_profit,
            stop_loss=stop_loss,
            trailing_stop=round(entry_price * (1 - config.TRAILING_STOP_PCT), 8),
            quantity_usdt=usdt_amount,
            is_paper=config.PAPER_TRADING,
            status="OPEN",
            entry_time=datetime.now(timezone.utc),
            sentiment_score=sentiment_score,
            notes=signal.get("reason", ""),
        )
        session.add(trade)
        session.commit()
        session.refresh(trade)
        logger.info(f"Trade #{trade.id} opened for {symbol}")
        return trade
    except Exception as e:
        session.rollback()
        logger.error(f"Failed to save trade to DB [{symbol}]: {e}")
        return None
    finally:
        session.close()


# ─── Close trade ──────────────────────────────────────────────────────────────

def close_trade(trade_id: int, exit_price: float, outcome: str):
    """
    Close an open trade. outcome must be 'WIN' or 'LOSS'.
    Updates DB, logs result, and manages consecutive loss counter.
    """
    global _consecutive_losses, _trading_halted

    session = get_session()
    try:
        trade = session.query(Trade).filter_by(id=trade_id).first()
        if not trade:
            logger.error(f"Trade #{trade_id} not found in DB")
            return

        pnl = (exit_price - trade.entry_price) * (trade.quantity_usdt / trade.entry_price)

        trade.exit_price = exit_price
        trade.exit_time = datetime.now(timezone.utc)
        trade.pnl_usdt = round(pnl, 4)
        trade.status = outcome
        session.commit()

        # Return the original stake + profit/loss back to the paper wallet
        if trade.is_paper:
            global _paper_balance
            returned = round(trade.quantity_usdt + pnl, 4)
            _paper_balance = round(_paper_balance + returned, 4)

        pnl_pct = (pnl / trade.quantity_usdt) * 100 if trade.quantity_usdt else 0
        logger.info(
            f"Trade #{trade_id} CLOSED | {trade.symbol} | {outcome} | "
            f"entry={trade.entry_price} → exit={exit_price} | "
            f"PnL=${pnl:+.4f} ({pnl_pct:+.1f}%) | balance=${_paper_balance:.2f}"
        )

        # Update consecutive loss counter
        if outcome == "LOSS":
            _consecutive_losses += 1
            if _consecutive_losses >= config.CONSECUTIVE_LOSS_HALT:
                _trading_halted = True
                logger.warning(
                    f"TRADING HALTED: {_consecutive_losses} consecutive losses. "
                    "Manual review required. Call reset_loss_counter() to resume."
                )
        else:
            _consecutive_losses = 0

    except Exception as e:
        session.rollback()
        logger.error(f"Failed to close trade #{trade_id}: {e}")
    finally:
        session.close()


# ─── Monitor open positions ───────────────────────────────────────────────────

def check_open_positions(current_prices: Dict[str, float]):
    """
    Check each open position against its TP/SL.
    Also ratchets the trailing stop upward as price rises to lock in profit.
    """
    session = get_session()
    try:
        open_trades = session.query(Trade).filter_by(status="OPEN").all()
        for trade in open_trades:
            price = current_prices.get(trade.symbol)
            if price is None:
                continue

            # Ratchet trailing stop up once price is 1%+ in profit
            if price > trade.entry_price * 1.01 and trade.trailing_stop is not None:
                new_trail = round(price * (1 - config.TRAILING_STOP_PCT), 8)
                if new_trail > trade.trailing_stop:
                    trade.trailing_stop = new_trail
                    session.commit()
                    logger.info(
                        f"Trade #{trade.id} [{trade.symbol}] trailing stop "
                        f"raised to {new_trail:.6g}"
                    )

            # Effective stop = whichever is higher: original SL or trailing stop
            effective_sl = max(
                trade.stop_loss,
                trade.trailing_stop if trade.trailing_stop else 0,
            )

            if price >= trade.take_profit:
                close_trade(trade.id, price, "WIN")
            elif price <= effective_sl:
                outcome = "WIN" if price > trade.entry_price else "LOSS"
                close_trade(trade.id, price, outcome)
    finally:
        session.close()



