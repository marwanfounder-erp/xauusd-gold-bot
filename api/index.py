"""
Vercel entry point for XAUUSD Gold Bot.
Exposes Flask `app` (required by Vercel Python runtime).
Dashboard + API served here. Bot loop runs in background thread.
State is persisted in Neon PostgreSQL so it survives instance restarts.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import logging
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, jsonify, Response

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs("logs", exist_ok=True)
LOG_BUFFER = deque(maxlen=500)

class BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        BufferHandler(),
    ],
)
logger = logging.getLogger("vercel")

# ── Bot imports ───────────────────────────────────────────────────────────────
from config import settings
from bot.paper_feed import PaperDataFeed
from bot.strategy import XAUUSDStrategy
from bot.risk_manager import RiskManager
from bot.executor import TradeExecutor
from bot.news_filter import NewsFilter
from bot.notifier import TelegramNotifier
from bot.database import Database
from bot.dashboard_server import GOLD_HTML

# ── Shared bot state ──────────────────────────────────────────────────────────
_state: dict = {
    "mode": "paper",
    "balance": 0.0,
    "equity": 0.0,
    "daily_pnl": 0.0,
    "open_positions": 0,
    "price": {},
    "positions": [],
    "trades": [],
    "risk": {},
    "news": [],
    "equity_curve": [],
    "logs": [],
    "running": False,
}

# ── Bot initialisation (once per process) ─────────────────────────────────────
_bot_started = False
_bot_lock = threading.Lock()

db = Database(settings)
feed = PaperDataFeed(settings, db)
strategy = XAUUSDStrategy(settings, feed)
risk = RiskManager(settings, feed, db)
executor = TradeExecutor(settings, feed, db)
news_filter = NewsFilter(settings)
notifier = TelegramNotifier(settings)


def _start_bot():
    global _bot_started
    with _bot_lock:
        if _bot_started:
            return
        _bot_started = True

    db.connect()
    feed.connect()

    balance = feed.get_account_info().get("balance", 5000.0)
    notifier.send_startup("paper", balance)
    logger.info(f"Gold Bot started on Vercel | balance=${balance:.2f}")

    thread = threading.Thread(target=_bot_loop, daemon=True)
    thread.start()


def _bot_loop():
    _state["running"] = True
    while True:
        try:
            tick = feed.get_tick(settings.symbol)
            account = feed.get_account_info()
            positions = feed.get_positions()
            trades_db = db.get_trades(limit=50) if db.conn else []
            daily_pnl = db.get_daily_pnl() if db.conn else 0.0
            news_upcoming = news_filter.get_upcoming_events(hours_ahead=4)
            equity_curve = db.get_equity_curve(limit=100) if db.conn else []

            _state.update({
                "balance": account.get("balance", 0) if account else 0,
                "equity": account.get("equity", 0) if account else 0,
                "daily_pnl": daily_pnl,
                "open_positions": len(positions),
                "price": tick or {},
                "positions": positions,
                "trades": trades_db,
                "risk": {
                    "daily_loss_pct": risk.get_daily_loss_pct(),
                    "drawdown_pct": risk.get_total_drawdown_pct(),
                    "open_positions": len(positions),
                },
                "news": news_upcoming,
                "equity_curve": equity_curve,
                "logs": list(LOG_BUFFER),
            })

            if account and db.conn:
                db.save_equity_snapshot(
                    balance=account.get("balance", 0),
                    equity=account.get("equity", 0),
                    drawdown=risk.get_total_drawdown_pct(),
                )

            # Monitor open positions
            for pos in positions:
                _manage_position(pos)

            if risk.should_close_friday() and positions:
                for pos in positions:
                    _close_position(pos, reason="friday_close")
                time.sleep(60)
                continue

            can_trade, reason = risk.can_trade()
            if not can_trade or positions:
                time.sleep(60)
                continue

            news_active, _ = news_filter.is_news_time()
            if news_active:
                time.sleep(60)
                continue

            if not (strategy.is_london_session() or strategy.is_ny_session()):
                time.sleep(60)
                continue

            signal = strategy.get_signal(settings.symbol)
            logger.info(f"Signal: {signal}")

            if signal.get("direction") in ("BUY", "SELL"):
                lot_size = risk.calculate_lot_size(settings.symbol, signal["sl_dollars"])
                trade = executor.execute_signal(signal, lot_size)
                if trade:
                    balance = account.get("balance", 5000.0) if account else 5000.0
                    notifier.send_signal(signal, lot_size, balance)
                    logger.info(
                        f"Trade opened | {signal['direction']} {lot_size} lots "
                        f"@ ${signal['entry']:.2f} [{signal.get('session')}]"
                    )

        except Exception as e:
            logger.error(f"Bot loop error: {e}", exc_info=True)

        time.sleep(60)


def _manage_position(pos):
    if risk.should_close_by_time(pos):
        _close_position(pos, reason="time_limit")
        return

    tick = feed.get_tick(pos["symbol"])
    if tick:
        if pos["type"] == "BUY":
            if tick["bid"] <= pos["sl"]:
                _close_position(pos, reason="stop_loss", close_price=pos["sl"])
                return
            if tick["bid"] >= pos["tp"]:
                _close_position(pos, reason="take_profit", close_price=pos["tp"])
                return
        else:
            if tick["ask"] >= pos["sl"]:
                _close_position(pos, reason="stop_loss", close_price=pos["sl"])
                return
            if tick["ask"] <= pos["tp"]:
                _close_position(pos, reason="take_profit", close_price=pos["tp"])
                return

    new_sl = risk.check_breakeven(pos)
    if new_sl:
        feed.update_sl(pos["ticket"], new_sl)
        logger.info(f"Breakeven set | ticket={pos['ticket']} new_sl={new_sl:.2f}")


def _close_position(pos, reason="manual", close_price=None):
    tick = feed.get_tick(pos["symbol"])
    if close_price is None and tick:
        close_price = tick["bid"] if pos["type"] == "BUY" else tick["ask"]
    closed = feed.close_position(pos["ticket"], close_price or 0.0, reason)
    pnl = closed["profit"] if closed else 0.0
    notifier.send_close(pos, pnl, reason)
    if db.conn:
        db.close_trade(pos["ticket"], close_price or 0.0, pnl, reason)


# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

def _session_name():
    h = datetime.utcnow().hour
    if 7 <= h < 10:  return "LONDON"
    if 13 <= h < 16: return "NEW YORK"
    if h < 7:        return "ASIAN"
    return "OFF-HOURS"


@app.before_request
def ensure_bot_running():
    _start_bot()


@app.route("/")
def index():
    return Response(GOLD_HTML, mimetype="text/html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "symbol": "XAUUSD"})


@app.route("/api/status")
def status():
    return jsonify({
        "symbol": "XAUUSD",
        "mode": _state["mode"],
        "balance": _state["balance"],
        "equity": _state["equity"],
        "daily_pnl": _state["daily_pnl"],
        "open_positions": _state["open_positions"],
        "session": _session_name(),
        "utc_time": datetime.utcnow().strftime("%H:%M UTC"),
        "running": _state["running"],
    })


@app.route("/api/price")
def price():
    return jsonify(_state["price"])


@app.route("/api/positions")
def positions():
    return jsonify(_state["positions"])


@app.route("/api/trades")
def trades():
    return jsonify(_state["trades"])


@app.route("/api/risk")
def risk_endpoint():
    return jsonify(_state["risk"])


@app.route("/api/news")
def news():
    return jsonify(_state["news"])


@app.route("/api/equity")
def equity():
    return jsonify(_state["equity_curve"])


@app.route("/api/logs")
def logs():
    return jsonify({"lines": _state["logs"]})


if __name__ == "__main__":
    _start_bot()
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port)
