import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


class XAUUSDStrategy:
    """London + NY Breakout Strategy — works for XAUUSD, GBPUSD, USDJPY."""

    def __init__(self, config, feed):
        self.config = config
        self.feed = feed

    # ── Pair helpers ─────────────────────────────────────────────────────────

    def get_pair_settings(self, symbol: str) -> dict:
        from config import PAIR_SETTINGS
        return PAIR_SETTINGS.get(symbol, {
            'pip_size': 0.01, 'pip_value': 1.0,
            'min_range': 300, 'max_range': 1500, 'buffer': 20,
            'yahoo_symbol': symbol, 'spread': 30,
        })

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
        high  = df["high"]
        low   = df["low"]
        close = df["close"].shift(1)
        tr    = pd.concat(
            [high - low, (high - close).abs(), (low - close).abs()], axis=1
        ).max(axis=1)
        atr = tr.ewm(span=period, min_periods=period).mean()
        return round(float(atr.iloc[-1]), 4)

    def get_trend_direction(self, symbol: str) -> str:
        df_daily = self.feed.get_candles(symbol, "1d", 30)
        if df_daily is None or len(df_daily) < self.config.ema_period:
            logger.info(f"{symbol} trend filter | daily=insufficient_data → neutral")
            return "neutral"
        ema_daily = df_daily["close"].ewm(span=self.config.ema_period).mean()
        price_d   = float(df_daily["close"].iloc[-1])
        ema_d_val = float(ema_daily.iloc[-1])
        if price_d > ema_d_val * 1.001:
            daily_trend = "bullish"
        elif price_d < ema_d_val * 0.999:
            daily_trend = "bearish"
        else:
            daily_trend = "neutral"

        try:
            df_h4 = self.feed.get_candles(symbol, "4h", 60)
        except Exception:
            df_h4 = None
        if df_h4 is None or len(df_h4) < self.config.ema_period:
            logger.info(f"{symbol} trend filter | daily={daily_trend} h4=unavailable → neutral")
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

        logger.info(f"{symbol} trend filter | daily={daily_trend} h4={h4_trend}")

        if daily_trend == "bullish" and h4_trend == "bullish":
            return "bullish"
        if daily_trend == "bearish" and h4_trend == "bearish":
            return "bearish"
        return "neutral"

    # ── Range calculators ────────────────────────────────────────────────────

    def calculate_asian_range(self, df: pd.DataFrame, symbol: str) -> Optional[dict]:
        pair     = self.get_pair_settings(symbol)
        pip_size = pair['pip_size']
        asian    = df[df.index.hour < self.config.asian_session_end]
        if len(asian) < 3:
            return None
        high       = float(asian["high"].max())
        low        = float(asian["low"].min())
        range_pips = (high - low) / pip_size
        return {
            "high":       high,
            "low":        low,
            "range_pips": round(range_pips, 1),
            "valid":      pair['min_range'] <= range_pips <= pair['max_range'],
            "pip_size":   pip_size,
        }

    def calculate_london_range(self, df: pd.DataFrame) -> Optional[dict]:
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
            return {"direction": "NONE", "reason": "market_closed", "symbol": symbol}

        df = self.feed.get_candles(symbol, "1h", 50)
        if df is None or len(df) < 20:
            return {"direction": "NONE", "reason": "insufficient_data", "symbol": symbol}

        tick = self.feed.get_tick(symbol)
        if not tick:
            return {"direction": "NONE", "reason": "no_tick", "symbol": symbol}

        pair     = self.get_pair_settings(symbol)
        pip_size = pair['pip_size']
        buf      = pair['buffer'] * pip_size   # buffer in price units

        current_price = tick["ask"]
        rsi           = self.calculate_rsi(df)
        atr           = self.calculate_atr(df)
        trend         = self.get_trend_direction(symbol)

        logger.info(
            f"{symbol} signal check | price={current_price:.5f} rsi={rsi} "
            f"atr={atr:.5f} trend={trend} buf={buf:.5f}"
        )

        # ── LONDON SESSION — Asian range breakout ─────────────────────────────
        if self.is_london_session():
            asian_range = self.calculate_asian_range(df, symbol)
            if asian_range:
                logger.info(
                    f"{symbol} Asian range | high={asian_range['high']:.5f} "
                    f"low={asian_range['low']:.5f} range={asian_range['range_pips']:.1f}pips "
                    f"valid={asian_range['valid']}"
                )

            logger.info(f"{symbol} session=LONDON | price={current_price:.5f} rsi={rsi} trend={trend}")

            if not asian_range or not asian_range["valid"]:
                return {"direction": "NONE", "reason": "invalid_asian_range", "symbol": symbol}

            buy_level  = asian_range["high"] + buf
            sell_level = asian_range["low"]  - buf

            if current_price > buy_level and rsi > self.config.rsi_buy_threshold and trend == "bullish":
                sl          = asian_range["low"] - buf
                sl_distance = current_price - sl
                tp          = current_price + (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="BUY", entry=current_price, sl=sl, tp=tp,
                    rsi=rsi, trend=trend, session="LONDON", symbol=symbol,
                )

            if current_price < sell_level and rsi < self.config.rsi_sell_threshold and trend == "bearish":
                sl          = asian_range["high"] + buf
                sl_distance = sl - current_price
                tp          = current_price - (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="SELL", entry=current_price, sl=sl, tp=tp,
                    rsi=rsi, trend=trend, session="LONDON", symbol=symbol,
                )

            return {"direction": "NONE", "reason": "no_signal", "symbol": symbol}

        # ── NY SESSION — London range breakout ────────────────────────────────
        elif self.is_ny_session():
            london_range = self.calculate_london_range(df)
            if not london_range:
                return {"direction": "NONE", "reason": "no_london_range", "symbol": symbol}

            buy_level  = london_range["high"] + buf
            sell_level = london_range["low"]  - buf

            logger.info(
                f"{symbol} session=NY | price={current_price:.5f} rsi={rsi} trend={trend} "
                f"london_high={london_range['high']:.5f} london_low={london_range['low']:.5f}"
            )

            if current_price > buy_level and rsi > self.config.rsi_buy_threshold and trend == "bullish":
                sl          = london_range["low"] - buf
                sl_distance = current_price - sl
                tp          = current_price + (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="BUY", entry=current_price, sl=sl, tp=tp,
                    rsi=rsi, trend=trend, session="NY", symbol=symbol,
                )

            if current_price < sell_level and rsi < self.config.rsi_sell_threshold and trend == "bearish":
                sl          = london_range["high"] + buf
                sl_distance = sl - current_price
                tp          = current_price - (sl_distance * self.config.rr_ratio)
                return self._build_signal(
                    direction="SELL", entry=current_price, sl=sl, tp=tp,
                    rsi=rsi, trend=trend, session="NY", symbol=symbol,
                )

            return {"direction": "NONE", "reason": "no_signal", "symbol": symbol}

        return {"direction": "NONE", "reason": "no_signal", "symbol": symbol}

    def _build_signal(self, direction: str, entry: float, sl: float, tp: float,
                      rsi: float, trend: str, session: str, symbol: str) -> dict:
        pair     = self.get_pair_settings(symbol)
        pip_size = pair['pip_size']

        if direction == "BUY":
            sl_distance = entry - sl
            confidence  = "high" if rsi > self.config.rsi_buy_threshold + 5 else "medium"
        else:
            sl_distance = sl - entry
            confidence  = "high" if rsi < self.config.rsi_sell_threshold - 5 else "medium"

        sl_pips = sl_distance / pip_size
        decimals = 2 if symbol == "XAUUSD" else (3 if "JPY" in symbol else 5)

        logger.info(
            f"{symbol} {direction} signal | session={session} entry={round(entry, decimals)} "
            f"sl={round(sl, decimals)} tp={round(tp, decimals)} "
            f"sl_pips={round(sl_pips, 1)} rsi={rsi} trend={trend}"
        )

        return {
            "direction":   direction,
            "symbol":      symbol,
            "entry":       round(entry, decimals),
            "stop_loss":   round(sl, decimals),
            "take_profit": round(tp, decimals),
            "sl_pips":     round(sl_pips, 1),
            "sl_dollars":  round(sl_distance, decimals),  # kept for backward compat
            "rsi":         rsi,
            "trend":       trend,
            "session":     session,
            "confidence":  confidence,
        }


# Alias for backward compatibility
LondonBreakoutStrategy = XAUUSDStrategy
