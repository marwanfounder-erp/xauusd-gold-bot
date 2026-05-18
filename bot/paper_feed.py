"""
Paper data feed — yfinance primary, supports XAUUSD, GBPUSD, USDJPY.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import certifi
import requests

logger = logging.getLogger(__name__)

PAPER_STARTING_BALANCE = 5000.0

FINNHUB_BASE = "https://finnhub.io/api/v1"
FINNHUB_SYMBOL = "OANDA:XAU_USD"   # Gold/USD spot on Finnhub (fallback)

FINNHUB_RESOLUTION = {
    "1m": "1", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "4h": "240", "1d": "D",
}

# Yahoo Finance tickers per symbol
YAHOO_SYMBOLS = {
    'XAUUSD': 'GC=F',
    'GBPUSD': 'GBPUSD=X',
    'USDJPY': 'JPY=X',
    'EURUSD': 'EURUSD=X',
}


def _pair_settings(symbol: str) -> dict:
    from config import PAIR_SETTINGS
    return PAIR_SETTINGS.get(symbol, {
        'pip_size': 0.01, 'pip_value': 1.0, 'spread': 30,
    })


class PaperDataFeed:
    def __init__(self, config, db=None):
        self.config = config
        self.db = db
        self._positions: list = []
        self._next_ticket = 1
        self._api_key: str = getattr(config, "finnhub_api_key", "")

    # ── Finnhub helpers (XAUUSD fallback) ────────────────────────────────────

    def _finnhub_get(self, endpoint: str, params: dict) -> Optional[dict]:
        if not self._api_key:
            return None
        try:
            params["token"] = self._api_key
            resp = requests.get(
                f"{FINNHUB_BASE}/{endpoint}",
                params=params,
                timeout=8,
                verify=certifi.where(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Finnhub {endpoint} error: {e}")
            return None

    # ── yfinance helpers ──────────────────────────────────────────────────────

    def _yfinance_tick(self, symbol: str) -> Optional[dict]:
        try:
            import yfinance as yf
            yf.set_tz_cache_location("/tmp/yfinance")
            yahoo_sym = YAHOO_SYMBOLS.get(symbol, symbol)
            ticker = yf.Ticker(yahoo_sym)
            data = ticker.history(period="1d", interval="1m", timeout=10)
            if data.empty:
                return None
            price = float(data["Close"].iloc[-1])

            pair = _pair_settings(symbol)
            spread_price = pair['spread'] * pair['pip_size']
            half = spread_price / 2

            decimals = 2 if symbol == "XAUUSD" else (3 if "JPY" in symbol else 5)
            return {
                "bid":    round(price - half, decimals),
                "ask":    round(price + half, decimals),
                "spread": pair['spread'],
                "symbol": symbol,
                "time":   datetime.utcnow(),
                "source": "yfinance",
            }
        except Exception as e:
            logger.warning(f"yfinance tick {symbol} failed: {e}")
            return None

    def _yfinance_candles(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        tf_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "4h": "4h", "1d": "1d",
        }
        period_map = {
            "1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d",
            "1h": "730d", "4h": "730d", "1d": "2y",
        }
        interval = tf_map.get(timeframe, "1h")
        period   = period_map.get(interval, "730d")
        try:
            import yfinance as yf
            yf.set_tz_cache_location("/tmp/yfinance")
            yahoo_sym = YAHOO_SYMBOLS.get(symbol, symbol)
            ticker = yf.Ticker(yahoo_sym)
            data = ticker.history(period=period, interval=interval, timeout=15)
            if data.empty:
                return None
            data.columns = [c.lower() for c in data.columns]
            if data.index.tzinfo is not None:
                data.index = data.index.tz_convert("UTC").tz_localize(None)
            data = data[["open", "high", "low", "close", "volume"]].dropna()
            data = data[~data.index.duplicated(keep="first")].sort_index()
            return data.tail(count)
        except Exception as e:
            logger.warning(f"yfinance candles {symbol} failed: {e}")
            return None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_tick(self, symbol: str) -> Optional[dict]:
        tick = self._yfinance_tick(symbol)
        if tick:
            return tick
        # Finnhub fallback (XAUUSD only)
        if symbol == "XAUUSD":
            data = self._finnhub_get("quote", {"symbol": FINNHUB_SYMBOL})
            if data and data.get("c"):
                price = float(data["c"])
                pair  = _pair_settings(symbol)
                spread_price = pair['spread'] * pair['pip_size']
                half  = spread_price / 2
                return {
                    "bid":    round(price - half, 2),
                    "ask":    round(price + half, 2),
                    "spread": pair['spread'],
                    "symbol": symbol,
                    "time":   datetime.utcnow(),
                    "source": "finnhub",
                }
        logger.warning(f"tick fetch failed for {symbol}")
        return None

    def get_candles(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        df = self._yfinance_candles(symbol, timeframe, count)
        if df is not None:
            return df

        # Finnhub fallback (XAUUSD only)
        if symbol == "XAUUSD":
            resolution = FINNHUB_RESOLUTION.get(timeframe, "60")
            bar_seconds = {"1": 60, "5": 300, "15": 900, "30": 1800,
                           "60": 3600, "240": 14400, "D": 86400}
            secs   = bar_seconds.get(resolution, 3600)
            to_ts  = int(datetime.now(timezone.utc).timestamp())
            from_ts = to_ts - (secs * count * 2)
            data = self._finnhub_get(
                "forex/candle",
                {"symbol": FINNHUB_SYMBOL, "resolution": resolution,
                 "from": from_ts, "to": to_ts},
            )
            if data and data.get("s") == "ok" and data.get("c"):
                try:
                    df = pd.DataFrame({
                        "open":   data["o"], "high":   data["h"],
                        "low":    data["l"], "close":  data["c"],
                        "volume": data.get("v", [0] * len(data["c"])),
                    }, index=pd.to_datetime(data["t"], unit="s", utc=True))
                    df.index = df.index.tz_localize(None)
                    return df.sort_index().tail(count)
                except Exception as e:
                    logger.warning(f"Finnhub candle parse error: {e}")

        logger.warning(f"candles fetch failed for {symbol}")
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
            "balance":     balance,
            "equity":      balance + open_profit,
            "margin":      0.0,
            "free_margin": balance + open_profit,
            "profit":      open_profit,
        }

    def get_positions(self) -> list:
        for pos in self._positions:
            tick = self.get_tick(pos["symbol"])
            if tick:
                pair     = _pair_settings(pos["symbol"])
                pip_size = pair['pip_size']
                pip_val  = pair['pip_value']
                if pos["type"] == "BUY":
                    pips = (tick["bid"] - pos["price_open"]) / pip_size
                else:
                    pips = (pos["price_open"] - tick["ask"]) / pip_size
                pos["unrealized_profit"] = round(pips * pip_val * pos["volume"], 2)
        return list(self._positions)

    def open_position(self, symbol: str, direction: str, volume: float,
                      entry: float, sl: float, tp: float) -> dict:
        ticket = self._next_ticket
        self._next_ticket += 1
        pos = {
            "ticket": ticket, "symbol": symbol, "type": direction,
            "volume": volume, "price_open": entry, "sl": sl, "tp": tp,
            "profit": 0.0, "unrealized_profit": 0.0, "time": datetime.utcnow(),
        }
        self._positions.append(pos)
        logger.info(
            f"Paper position opened | {symbol} {direction} {volume} lots "
            f"@ {entry} SL={sl} TP={tp}"
        )
        return pos

    def close_position(self, ticket: int, close_price: float, reason: str = "manual") -> Optional[dict]:
        for i, pos in enumerate(self._positions):
            if pos["ticket"] == ticket:
                pair     = _pair_settings(pos["symbol"])
                pip_size = pair['pip_size']
                pip_val  = pair['pip_value']
                if pos["type"] == "BUY":
                    pips = (close_price - pos["price_open"]) / pip_size
                else:
                    pips = (pos["price_open"] - close_price) / pip_size
                pnl = round(pips * pip_val * pos["volume"], 2)
                pos["profit"] = pnl
                pos["close_price"] = close_price
                pos["close_time"]  = datetime.utcnow()
                pos["close_reason"] = reason
                self._positions.pop(i)
                logger.info(
                    f"Paper position closed | {pos['symbol']} ticket={ticket} "
                    f"pnl=${pnl:.2f} reason={reason}"
                )
                return pos
        return None

    def update_sl(self, ticket: int, new_sl: float) -> bool:
        for pos in self._positions:
            if pos["ticket"] == ticket:
                pos["sl"] = new_sl
                if self.db:
                    self.db.update_sl(ticket, new_sl, pos["symbol"])
                return True
        return False

    def connect(self) -> bool:
        source = "Finnhub" if self._api_key else "yfinance (no Finnhub key)"
        logger.info(f"PaperFeed initialized | price source: {source}")
        if self.db and self.db.conn:
            open_positions = self.db.get_open_positions()
            if open_positions:
                self._positions = open_positions
                max_ticket = max(p["ticket"] for p in open_positions if p.get("ticket"))
                self._next_ticket = max_ticket + 1
                logger.info(f"Reloaded {len(open_positions)} open position(s) from DB")
        return True

    def disconnect(self):
        pass
