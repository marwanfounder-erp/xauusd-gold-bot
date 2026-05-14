import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

MONTHLY_LOSS_LIMIT = 0.03   # halt if monthly loss exceeds 3% of month-start balance


class RiskManager:
    def __init__(self, config, feed, db=None, notifier=None):
        self.config   = config
        self.feed     = feed
        self.db       = db
        self.notifier = notifier   # optional: object with send_alert(str) or send_message(str)

        self._daily_loss          = 0.0
        self._daily_reset_date: Optional[date] = None
        self._starting_balance: Optional[float] = None

        # Monthly circuit breaker
        self._monthly_loss         = 0.0
        self._monthly_halted       = False
        self._cb_month_key: str    = ""   # "YYYY-MM" of the current tracked month
        self._monthly_start_bal: Optional[float] = None

        # Tracks which tickets have already had their 50% partial close executed.
        # Keyed by ticket id; value = True once partial close is done.
        # Resets on process restart — persisting this to DB is a future enhancement.
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

    # ── Lot size (Gold-specific) ──────────────────────────────────────────────

    def calculate_lot_size(self, symbol: str, sl_dollars: float) -> float:
        """
        Gold lot sizing:
          pip = $0.01, pip_value = $1 per lot per pip
          lot = risk_amount / (sl_pips * pip_value)
        """
        try:
            balance     = self.get_equity()
            risk_amount = balance * self.config.risk_per_trade
            sl_pips     = sl_dollars / self.config.pip_size
            pip_value   = self.config.pip_value  # $1 per pip per lot

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
        # Compute next month label for the message
        if now.month == 12:
            next_month = f"{now.year + 1}-01"
        else:
            next_month = f"{now.year}-{now.month + 1:02d}"
        msg = (
            f"⚠️ XAUUSD Bot — Monthly Loss Limit Hit\n"
            f"Monthly loss exceeded {MONTHLY_LOSS_LIMIT:.0%} of balance.\n"
            f"Trading halted until {next_month}."
        )
        try:
            if hasattr(self.notifier, "send_alert"):
                self.notifier.send_alert(msg)
            elif hasattr(self.notifier, "send_message"):
                self.notifier.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send monthly halt Telegram alert: {e}")

    # ─────────────────────────────────────────────────────────────────────────

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
        entry     = position["price_open"]
        tp        = position["tp"]
        direction = position["type"]
        current_sl = position["sl"]

        trigger_ratio = self.config.breakeven_trigger_ratio
        tp_distance   = abs(tp - entry)

        if direction == "BUY":
            trigger_price = entry + (tp_distance * trigger_ratio)
            tick = self.feed.get_tick(position["symbol"])
            if tick and tick["bid"] >= trigger_price:
                new_sl = entry + self.config.pip_size
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

    # ── Partial TP monitor ────────────────────────────────────────────────────

    def monitor_positions(self) -> list[dict]:
        """
        Check open positions for partial TP events and return a list of actions
        for the main loop to execute. Does NOT call feed methods directly so it
        remains compatible with any broker adapter.

        Each returned action dict:
          action="partial_close" → close lots_to_close, then modify_position(new_sl, new_tp)
          action="full_tp"       → position closed naturally; clean up internal state

        DB columns to record on partial close (requires database.py update):
          partial_closed (bool), partial_close_price (float)

        Integration in main loop:
          for action in risk_manager.monitor_positions():
              if action["action"] == "partial_close":
                  feed.close_partial(action["ticket"], action["lots_to_close"])
                  feed.modify_position(action["ticket"],
                                       sl=action["new_sl"], tp=action["new_tp"])
        """
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

            if tp1 is None:
                continue

            tick = self.feed.get_tick(pos.get("symbol", ""))
            if not tick:
                continue

            partial_done = self._partial_closed.get(ticket, False)

            if not partial_done:
                hit_tp1 = (direction == "BUY"  and tick["bid"] >= tp1) or \
                          (direction == "SELL" and tick["ask"] <= tp1)

                if hit_tp1:
                    half_lot = round(lot / 2, 2)
                    be_sl    = round(
                        entry + self.config.pip_size if direction == "BUY"
                        else entry - self.config.pip_size, 2
                    )
                    self._partial_closed[ticket] = True
                    logger.info(
                        f"Partial TP hit — moved SL to breakeven | "
                        f"ticket={ticket} dir={direction} tp1={tp1:.2f} "
                        f"lots_to_close={half_lot} new_sl={be_sl:.2f}"
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
                # Second half: check if tp2 was hit (position may already be closed
                # by the broker — clean up our tracking state if ticket disappears)
                hit_tp2 = (direction == "BUY"  and tick["bid"] >= tp2) or \
                          (direction == "SELL" and tick["ask"] <= tp2)
                if hit_tp2:
                    self._partial_closed.pop(ticket, None)
                    logger.info(
                        f"Full TP hit | ticket={ticket} dir={direction} tp2={tp2:.2f}"
                    )
                    actions.append({
                        "action":      "full_tp",
                        "ticket":      ticket,
                        "close_price": tp2,
                    })

        # Clean up tracking for tickets that are no longer open
        open_tickets = {pos.get("ticket") or pos.get("id") for pos in positions}
        stale = [t for t in self._partial_closed if t not in open_tickets]
        for t in stale:
            self._partial_closed.pop(t, None)

        return actions
