import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class XAUUSDStrategy:
    def __init__(self, config, feed):
        self.config = config
        self.feed = feed

    # ── Session helpers ──────────────────────────────────────────────────────

    def is_london_session(self) -> bool:
        h = datetime.utcnow().hour
        return self.config.london_session_start <= h < self.config.london_session_end

    def is_ny_session(self) -> bool:
        h = datetime.utcnow().hour
        return self.config.ny_session_start <= h < self.config.ny_session_end

    def is_market_open(self) -> bool:
        now = datetime.utcnow()
        # Saturday: closed
        if now.weekday() == 5:
            return False
        # Sunday before 21:00 UTC: closed
        if now.weekday() == 6 and now.hour < 21:
            return False
        # Friday after configured close hour: closed
        if now.weekday() == 4 and now.hour >= self.config.friday_close_hour_utc:
            return False
        return True

    # ── Indicators ───────────────────────────────────────────────────────────

    def calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = avg_loss.where(avg_loss != 0, 0.0001)
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 2)

    def get_trend_direction(self, symbol: str) -> str:
        df = self.feed.get_candles(symbol, "1d", 30)
        if df is None or len(df) < self.config.ema_period:
            return "neutral"
        ema = df["close"].ewm(span=self.config.ema_period).mean()
        price = float(df["close"].iloc[-1])
        ema_val = float(ema.iloc[-1])
        buffer = ema_val * 0.001
        if price > ema_val + buffer:
            return "bullish"
        elif price < ema_val - buffer:
            return "bearish"
        return "neutral"

    # ── Range calculators ────────────────────────────────────────────────────

    def calculate_asian_range(self, df: pd.DataFrame) -> Optional[dict]:
        """Asian session (00:00–07:00 UTC) high/low from H1 data."""
        asian = df[df.index.hour < self.config.asian_session_end]
        if len(asian) < 3:
            return None
        high = float(asian["high"].max())
        low = float(asian["low"].min())
        range_dollars = high - low
        return {
            "high": high,
            "low": low,
            "range_dollars": round(range_dollars, 2),
            "valid": self.config.min_range_dollars <= range_dollars <= self.config.max_range_dollars,
        }

    def calculate_london_range(self, df: pd.DataFrame) -> Optional[dict]:
        """London session (07:00–10:00 UTC) high/low for NY breakout."""
        mask = (df.index.hour >= self.config.london_session_start) & \
               (df.index.hour < self.config.london_session_end)
        london = df[mask]
        if len(london) < 1:
            return None
        return {
            "high": float(london["high"].max()),
            "low": float(london["low"].min()),
        }

    # ── Main signal ──────────────────────────────────────────────────────────

    def get_signal(self, symbol: str) -> dict:
        if not self.is_market_open():
            return {"direction": "NONE", "reason": "market_closed"}

        df = self.feed.get_candles(symbol, "1h", 50)
        if df is None or len(df) < 20:
            return {"direction": "NONE", "reason": "insufficient_data"}

        tick = self.feed.get_tick(symbol)
        if not tick:
            return {"direction": "NONE", "reason": "no_tick"}

        current_price = tick["ask"]
        rsi = self.calculate_rsi(df)
        trend = self.get_trend_direction(symbol)
        buf = self.config.breakout_buffer_dollars

        # ── LONDON SESSION ────────────────────────────────────────────────────
        if self.is_london_session():
            asian_range = self.calculate_asian_range(df)

            if not asian_range:
                return {"direction": "NONE", "reason": "no_asian_range"}

            range_val = asian_range["range_dollars"]
            logger.info(
                f"Gold Asian range | high=${asian_range['high']:.2f} "
                f"low=${asian_range['low']:.2f} range=${range_val:.2f} "
                f"valid={asian_range['valid']}"
            )

            if not asian_range["valid"]:
                return {"direction": "NONE", "reason": f"invalid_asian_range_${range_val:.2f}"}

            buy_level = asian_range["high"] + buf
            sell_level = asian_range["low"] - buf

            if current_price > buy_level and rsi > self.config.rsi_buy_threshold and trend != "bearish":
                sl = asian_range["low"] - buf
                sl_distance = current_price - sl
                tp = current_price + (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="BUY",
                    entry=current_price,
                    sl=sl,
                    tp=tp,
                    rsi=rsi,
                    trend=trend,
                    session="LONDON",
                    extra={"range_dollars": range_val},
                )

            if current_price < sell_level and rsi < self.config.rsi_sell_threshold and trend != "bullish":
                sl = asian_range["high"] + buf
                sl_distance = sl - current_price
                tp = current_price - (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="SELL",
                    entry=current_price,
                    sl=sl,
                    tp=tp,
                    rsi=rsi,
                    trend=trend,
                    session="LONDON",
                    extra={"range_dollars": range_val},
                )

        # ── NY SESSION ────────────────────────────────────────────────────────
        elif self.is_ny_session():
            london_range = self.calculate_london_range(df)

            if not london_range:
                return {"direction": "NONE", "reason": "no_london_range"}

            ny_buy_level = london_range["high"] + buf
            ny_sell_level = london_range["low"] - buf

            if current_price > ny_buy_level and rsi > self.config.rsi_buy_threshold and trend != "bearish":
                sl = london_range["low"] - buf
                sl_distance = current_price - sl
                tp = current_price + (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="BUY",
                    entry=current_price,
                    sl=sl,
                    tp=tp,
                    rsi=rsi,
                    trend=trend,
                    session="NY",
                    extra={},
                )

            if current_price < ny_sell_level and rsi < self.config.rsi_sell_threshold and trend != "bullish":
                sl = london_range["high"] + buf
                sl_distance = sl - current_price
                tp = current_price - (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="SELL",
                    entry=current_price,
                    sl=sl,
                    tp=tp,
                    rsi=rsi,
                    trend=trend,
                    session="NY",
                    extra={},
                )

        return {"direction": "NONE", "reason": "no_signal"}

    def _build_signal(self, direction: str, entry: float, sl: float, tp: float,
                      rsi: float, trend: str, session: str, extra: dict) -> dict:
        if direction == "BUY":
            sl_distance = entry - sl
            confidence = "high" if rsi > 65 else "medium"
        else:
            sl_distance = sl - entry
            confidence = "high" if rsi < 35 else "medium"

        sl_pips = sl_distance / self.config.pip_size

        signal = {
            "direction": direction,
            "entry": round(entry, 2),
            "stop_loss": round(sl, 2),
            "take_profit": round(tp, 2),
            "sl_pips": round(sl_pips, 1),
            "sl_dollars": round(sl_distance, 2),
            "rsi": rsi,
            "trend": trend,
            "session": session,
            "confidence": confidence,
        }
        signal.update(extra)
        return signal
