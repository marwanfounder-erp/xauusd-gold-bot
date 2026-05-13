"""
XAUUSD Gold Bot — London + NY Breakout Strategy
Usage:
  python main.py --paper      paper trading via yfinance
  python main.py --live       live trading via MT5
  python main.py --backtest   run backtest engine
"""

import argparse
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime

from config import settings

# ── Logging setup ─────────────────────────────────────────────────────────────
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
        logging.FileHandler(f"logs/gold_bot_{datetime.utcnow().strftime('%Y%m%d')}.log"),
        BufferHandler(),
    ],
)
logger = logging.getLogger("main")

# ── Imports (after logging) ───────────────────────────────────────────────────
from bot.strategy import XAUUSDStrategy
from bot.risk_manager import RiskManager
from bot.executor import TradeExecutor
from bot.news_filter import NewsFilter
from bot.notifier import TelegramNotifier
from bot.database import Database
from bot.dashboard_server import DashboardServer


def run_paper(args):
    from bot.paper_feed import PaperDataFeed

    db = Database(settings)
    db.connect()

    feed = PaperDataFeed(settings, db)
    feed.connect()

    strategy = XAUUSDStrategy(settings, feed)
    risk = RiskManager(settings, feed, db)
    executor = TradeExecutor(settings, feed, db)
    news_filter = NewsFilter(settings)
    notifier = TelegramNotifier(settings)
    dashboard = DashboardServer(settings)
    dashboard.start()

    balance = feed.get_account_info().get("balance", 5000.0)
    notifier.send_startup("paper", balance)
    logger.info(f"XAUUSD Gold Bot started in PAPER mode | balance=${balance:.2f}")

    _main_loop(
        feed=feed,
        strategy=strategy,
        risk=risk,
        executor=executor,
        news_filter=news_filter,
        notifier=notifier,
        dashboard=dashboard,
        db=db,
        mode="paper",
    )


def run_live(args):
    from bot.data_feed import MT5DataFeed

    db = Database(settings)
    db.connect()

    feed = MT5DataFeed(settings)
    if not feed.connect():
        logger.error("MT5 connection failed — aborting")
        sys.exit(1)

    strategy = XAUUSDStrategy(settings, feed)
    risk = RiskManager(settings, feed, db)
    executor = TradeExecutor(settings, feed, db)
    news_filter = NewsFilter(settings)
    notifier = TelegramNotifier(settings)
    dashboard = DashboardServer(settings)
    dashboard.start()

    balance = feed.get_account_info().get("balance", 0.0)
    notifier.send_startup("live", balance)
    logger.info(f"XAUUSD Gold Bot started in LIVE mode | balance=${balance:.2f}")

    _main_loop(
        feed=feed,
        strategy=strategy,
        risk=risk,
        executor=executor,
        news_filter=news_filter,
        notifier=notifier,
        dashboard=dashboard,
        db=db,
        mode="live",
    )


def _main_loop(feed, strategy, risk, executor, news_filter, notifier, dashboard, db, mode):
    logger.info("Main loop started — polling every 60s")
    last_daily_report = None

    while True:
        try:
            now = datetime.utcnow()

            # ── Daily report ───────────────────────────────────────────────
            if now.hour == 22 and now.minute < 2:
                today_key = now.date()
                if last_daily_report != today_key:
                    last_daily_report = today_key
                    _send_daily_report(db, risk, notifier)

            # ── Dashboard state ────────────────────────────────────────────
            tick = feed.get_tick(settings.symbol)
            account = feed.get_account_info()
            positions = feed.get_positions()
            trades_db = db.get_trades(limit=50) if db.conn else []
            daily_pnl = db.get_daily_pnl() if db.conn else 0.0
            news_active, news_msg, news_upcoming = news_filter.check(hours_ahead=4)
            equity_curve = db.get_equity_curve(limit=100) if db.conn else []

            dashboard.update(
                mode=mode,
                balance=account.get("balance", 0) if account else 0,
                equity=account.get("equity", 0) if account else 0,
                daily_pnl=daily_pnl,
                open_positions=len(positions),
                price=tick or {},
                positions=positions,
                trades=trades_db,
                risk={
                    "daily_loss_pct": risk.get_daily_loss_pct(),
                    "drawdown_pct": risk.get_total_drawdown_pct(),
                    "open_positions": len(positions),
                },
                news=news_upcoming,
                equity_curve=equity_curve,
                logs=list(LOG_BUFFER),
            )

            # ── Equity snapshot ────────────────────────────────────────────
            if account and db.conn:
                db.save_equity_snapshot(
                    balance=account.get("balance", 0),
                    equity=account.get("equity", 0),
                    drawdown=risk.get_total_drawdown_pct(),
                )

            # ── Monitor open positions ─────────────────────────────────────
            for pos in positions:
                _manage_position(pos, feed, risk, executor, notifier, db, mode)

            # ── Friday close ───────────────────────────────────────────────
            if risk.should_close_friday() and positions:
                logger.info("Friday close — closing all positions")
                for pos in positions:
                    _close_position(pos, feed, executor, notifier, db, mode, reason="friday_close")
                time.sleep(60)
                continue

            # ── Trading guards ─────────────────────────────────────────────
            can_trade, reason = risk.can_trade()
            if not can_trade:
                logger.info(f"Trading blocked: {reason}")
                time.sleep(60)
                continue

            if positions:
                logger.info(f"Position open — skipping signal check")
                time.sleep(60)
                continue

            # ── News filter (result already computed above in dashboard section)
            if news_active:
                logger.info(f"News block: {news_msg}")
                time.sleep(60)
                continue

            # ── Signal check ───────────────────────────────────────────────
            if not (strategy.is_london_session() or strategy.is_ny_session()):
                time.sleep(60)
                continue

            signal = strategy.get_signal(settings.symbol)
            logger.info(f"Signal: {signal}")

            if signal.get("direction") not in ("BUY", "SELL"):
                time.sleep(60)
                continue

            # ── Execute trade ──────────────────────────────────────────────
            lot_size = risk.calculate_lot_size(settings.symbol, signal["sl_dollars"])
            balance = account.get("balance", 5000.0) if account else 5000.0

            trade = executor.execute_signal(signal, lot_size)
            if trade:
                logger.info(
                    f"Trade opened | {signal['direction']} {lot_size} lots "
                    f"entry=${signal['entry']:.2f} SL=${signal['stop_loss']:.2f} "
                    f"TP=${signal['take_profit']:.2f} [{signal.get('session')}]"
                )
                notifier.send_signal(signal, lot_size, balance)

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)

        time.sleep(60)


def _manage_position(pos, feed, risk, executor, notifier, db, mode):
    # Time limit
    if risk.should_close_by_time(pos):
        logger.info(f"Time limit reached for ticket {pos['ticket']}")
        _close_position(pos, feed, executor, notifier, db, mode, reason="time_limit")
        return

    # Paper SL/TP check
    if mode == "paper":
        tick = feed.get_tick(pos["symbol"])
        if tick:
            if pos["type"] == "BUY":
                if tick["bid"] <= pos["sl"]:
                    _close_position(pos, feed, executor, notifier, db, mode, reason="stop_loss", close_price=pos["sl"])
                    return
                if tick["bid"] >= pos["tp"]:
                    _close_position(pos, feed, executor, notifier, db, mode, reason="take_profit", close_price=pos["tp"])
                    return
            else:
                if tick["ask"] >= pos["sl"]:
                    _close_position(pos, feed, executor, notifier, db, mode, reason="stop_loss", close_price=pos["sl"])
                    return
                if tick["ask"] <= pos["tp"]:
                    _close_position(pos, feed, executor, notifier, db, mode, reason="take_profit", close_price=pos["tp"])
                    return

    # Breakeven
    new_sl = risk.check_breakeven(pos)
    if new_sl:
        if mode == "paper":
            feed.update_sl(pos["ticket"], new_sl)
        else:
            executor.modify_sl_mt5(pos["ticket"], new_sl)
        logger.info(f"Breakeven set for ticket {pos['ticket']} new_sl={new_sl:.2f}")


def _close_position(pos, feed, executor, notifier, db, mode, reason="manual", close_price=None):
    tick = feed.get_tick(pos["symbol"])
    if close_price is None and tick:
        close_price = tick["bid"] if pos["type"] == "BUY" else tick["ask"]

    if mode == "paper":
        closed = feed.close_position(pos["ticket"], close_price or 0.0, reason)
        pnl = closed["profit"] if closed else 0.0
    else:
        executor.close_position_mt5(pos, reason)
        tick = feed.get_tick(pos["symbol"])
        cp = tick["bid"] if tick and pos["type"] == "BUY" else (tick["ask"] if tick else 0)
        if pos["type"] == "BUY":
            pnl = (cp - pos["price_open"]) * pos["volume"] * 100
        else:
            pnl = (pos["price_open"] - cp) * pos["volume"] * 100
        pnl = round(pnl, 2)

    notifier.send_close(pos, pnl, reason)
    if db.conn:
        db.close_trade(pos["ticket"], close_price or 0.0, pnl, reason)
    from bot.risk_manager import RiskManager  # local import to avoid circular
    # record for daily tracking (handled inside risk manager externally)
    logger.info(f"Position closed | ticket={pos['ticket']} pnl=${pnl:.2f} reason={reason}")


def _send_daily_report(db, risk, notifier):
    daily_pnl = db.get_daily_pnl() if db.conn else 0.0
    trades = db.get_trades(limit=100) if db.conn else []
    today_trades = [t for t in trades if t.get("status") == "closed"]
    wins = sum(1 for t in today_trades if (t.get("pnl") or 0) > 0)
    notifier.send_daily_report({
        "daily_pnl": daily_pnl,
        "daily_trades": len(today_trades),
        "daily_wins": wins,
        "drawdown_pct": risk.get_total_drawdown_pct(),
    })


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="XAUUSD Gold Bot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--paper", action="store_true", help="Paper trading mode")
    group.add_argument("--live", action="store_true", help="Live MT5 trading")
    group.add_argument("--backtest", action="store_true", help="Run backtest")
    parser.add_argument(
        "--source",
        choices=["alpaca", "yfinance"],
        default=None,
        help="Force backtest data source (alpaca | yfinance). Default: auto-detect.",
    )
    args = parser.parse_args()

    if args.backtest:
        from backtest.backtest_engine import run_backtest
        run_backtest(source=args.source)
    elif args.paper:
        run_paper(args)
    elif args.live:
        run_live(args)


if __name__ == "__main__":
    main()
