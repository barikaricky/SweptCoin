"""
database/models.py — SQLAlchemy ORM table definitions.
Defines what a price candle, trade, and screened coin look like in the database.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, Text, create_engine
)
from sqlalchemy.orm import declarative_base, sessionmaker
import config

Base = declarative_base()


class PriceCandle(Base):
    """Stores OHLCV candle data for each coin at a given interval."""
    __tablename__ = "price_candles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    interval = Column(String(5), nullable=False)          # e.g. "1" or "D"
    open_time = Column(DateTime, nullable=False, index=True)
    open = Column(Float, nullable=False)
    high = Column(Float, nullable=False)
    low = Column(Float, nullable=False)
    close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)

    def __repr__(self):
        return f"<PriceCandle {self.symbol} {self.interval} {self.open_time} close={self.close}>"


class Trade(Base):
    """Records every trade the bot makes (real or paper)."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)          # Null until trade is closed
    take_profit = Column(Float, nullable=False)
    stop_loss = Column(Float, nullable=False)
    quantity_usdt = Column(Float, nullable=False)
    is_paper = Column(Boolean, default=True)
    status = Column(String(10), default="OPEN")        # OPEN, WIN, LOSS
    entry_time = Column(DateTime, default=datetime.utcnow)
    exit_time = Column(DateTime, nullable=True)
    pnl_usdt = Column(Float, nullable=True)            # Profit or loss in USDT
    sentiment_score = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)

    def __repr__(self):
        return f"<Trade {self.symbol} {self.status} entry={self.entry_price} pnl={self.pnl_usdt}>"


class ScreenedCoin(Base):
    """Stores coins that passed the screener, with their metadata."""
    __tablename__ = "screened_coins"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False)
    base_currency = Column(String(10), nullable=False)  # e.g. "BTC" from "BTCUSDT"
    market_cap_usd = Column(Float, nullable=True)
    launch_date = Column(DateTime, nullable=True)
    age_days = Column(Integer, nullable=True)
    partnership_score = Column(Integer, default=0)      # Keyword hit count
    sentiment_score = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True)
    last_screened = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ScreenedCoin {self.symbol} mcap=${self.market_cap_usd:,.0f} age={self.age_days}d>"
