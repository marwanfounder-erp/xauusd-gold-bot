import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

MT5_TIMEFRAMES = {}
try:
    import MetaTrader5 as mt5
    MT5_TIMEFRAMES = {
        "1m": mt5.TIMEFRAME_M1,
        "5m": mt5.TIMEFRAME_M5,
        "15m": mt5.TIMEFRAME_M15,
        "30m": mt5.TIMEFRAME_M30,
        "1h": mt5.TIMEFRAME_H1,
        "4h": mt5.TIMEFRAME_H4,
        "1d": mt5.TIMEFRAME_D1,
    }
except ImportError:
    mt5 = None


class MT5DataFeed:
    def __init__(self, config):
        self.config = config

    def connect(self) -> bool:
        if mt5 is None:
            logger.error("MetaTrader5 package not installed")
            return False
        if not mt5.initialize():
            logger.error(f"MT5 initialize failed: {mt5.last_error()}")
            return False
        auth = mt5.login(
            login=self.config.mt5_login,
            password=self.config.mt5_password,
            server=self.config.mt5_server,
        )
        if not auth:
            logger.error(f"MT5 login failed: {mt5.last_error()}")
            return False
        info = mt5.account_info()
        logger.info(f"MT5 connected | account={info.login} balance={info.balance}")
        return True

    def disconnect(self):
        if mt5:
            mt5.shutdown()

    def get_tick(self, symbol: str) -> Optional[dict]:
        if mt5 is None:
            return None
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return None
        return {"bid": tick.bid, "ask": tick.ask, "time": tick.time}

    def get_candles(self, symbol: str, timeframe: str, count: int) -> Optional[pd.DataFrame]:
        if mt5 is None:
            return None
        tf = MT5_TIMEFRAMES.get(timeframe)
        if tf is None:
            logger.error(f"Unknown timeframe: {timeframe}")
            return None
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            return None
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        df.index = df.index.tz_convert("UTC").tz_localize(None)
        return df

    def get_account_info(self) -> Optional[dict]:
        if mt5 is None:
            return None
        info = mt5.account_info()
        if info is None:
            return None
        return {
            "balance": info.balance,
            "equity": info.equity,
            "margin": info.margin,
            "free_margin": info.margin_free,
            "profit": info.profit,
        }

    def get_positions(self) -> list:
        if mt5 is None:
            return []
        positions = mt5.positions_get(symbol=self.config.symbol)
        if positions is None:
            return []
        result = []
        for p in positions:
            if p.magic == self.config.order_magic_id:
                result.append({
                    "ticket": p.ticket,
                    "symbol": p.symbol,
                    "type": "BUY" if p.type == 0 else "SELL",
                    "volume": p.volume,
                    "price_open": p.price_open,
                    "sl": p.sl,
                    "tp": p.tp,
                    "profit": p.profit,
                    "time": datetime.utcfromtimestamp(p.time),
                })
        return result
