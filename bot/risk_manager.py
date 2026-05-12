import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config, feed, db=None):
        self.config = config
        self.feed = feed
        self.db = db
        self._daily_loss = 0.0
        self._daily_reset_date: Optional[date] = None
        self._starting_balance: Optional[float] = None

    # ── Equity / balance ─────────────────────────────────────────────────────

    def get_equity(self) -> float:
        info = self.feed.get_account_info()
        if info:
            return info.get("equity", info.get("balance", 5000.0))
        return 5000.0

    def get_balance(self) -> float:
        info = self.feed.get_account_info()
        if info:
            return info.get("balance", 5000.0)
        return 5000.0

    # ── Lot size (Gold-specific) ──────────────────────────────────────────────

    def calculate_lot_size(self, symbol: str, sl_dollars: float) -> float:
        """
        Gold lot sizing:
          pip = $0.01, pip_value = $1 per lot per pip
          lot = risk_amount / (sl_pips * pip_value)
        """
        try:
            balance = self.get_equity()
            risk_amount = balance * self.config.risk_per_trade
            sl_pips = sl_dollars / self.config.pip_size
            pip_value = self.config.pip_value  # $1 per pip per lot

            if sl_pips <= 0:
                logger.warning("SL pips is zero/negative — defaulting to 0.01 lot")
                return 0.01

            lot_size = risk_amount / (sl_pips * pip_value)
            lot_size = round(lot_size, 2)
            lot_size = max(0.01, min(lot_size, self.config.max_lot_size))

            logger.info(
                f"Gold lot size | balance=${balance:.2f} risk=${risk_amount:.2f} "
                f"sl=${sl_dollars:.2f} sl_pips={sl_pips:.0f} lots={lot_size}"
            )
            return lot_size

        except Exception as e:
            logger.error(f"Lot size calc error: {e}")
            return 0.01

    # ── Daily loss tracking ───────────────────────────────────────────────────

    def _reset_daily_if_needed(self):
        today = date.today()
        if self._daily_reset_date != today:
            self._daily_reset_date = today
            self._daily_loss = 0.0
            if self._starting_balance is None:
                self._starting_balance = self.get_balance()

    def record_trade_result(self, pnl: float):
        self._reset_daily_if_needed()
        if pnl < 0:
            self._daily_loss += abs(pnl)

    def get_daily_loss_pct(self) -> float:
        self._reset_daily_if_needed()
        balance = self.get_balance()
        if balance <= 0:
            return 0.0
        return self._daily_loss / balance

    # ── Drawdown ──────────────────────────────────────────────────────────────

    def get_total_drawdown_pct(self) -> float:
        if self._starting_balance is None:
            self._starting_balance = self.get_balance()
        current = self.get_equity()
        if self._starting_balance <= 0:
            return 0.0
        return max(0.0, (self._starting_balance - current) / self._starting_balance)

    # ── Guards ────────────────────────────────────────────────────────────────

    def can_trade(self) -> tuple[bool, str]:
        self._reset_daily_if_needed()

        positions = self.feed.get_positions()
        if len(positions) >= self.config.max_open_positions:
            return False, f"max_positions_reached ({len(positions)})"

        daily_pct = self.get_daily_loss_pct()
        if daily_pct >= self.config.max_daily_loss:
            return False, f"daily_loss_limit_hit ({daily_pct:.1%})"

        dd_pct = self.get_total_drawdown_pct()
        if dd_pct >= self.config.max_total_drawdown:
            return False, f"max_drawdown_hit ({dd_pct:.1%})"

        return True, "ok"

    def should_close_friday(self) -> bool:
        now = datetime.utcnow()
        return now.weekday() == 4 and now.hour >= self.config.friday_close_hour_utc

    def check_breakeven(self, position: dict) -> Optional[float]:
        """Return new SL price if position should be moved to breakeven, else None."""
        entry = position["price_open"]
        tp = position["tp"]
        direction = position["type"]
        current_sl = position["sl"]

        trigger_ratio = self.config.breakeven_trigger_ratio
        tp_distance = abs(tp - entry)

        if direction == "BUY":
            trigger_price = entry + (tp_distance * trigger_ratio)
            tick = self.feed.get_tick(position["symbol"])
            if tick and tick["bid"] >= trigger_price:
                new_sl = entry + self.config.pip_size  # 1 pip above entry
                if current_sl < new_sl:
                    return new_sl
        else:
            trigger_price = entry - (tp_distance * trigger_ratio)
            tick = self.feed.get_tick(position["symbol"])
            if tick and tick["ask"] <= trigger_price:
                new_sl = entry - self.config.pip_size
                if current_sl > new_sl:
                    return new_sl

        return None

    def should_close_by_time(self, position: dict) -> bool:
        open_time = position.get("time")
        if not open_time:
            return False
        age_hours = (datetime.utcnow() - open_time).total_seconds() / 3600
        return age_hours >= self.config.max_trade_hours
