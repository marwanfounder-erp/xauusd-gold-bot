import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

YAHOO_SYMBOLS = {
    "XAUUSD": "GC=F",
    "XAUUSD=X": "GC=F",
}

PAPER_SPREAD_DOLLARS = 0.20   # $0.20 spread
PAPER_STARTING_BALANCE = 5000.0


class PaperDataFeed:
    def __init__(self, config, db=None):
        self.config = config
        self.db = db
        self._positions: list = []
        self._next_ticket = 1

    def _yahoo_symbol(self, symbol: str) -> str:
        return YAHOO_SYMBOLS.get(symbol, symbol)

    def get_tick(self, symbol: str) -> Optional[dict]:
        for attempt in range(3):
            try:
                ticker = yf.Ticker(self._yahoo_symbol(symbol))
                data = ticker.history(period="1d", interval="1m", timeout=10)
                if data.empty:
                    return None
                price = float(data["Close"].iloc[-1])
                half = PAPER_SPREAD_DOLLARS / 2
                return {
                    "bid": round(price - half, 2),
                    "ask": round(price + half, 2),
                    "spread_dollars": PAPER_SPREAD_DOLLARS,
                    "time": datetime.utcnow(),
                }
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    logger.warning(f"PaperFeed get_tick failed after 3 attempts: {e}")
        return None

    def get_candles(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        tf_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "4h": "4h", "1d": "1d",
        }
        interval = tf_map.get(timeframe, "1h")

        # yfinance period selection
        if interval in ("1m",):
            period = "7d"
        elif interval in ("5m", "15m", "30m"):
            period = "60d"
        elif interval in ("1h",):
            period = "730d"
        else:
            period = "2y"

        for attempt in range(3):
            try:
                ticker = yf.Ticker(self._yahoo_symbol(symbol))
                data = ticker.history(period=period, interval=interval, timeout=15)
                if data.empty:
                    return None
                data.index = data.index.tz_localize(None) if data.index.tzinfo else data.index
                data.columns = [c.lower() for c in data.columns]
                data = data[["open", "high", "low", "close", "volume"]].tail(count)
                return data
            except Exception as e:
                if attempt < 2:
                    time.sleep(2)
                else:
                    logger.warning(f"PaperFeed get_candles failed after 3 attempts: {e}")
        return None

    def get_account_info(self) -> dict:
        balance = PAPER_STARTING_BALANCE
        if self.db:
            try:
                balance = self.db.get_paper_balance(self.config.symbol) or PAPER_STARTING_BALANCE
            except Exception:
                pass
        open_profit = sum(p.get("unrealized_profit", 0.0) for p in self._positions)
        return {
            "balance": balance,
            "equity": balance + open_profit,
            "margin": 0.0,
            "free_margin": balance + open_profit,
            "profit": open_profit,
        }

    def get_positions(self) -> list:
        # Update unrealized P&L
        for pos in self._positions:
            tick = self.get_tick(pos["symbol"])
            if tick:
                if pos["type"] == "BUY":
                    pos["unrealized_profit"] = (tick["bid"] - pos["price_open"]) * pos["volume"] * 100
                else:
                    pos["unrealized_profit"] = (pos["price_open"] - tick["ask"]) * pos["volume"] * 100
        return list(self._positions)

    def open_position(self, symbol: str, direction: str, volume: float,
                      entry: float, sl: float, tp: float) -> dict:
        ticket = self._next_ticket
        self._next_ticket += 1
        pos = {
            "ticket": ticket,
            "symbol": symbol,
            "type": direction,
            "volume": volume,
            "price_open": entry,
            "sl": sl,
            "tp": tp,
            "profit": 0.0,
            "unrealized_profit": 0.0,
            "time": datetime.utcnow(),
        }
        self._positions.append(pos)
        logger.info(f"Paper position opened | {direction} {volume} lots @ {entry:.2f} SL={sl:.2f} TP={tp:.2f}")
        return pos

    def close_position(self, ticket: int, close_price: float, reason: str = "manual") -> Optional[dict]:
        for i, pos in enumerate(self._positions):
            if pos["ticket"] == ticket:
                if pos["type"] == "BUY":
                    pnl = (close_price - pos["price_open"]) * pos["volume"] * 100
                else:
                    pnl = (pos["price_open"] - close_price) * pos["volume"] * 100
                pos["profit"] = round(pnl, 2)
                pos["close_price"] = close_price
                pos["close_time"] = datetime.utcnow()
                pos["close_reason"] = reason
                self._positions.pop(i)
                logger.info(f"Paper position closed | ticket={ticket} pnl=${pnl:.2f} reason={reason}")
                return pos
        return None

    def update_sl(self, ticket: int, new_sl: float) -> bool:
        for pos in self._positions:
            if pos["ticket"] == ticket:
                pos["sl"] = new_sl
                return True
        return False

    def connect(self) -> bool:
        logger.info("PaperFeed initialized (yfinance GC=F)")
        return True

    def disconnect(self):
        pass
