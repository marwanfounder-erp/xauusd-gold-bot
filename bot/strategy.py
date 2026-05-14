import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

RSI_BUY  = 65.0
RSI_SELL = 35.0


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
        if now.weekday() == 5:
            return False
        if now.weekday() == 6 and now.hour < 21:
            return False
        if now.weekday() == 4 and now.hour >= self.config.friday_close_hour_utc:
            return False
        return True

    # ── Indicators ───────────────────────────────────────────────────────────

    def calculate_rsi(self, df: pd.DataFrame, period: int = 14) -> float:
        delta    = df["close"].diff()
        gain     = delta.where(delta > 0, 0.0)
        loss     = -delta.where(delta < 0, 0.0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = avg_loss.where(avg_loss != 0, 0.0001)
        rs       = avg_gain / avg_loss
        rsi      = 100 - (100 / (1 + rs))
        return round(float(rsi.iloc[-1]), 2)

    def calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Average True Range — logged on every signal check for volatility context."""
        high  = df["high"]
        low   = df["low"]
        close = df["close"].shift(1)
        tr    = pd.concat(
            [high - low, (high - close).abs(), (low - close).abs()], axis=1
        ).max(axis=1)
        atr = tr.ewm(span=period, min_periods=period).mean()
        return round(float(atr.iloc[-1]), 2)

    def get_trend_direction(self, symbol: str) -> str:
        """
        Multi-timeframe trend filter.
        Both H4 and Daily EMA20 must agree for a non-neutral result.
        """
        # Daily EMA
        df_daily = self.feed.get_candles(symbol, "1d", 30)
        if df_daily is None or len(df_daily) < self.config.ema_period:
            logger.info("Trend filter | daily=insufficient_data h4=skipped → neutral")
            return "neutral"
        ema_daily  = df_daily["close"].ewm(span=self.config.ema_period).mean()
        price_d    = float(df_daily["close"].iloc[-1])
        ema_d_val  = float(ema_daily.iloc[-1])
        if price_d > ema_d_val * 1.001:
            daily_trend = "bullish"
        elif price_d < ema_d_val * 0.999:
            daily_trend = "bearish"
        else:
            daily_trend = "neutral"

        # H4 EMA
        try:
            df_h4 = self.feed.get_candles(symbol, "4h", 60)
        except Exception:
            df_h4 = None
        if df_h4 is None or len(df_h4) < self.config.ema_period:
            logger.info(f"Trend filter | daily={daily_trend} h4=unavailable → neutral")
            return "neutral"
        ema_h4     = df_h4["close"].ewm(span=self.config.ema_period).mean()
        price_h4   = float(df_h4["close"].iloc[-1])
        ema_h4_val = float(ema_h4.iloc[-1])
        if price_h4 > ema_h4_val * 1.001:
            h4_trend = "bullish"
        elif price_h4 < ema_h4_val * 0.999:
            h4_trend = "bearish"
        else:
            h4_trend = "neutral"

        logger.info(f"Trend filter | daily={daily_trend} h4={h4_trend}")

        if daily_trend == "bullish" and h4_trend == "bullish":
            return "bullish"
        if daily_trend == "bearish" and h4_trend == "bearish":
            return "bearish"
        return "neutral"

    # ── Range calculators ────────────────────────────────────────────────────

    def calculate_asian_range(self, df: pd.DataFrame) -> Optional[dict]:
        """Asian session (00:00–07:00 UTC) high/low from H1 data."""
        asian = df[df.index.hour < self.config.asian_session_end]
        if len(asian) < 3:
            return None
        high          = float(asian["high"].max())
        low           = float(asian["low"].min())
        range_dollars = high - low
        return {
            "high":          high,
            "low":           low,
            "range_dollars": round(range_dollars, 2),
            "valid":         self.config.min_range_dollars <= range_dollars <= self.config.max_range_dollars,
        }

    def calculate_london_range(self, df: pd.DataFrame) -> Optional[dict]:
        """London session (07:00–10:00 UTC) high/low for NY breakout reference."""
        mask   = (df.index.hour >= self.config.london_session_start) & \
                 (df.index.hour < self.config.london_session_end)
        london = df[mask]
        if len(london) < 1:
            return None
        return {
            "high": float(london["high"].max()),
            "low":  float(london["low"].min()),
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
        rsi   = self.calculate_rsi(df)
        atr   = self.calculate_atr(df)
        trend = self.get_trend_direction(symbol)
        buf   = self.config.breakout_buffer_dollars  # fixed $0.20

        logger.info(
            f"Signal check | price={current_price:.2f} rsi={rsi} "
            f"atr={atr:.2f} trend={trend} buf=${buf:.2f}"
        )

        # ── LONDON SESSION (07:00–10:00 UTC) — Asian range breakout ──────────────
        if self.is_london_session():
            asian_range = self.calculate_asian_range(df)
            if asian_range:
                logger.info(
                    f"Gold Asian range | high=${asian_range['high']:.2f} "
                    f"low=${asian_range['low']:.2f} range=${asian_range['range_dollars']:.2f} "
                    f"valid={asian_range['valid']}"
                )

            logger.info(
                f"Session: LONDON | price={current_price:.2f} rsi={rsi} trend={trend}"
            )

            if not asian_range or not asian_range["valid"]:
                logger.info("Signal: NONE | reason=invalid_asian_range | session=LONDON")
                return {"direction": "NONE", "reason": "invalid_asian_range"}

            buy_level  = asian_range["high"] + buf
            sell_level = asian_range["low"]  - buf

            if current_price > buy_level and rsi > RSI_BUY and trend == "bullish":
                sl          = asian_range["low"] - buf
                sl_distance = current_price - sl
                tp          = current_price + (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="BUY", entry=current_price, sl=sl, tp=tp,
                    rsi=rsi, trend=trend, session="LONDON",
                )

            if current_price < sell_level and rsi < RSI_SELL and trend == "bearish":
                sl          = asian_range["high"] + buf
                sl_distance = sl - current_price
                tp          = current_price - (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="SELL", entry=current_price, sl=sl, tp=tp,
                    rsi=rsi, trend=trend, session="LONDON",
                )

            logger.info("Signal: NONE | reason=no_signal | session=LONDON")
            return {"direction": "NONE", "reason": "no_signal"}

        # ── NY SESSION — disabled (46.4% WR, adding DD without return) ────────
        elif self.is_ny_session():
            return {"direction": "NONE", "reason": "ny_disabled"}

        return {"direction": "NONE", "reason": "no_signal"}

    def _build_signal(self, direction: str, entry: float, sl: float, tp: float,
                      rsi: float, trend: str, session: str) -> dict:
        if direction == "BUY":
            sl_distance = entry - sl
            confidence  = "high" if rsi > RSI_BUY else "medium"
        else:
            sl_distance = sl - entry
            confidence  = "high" if rsi < RSI_SELL else "medium"

        sl_pips = sl_distance / self.config.pip_size

        return {
            "direction":   direction,
            "entry":       round(entry, 2),
            "stop_loss":   round(sl, 2),
            "take_profit": round(tp, 2),
            "sl_pips":     round(sl_pips, 1),
            "sl_dollars":  round(sl_distance, 2),
            "rsi":         rsi,
            "trend":       trend,
            "session":     session,
            "confidence":  confidence,
        }
