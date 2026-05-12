import logging
from datetime import datetime
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, config):
        self.config = config
        self.enabled = bool(config.telegram_bot_token and config.telegram_chat_id)

    def _send(self, text: str) -> bool:
        if not self.enabled:
            logger.debug(f"Telegram disabled. Message: {text[:80]}")
            return False
        try:
            url = self.BASE_URL.format(token=self.config.telegram_bot_token)
            payload = {
                "chat_id": self.config.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
            }
            resp = requests.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_signal(self, signal: dict, lot_size: float, balance: float) -> bool:
        direction = signal["direction"]
        emoji = "🟡 BUY" if direction == "BUY" else "🔴 SELL"
        session = signal.get("session", "?")
        confidence = signal.get("confidence", "?").upper()

        risk_dollars = balance * self.config.risk_per_trade
        range_info = ""
        if "range_dollars" in signal:
            range_info = f"\n📐 Asian Range: <b>${signal['range_dollars']:.2f}</b>"

        text = (
            f"<b>XAUUSD Gold Bot — {emoji}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Session: <b>{session}</b>\n"
            f"Entry:  <b>${signal['entry']:.2f}</b>\n"
            f"SL:     <b>${signal['stop_loss']:.2f}</b>  (${signal['sl_dollars']:.2f})\n"
            f"TP:     <b>${signal['take_profit']:.2f}</b>\n"
            f"Lots:   <b>{lot_size}</b>\n"
            f"RSI:    <b>{signal['rsi']}</b>\n"
            f"Trend:  <b>{signal.get('trend','?').upper()}</b>\n"
            f"Risk:   <b>${risk_dollars:.2f}</b> ({self.config.risk_per_trade*100:.0f}%)\n"
            f"Confidence: <b>{confidence}</b>"
            f"{range_info}\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M UTC')}"
        )
        return self._send(text)

    def send_close(self, position: dict, pnl: float, reason: str) -> bool:
        result = "✅ WIN" if pnl >= 0 else "❌ LOSS"
        text = (
            f"<b>XAUUSD Gold Bot — Trade Closed {result}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Direction: <b>{position.get('type')}</b>\n"
            f"P&L: <b>${pnl:+.2f}</b>\n"
            f"Reason: <b>{reason.upper()}</b>\n"
            f"Lots: <b>{position.get('volume')}</b>\n"
            f"⏰ {datetime.utcnow().strftime('%H:%M UTC')}"
        )
        return self._send(text)

    def send_risk_alert(self, message: str) -> bool:
        text = f"⚠️ <b>XAUUSD Gold Bot — Risk Alert</b>\n{message}"
        return self._send(text)

    def send_startup(self, mode: str, balance: float) -> bool:
        text = (
            f"🟡 <b>XAUUSD Gold Bot Started</b>\n"
            f"Mode: <b>{mode.upper()}</b>\n"
            f"Balance: <b>${balance:.2f}</b>\n"
            f"Symbol: <b>XAUUSD</b>\n"
            f"Strategy: <b>London + NY Breakout</b>\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        return self._send(text)

    def send_daily_report(self, stats: dict) -> bool:
        pnl = stats.get("daily_pnl", 0.0)
        trades = stats.get("daily_trades", 0)
        wins = stats.get("daily_wins", 0)
        wr = (wins / trades * 100) if trades > 0 else 0.0
        dd = stats.get("drawdown_pct", 0.0)

        text = (
            f"📊 <b>XAUUSD Daily Report</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"P&L Today:  <b>${pnl:+.2f}</b>\n"
            f"Trades:     <b>{trades}</b>  ({wins}W / {trades-wins}L)\n"
            f"Win Rate:   <b>{wr:.0f}%</b>\n"
            f"Drawdown:   <b>{dd:.1%}</b>\n"
            f"⏰ {datetime.utcnow().strftime('%Y-%m-%d UTC')}"
        )
        return self._send(text)
