import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

MONTHLY_LOSS_LIMIT = 0.04   # halt if monthly loss exceeds 4% of month-start balance


class RiskManager:
    def __init__(self, config, feed, db=None, notifier=None):
        self.config   = config
        self.feed     = feed
        self.db       = db
        self.notifier = notifier

        self._daily_loss          = 0.0
        self._daily_reset_date: Optional[date] = None
        self._starting_balance: Optional[float] = None

        self._monthly_loss         = 0.0
        self._monthly_halted       = False
        self._cb_month_key: str    = ""
        self._monthly_start_bal: Optional[float] = None

        self._partial_closed: dict = {}

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

    # ── Lot size (multi-pair) ─────────────────────────────────────────────────

    def calculate_lot_size(self, symbol: str, sl_pips: float) -> float:
        """
        Universal lot sizing using per-pair pip_value.
          lot = risk_amount / (sl_pips × pip_value)
        """
        try:
            from config import PAIR_SETTINGS
            pair      = PAIR_SETTINGS.get(symbol, {})
            pip_value = pair.get('pip_value', 10.0)

            balance     = self.get_equity()
            risk_amount = balance * self.config.risk_per_trade

            if sl_pips <= 0:
                logger.warning(f"{symbol} SL pips is zero/negative — defaulting to 0.01 lot")
                return 0.01

            lot_size = risk_amount / (sl_pips * pip_value)
            lot_size = round(lot_size, 2)
            lot_size = max(0.01, min(lot_size, self.config.max_lot_size))

            logger.info(
                f"{symbol} lot size | balance=${balance:.2f} risk=${risk_amount:.2f} "
                f"sl_pips={sl_pips:.1f} pip_val=${pip_value} lots={lot_size}"
            )
            return lot_size

        except Exception as e:
            logger.error(f"Lot size calc error ({symbol}): {e}")
            return 0.01

    # ── Daily loss tracking ───────────────────────────────────────────────────

    def _reset_daily_if_needed(self):
        today = date.today()
        if self._daily_reset_date != today:
            self._daily_reset_date = today
            self._daily_loss       = 0.0
            if self._starting_balance is None:
                self._starting_balance = self.get_balance()

    # ── Monthly circuit breaker ───────────────────────────────────────────────

    def _reset_monthly_if_needed(self):
        now     = datetime.utcnow()
        cur_key = now.strftime("%Y-%m")
        if cur_key != self._cb_month_key:
            self._cb_month_key      = cur_key
            self._monthly_loss      = 0.0
            self._monthly_halted    = False
            self._monthly_start_bal = self.get_balance()
            logger.info(f"Monthly circuit breaker reset for {cur_key}")

    def is_monthly_halted(self) -> bool:
        self._reset_monthly_if_needed()
        return self._monthly_halted

    def get_monthly_loss_pct(self) -> float:
        self._reset_monthly_if_needed()
        ref = self._monthly_start_bal or self.get_balance()
        return self._monthly_loss / ref if ref > 0 else 0.0

    def _send_monthly_halt_alert(self):
        if self.notifier is None:
            return
        now = datetime.utcnow()
        if now.month == 12:
            next_month = f"{now.year + 1}-01"
        else:
            next_month = f"{now.year}-{now.month + 1:02d}"
        msg = (
            f"⚠️ Multi-Pair Bot — Monthly Loss Limit Hit\n"
            f"Monthly loss exceeded {MONTHLY_LOSS_LIMIT:.0%} of balance.\n"
            f"Trading halted until {next_month}."
        )
        try:
            if hasattr(self.notifier, "send_alert"):
                self.notifier.send_alert(msg)
            elif hasattr(self.notifier, "send_message"):
                self.notifier.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send monthly halt alert: {e}")

    def record_trade_result(self, pnl: float):
        self._reset_daily_if_needed()
        self._reset_monthly_if_needed()
        if pnl < 0:
            self._daily_loss   += abs(pnl)
            self._monthly_loss += abs(pnl)
            limit = (self._monthly_start_bal or self.get_balance()) * MONTHLY_LOSS_LIMIT
            if not self._monthly_halted and self._monthly_loss >= limit:
                self._monthly_halted = True
                logger.warning(
                    f"Monthly loss limit hit — halting until next month "
                    f"(loss={self._monthly_loss:.2f}, limit={limit:.2f})"
                )
                self._send_monthly_halt_alert()

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
        self._reset_monthly_if_needed()

        if self._monthly_halted:
            return False, f"monthly_loss_limit_hit ({self.get_monthly_loss_pct():.1%})"

        positions = self.feed.get_positions()
        if len(positions) >= self.config.max_total_positions:
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
        entry      = position["price_open"]
        tp         = position["tp"]
        direction  = position["type"]
        current_sl = position["sl"]
        symbol     = position.get("symbol", self.config.symbol)

        from config import PAIR_SETTINGS
        pip_size = PAIR_SETTINGS.get(symbol, {}).get('pip_size', self.config.pip_size)

        trigger_ratio = self.config.breakeven_trigger_ratio
        tp_distance   = abs(tp - entry)

        if direction == "BUY":
            trigger_price = entry + (tp_distance * trigger_ratio)
            tick = self.feed.get_tick(symbol)
            if tick and tick["bid"] >= trigger_price:
                new_sl = entry + pip_size
                if current_sl < new_sl:
                    return new_sl
        else:
            trigger_price = entry - (tp_distance * trigger_ratio)
            tick = self.feed.get_tick(symbol)
            if tick and tick["ask"] <= trigger_price:
                new_sl = entry - pip_size
                if current_sl > new_sl:
                    return new_sl

        return None

    def should_close_by_time(self, position: dict) -> bool:
        open_time = position.get("time")
        if not open_time:
            return False
        age_hours = (datetime.utcnow() - open_time).total_seconds() / 3600
        return age_hours >= self.config.max_trade_hours

    # ── Partial TP monitor ────────────────────────────────────────────────────

    def monitor_positions(self) -> list[dict]:
        actions   = []
        positions = self.feed.get_positions()

        for pos in positions:
            ticket    = pos.get("ticket") or pos.get("id")
            if ticket is None:
                continue

            direction = pos.get("type", "")
            entry     = pos.get("price_open", 0.0)
            tp1       = pos.get("tp1")
            tp2       = pos.get("tp") or pos.get("tp2")
            lot       = pos.get("volume", 0.01)
            symbol    = pos.get("symbol", self.config.symbol)

            if tp1 is None:
                continue

            tick = self.feed.get_tick(symbol)
            if not tick:
                continue

            partial_done = self._partial_closed.get(ticket, False)

            if not partial_done:
                hit_tp1 = (direction == "BUY"  and tick["bid"] >= tp1) or \
                          (direction == "SELL" and tick["ask"] <= tp1)

                if hit_tp1:
                    from config import PAIR_SETTINGS
                    pip_size = PAIR_SETTINGS.get(symbol, {}).get('pip_size', self.config.pip_size)
                    half_lot = round(lot / 2, 2)
                    be_sl    = round(
                        entry + pip_size if direction == "BUY"
                        else entry - pip_size, 5
                    )
                    self._partial_closed[ticket] = True
                    logger.info(
                        f"Partial TP hit | {symbol} ticket={ticket} dir={direction} "
                        f"tp1={tp1} lots_to_close={half_lot} new_sl={be_sl}"
                    )
                    actions.append({
                        "action":        "partial_close",
                        "ticket":        ticket,
                        "lots_to_close": half_lot,
                        "close_price":   tp1,
                        "new_sl":        be_sl,
                        "new_tp":        tp2,
                    })
            else:
                hit_tp2 = (direction == "BUY"  and tick["bid"] >= tp2) or \
                          (direction == "SELL" and tick["ask"] <= tp2)
                if hit_tp2:
                    self._partial_closed.pop(ticket, None)
                    actions.append({"action": "full_tp", "ticket": ticket, "close_price": tp2})

        open_tickets = {pos.get("ticket") or pos.get("id") for pos in positions}
        for t in [k for k in self._partial_closed if k not in open_tickets]:
            self._partial_closed.pop(t, None)

        return actions
