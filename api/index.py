"""
Vercel entry point — XAUUSD Gold Bot.
Each API endpoint reads directly from its source (Finnhub/DB) so
Vercel cold starts never show zeros. Background thread handles trading.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from flask import Flask, jsonify, Response

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_BUFFER = deque(maxlen=500)

class BufferHandler(logging.Handler):
    def emit(self, record):
        msg = self.format(record)
        LOG_BUFFER.append(msg)
        # Also persist to DB so logs survive across Vercel cold-starts.
        # db may not be connected yet at import time; guard with hasattr.
        try:
            if "db" in globals() and db.conn:
                db.save_log(msg)
        except Exception:
            pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), BufferHandler()],
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

# ── Singletons ────────────────────────────────────────────────────────────────
db = Database(settings)
feed = PaperDataFeed(settings, db)
strategy = XAUUSDStrategy(settings, feed)
risk = RiskManager(settings, feed, db)
executor = TradeExecutor(settings, feed, db)
news_filter = NewsFilter(settings)
notifier = TelegramNotifier(settings)

# ── Price cache (30s TTL — avoids hammering Finnhub on every dashboard poll) ──
_tick_cache: dict = {"data": None, "ts": 0.0}
_TICK_TTL = 30  # seconds

def _get_tick():
    now = time.time()
    if _tick_cache["data"] and (now - _tick_cache["ts"]) < _TICK_TTL:
        return _tick_cache["data"]
    tick = feed.get_tick(settings.symbol)
    if tick:
        _tick_cache["data"] = tick
        _tick_cache["ts"] = now
    return tick or _tick_cache.get("data") or {}

# ── Bot startup (once per process) ───────────────────────────────────────────
_bot_started = False
_bot_lock = threading.Lock()

_STARTUP_COOLDOWN = 360  # minutes between Telegram startup messages (6h — Vercel recycles ~30min)
_STARTUP_TABLE = """
CREATE TABLE IF NOT EXISTS gold_startup_log (
    id SERIAL PRIMARY KEY, started_at TIMESTAMPTZ DEFAULT NOW()
);
"""

def _should_notify() -> bool:
    if not db.conn:
        return False
    try:
        with db.conn.cursor() as cur:
            cur.execute(_STARTUP_TABLE)
            cur.execute("SELECT started_at FROM gold_startup_log ORDER BY started_at DESC LIMIT 1")
            row = cur.fetchone()
            if row:
                last = row[0].replace(tzinfo=None) if row[0].tzinfo else row[0]
                if datetime.utcnow() - last < timedelta(minutes=_STARTUP_COOLDOWN):
                    return False
            cur.execute("INSERT INTO gold_startup_log DEFAULT VALUES")
            return True
    except Exception as e:
        logger.warning(f"Startup log error: {e}")
        return False


def _start_bot():
    global _bot_started
    with _bot_lock:
        if _bot_started:
            return
        _bot_started = True
    db.connect()
    feed.connect()
    logger.info("Gold Bot process started on Vercel")
    if _should_notify():
        acct = feed.get_account_info()
        notifier.send_startup("paper", acct.get("balance", 5000.0))
    threading.Thread(target=_bot_loop, daemon=True).start()


# ── Trading cycle (called both from loop and per-request) ─────────────────────
_cycle_lock = threading.Lock()

def _run_trading_cycle():
    """Single trading check — safe to call from any thread, deduplicated."""
    if not _cycle_lock.acquire(blocking=False):
        return  # another cycle is already running
    try:
        positions = feed.get_positions()

        for pos in list(positions):
            _manage_position(pos)

        if risk.should_close_friday() and positions:
            for pos in positions:
                _close_position(pos, reason="friday_close")
            return

        can_trade, _ = risk.can_trade()
        if not can_trade or positions:
            return

        if news_filter.is_news_time()[0]:
            return

        if not (strategy.is_london_session() or strategy.is_ny_session()):
            return

        signal = strategy.get_signal(settings.symbol)
        if signal.get("direction") not in ("BUY", "SELL"):
            return

        logger.info(f"Signal: {signal}")
        lot = risk.calculate_lot_size(settings.symbol, signal["sl_dollars"])
        trade = executor.execute_signal(signal, lot)
        if trade:
            acct = feed.get_account_info()
            notifier.send_signal(signal, lot, acct.get("balance", 5000))
            logger.info(
                f"Trade | {signal['direction']} {lot} lots "
                f"@ ${signal['entry']:.2f} [{signal.get('session')}]"
            )
    except Exception as e:
        logger.error(f"Trading cycle error: {e}", exc_info=True)
    finally:
        _cycle_lock.release()


# ── Background loop (keeps running between requests when instance is alive) ───
def _bot_loop():
    while True:
        _run_trading_cycle()
        time.sleep(60)


def _manage_position(pos):
    if risk.should_close_by_time(pos):
        _close_position(pos, reason="time_limit")
        return
    tick = feed.get_tick(pos["symbol"])
    if tick:
        if pos["type"] == "BUY":
            if tick["bid"] <= pos["sl"]:
                _close_position(pos, "stop_loss", pos["sl"])
                return
            if tick["bid"] >= pos["tp"]:
                _close_position(pos, "take_profit", pos["tp"])
                return
        else:
            if tick["ask"] >= pos["sl"]:
                _close_position(pos, "stop_loss", pos["sl"])
                return
            if tick["ask"] <= pos["tp"]:
                _close_position(pos, "take_profit", pos["tp"])
                return
    new_sl = risk.check_breakeven(pos)
    if new_sl:
        feed.update_sl(pos["ticket"], new_sl)


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


@app.route("/api/price")
def price():
    # Direct Finnhub call — never returns stale zero
    tick = _get_tick()
    return jsonify(tick)


@app.route("/api/status")
def status():
    # Read balance from DB, positions from memory
    acct = feed.get_account_info()
    positions = feed.get_positions()
    daily_pnl = db.get_daily_pnl() if db.conn else 0.0

    # Trigger trading logic inline — fires even when background thread is dead
    threading.Thread(target=_run_trading_cycle, daemon=True).start()

    return jsonify({
        "symbol": "XAUUSD",
        "mode": "paper",
        "balance": acct.get("balance", 5000.0),
        "equity": acct.get("equity", 5000.0),
        "daily_pnl": daily_pnl,
        "open_positions": len(positions),
        "session": _session_name(),
        "utc_time": datetime.utcnow().strftime("%H:%M UTC"),
        "running": True,
    })


@app.route("/api/positions")
def positions():
    return jsonify(feed.get_positions())


@app.route("/api/trades")
def trades():
    return jsonify(db.get_trades(limit=50) if db.conn else [])


@app.route("/api/risk")
def risk_api():
    positions = feed.get_positions()
    return jsonify({
        "daily_loss_pct": risk.get_daily_loss_pct(),
        "drawdown_pct": risk.get_total_drawdown_pct(),
        "open_positions": len(positions),   # always an int, never undefined
    })


@app.route("/api/news")
def news():
    return jsonify(news_filter.get_upcoming_events(hours_ahead=4))


@app.route("/api/equity")
def equity():
    return jsonify(db.get_equity_curve(limit=100) if db.conn else [])


@app.route("/api/logs")
def logs():
    # Prefer DB logs — they survive across Vercel cold-starts / instances.
    # Fall back to in-memory buffer if DB is unavailable.
    if db.conn:
        lines = db.get_logs(limit=100)
    else:
        lines = list(LOG_BUFFER)
    return jsonify({"lines": lines})


if __name__ == "__main__":
    _start_bot()
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port)
