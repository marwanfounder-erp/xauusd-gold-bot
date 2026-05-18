import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None


class TradeExecutor:
    def __init__(self, config, feed, db=None):
        self.config = config
        self.feed = feed
        self.db = db
        self.paper_mode = not hasattr(feed, "__class__") or feed.__class__.__name__ == "PaperDataFeed"

    def execute_signal(self, signal: dict, lot_size: float) -> Optional[dict]:
        if signal.get("direction") == "NONE":
            return None
        if self.paper_mode:
            return self._paper_execute(signal, lot_size)
        return self._mt5_execute(signal, lot_size)

    # ── Paper execution ───────────────────────────────────────────────────────

    def _paper_execute(self, signal: dict, lot_size: float) -> Optional[dict]:
        symbol = signal.get("symbol", self.config.symbol)
        result = self.feed.open_position(
            symbol=symbol,
            direction=signal["direction"],
            volume=lot_size,
            entry=signal["entry"],
            sl=signal["stop_loss"],
            tp=signal["take_profit"],
        )
        if result and self.db:
            try:
                self.db.save_trade({
                    "symbol":      symbol,
                    "direction":   signal["direction"],
                    "lot_size":    lot_size,
                    "entry_price": signal["entry"],
                    "stop_loss":   signal["stop_loss"],
                    "take_profit": signal["take_profit"],
                    "sl_dollars":  signal.get("sl_dollars"),
                    "rsi":         signal.get("rsi"),
                    "trend":       signal.get("trend"),
                    "session":     signal.get("session"),
                    "confidence":  signal.get("confidence"),
                    "status":      "open",
                    "open_time":   datetime.utcnow(),
                    "ticket":      result.get("ticket"),
                    "mode":        "paper",
                })
            except Exception as e:
                logger.error(f"DB save trade error: {e}")
        return result

    # ── MT5 live execution ────────────────────────────────────────────────────

    def _mt5_execute(self, signal: dict, lot_size: float) -> Optional[dict]:
        if mt5 is None:
            logger.error("MT5 not available")
            return None

        symbol    = signal.get("symbol", self.config.symbol)
        direction = signal["direction"]
        order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL

        filling_map = {
            0: mt5.ORDER_FILLING_FOK,
            1: mt5.ORDER_FILLING_IOC,
            2: mt5.ORDER_FILLING_RETURN,
        }
        filling = filling_map.get(self.config.order_filling_mode, mt5.ORDER_FILLING_FOK)

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       lot_size,
            "type":         order_type,
            "price":        signal["entry"],
            "sl":           signal["stop_loss"],
            "tp":           signal["take_profit"],
            "deviation":    10,
            "magic":        self.config.order_magic_id,
            "comment":      f"MultiPair-{symbol}-{signal.get('session','?')}",
            "type_filling": filling,
        }

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            logger.error(f"MT5 order failed | {symbol} retcode={code}")
            return None

        trade = {
            "ticket":     result.order,
            "symbol":     symbol,
            "type":       direction,
            "volume":     lot_size,
            "price_open": result.price,
            "sl":         signal["stop_loss"],
            "tp":         signal["take_profit"],
            "time":       datetime.utcnow(),
        }

        if self.db:
            try:
                self.db.save_trade({
                    "symbol":      symbol,
                    "direction":   direction,
                    "lot_size":    lot_size,
                    "entry_price": result.price,
                    "stop_loss":   signal["stop_loss"],
                    "take_profit": signal["take_profit"],
                    "sl_dollars":  signal.get("sl_dollars"),
                    "rsi":         signal.get("rsi"),
                    "trend":       signal.get("trend"),
                    "session":     signal.get("session"),
                    "confidence":  signal.get("confidence"),
                    "status":      "open",
                    "open_time":   datetime.utcnow(),
                    "ticket":      result.order,
                    "mode":        "live",
                })
            except Exception as e:
                logger.error(f"DB save trade error: {e}")

        return trade

    def close_position_mt5(self, position: dict, reason: str = "manual") -> bool:
        if mt5 is None:
            return False
        symbol = position.get("symbol", self.config.symbol)
        tick   = self.feed.get_tick(symbol)
        if not tick:
            return False

        direction   = position["type"]
        close_price = tick["bid"] if direction == "BUY" else tick["ask"]
        close_type  = mt5.ORDER_TYPE_SELL if direction == "BUY" else mt5.ORDER_TYPE_BUY

        request = {
            "action":       mt5.TRADE_ACTION_DEAL,
            "symbol":       symbol,
            "volume":       position["volume"],
            "type":         close_type,
            "position":     position["ticket"],
            "price":        close_price,
            "deviation":    10,
            "magic":        self.config.order_magic_id,
            "comment":      f"close_{reason}",
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            code = result.retcode if result else "None"
            logger.error(f"MT5 close failed | {symbol} retcode={code}")
            return False
        logger.info(f"MT5 position closed | {symbol} ticket={position['ticket']} reason={reason}")
        return True

    def modify_sl_mt5(self, ticket: int, new_sl: float) -> bool:
        if mt5 is None:
            return False
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": ticket,
            "sl":       new_sl,
        }
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
