"""
Multi-Pair London Breakout Bot
  Pairs:   XAUUSD · GBPUSD · USDJPY
  Session: London (07-10 UTC) + NY (13-16 UTC)
  Mode:    --paper | --live | --backtest
"""

import argparse
import logging
import os
import sys
import time
from collections import deque
from datetime import datetime

from config import settings, PAIR_SETTINGS

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
    risk     = RiskManager(settings, feed, db)
    executor = TradeExecutor(settings, feed, db)
    news_filter = NewsFilter(settings)
    notifier    = TelegramNotifier(settings)
    dashboard   = DashboardServer(settings)
    dashboard.start()

    balance = feed.get_account_info().get("balance", 5000.0)
    notifier.send_startup("paper", balance)
    logger.info(
        f"Multi-Pair Bot started | PAPER mode | "
        f"pairs={settings.symbols} balance=${balance:.2f}"
    )

    _main_loop(
        feed=feed, strategy=strategy, risk=risk, executor=executor,
        news_filter=news_filter, notifier=notifier, dashboard=dashboard,
        db=db, mode="paper",
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
    risk     = RiskManager(settings, feed, db)
    executor = TradeExecutor(settings, feed, db)
    news_filter = NewsFilter(settings)
    notifier    = TelegramNotifier(settings)
    dashboard   = DashboardServer(settings)
    dashboard.start()

    balance = feed.get_account_info().get("balance", 0.0)
    notifier.send_startup("live", balance)
    logger.info(
        f"Multi-Pair Bot started | LIVE mode | "
        f"pairs={settings.symbols} balance=${balance:.2f}"
    )

    _main_loop(
        feed=feed, strategy=strategy, risk=risk, executor=executor,
        news_filter=news_filter, notifier=notifier, dashboard=dashboard,
        db=db, mode="live",
    )


def _main_loop(feed, strategy, risk, executor, news_filter, notifier, dashboard, db, mode):
    logger.info(f"Main loop started — pairs={settings.symbols} polling every 60s")
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

            # ── Aggregate tick data for dashboard ──────────────────────────
            prices      = {}
            for sym in settings.symbols:
                t = feed.get_tick(sym)
                if t:
                    prices[sym] = t
            # Use first available price for legacy dashboard price field
            price_tick = prices.get(settings.symbols[0], {})

            account   = feed.get_account_info()
            positions = feed.get_positions()
            trades_db = db.get_trades(limit=50) if db.conn else []
            daily_pnl = db.get_daily_pnl() if db.conn else 0.0
            news_active, news_msg, news_upcoming = news_filter.check(hours_ahead=4)
            equity_curve = db.get_equity_curve(limit=100) if db.conn else []

            # Per-pair stats for dashboard
            pair_stats = {}
            for sym in settings.symbols:
                pair_stats[sym] = db.get_pair_stats(sym) if db.conn else {}

            # Per-pair current positions for dashboard
            pair_positions = {}
            for sym in settings.symbols:
                pair_positions[sym] = [p for p in positions if p.get("symbol") == sym]

            dashboard.update(
                mode=mode,
                balance=account.get("balance", 0) if account else 0,
                equity=account.get("equity", 0) if account else 0,
                daily_pnl=daily_pnl,
                open_positions=len(positions),
                price=price_tick,
                prices=prices,
                positions=positions,
                trades=trades_db,
                risk={
                    "daily_loss_pct":  risk.get_daily_loss_pct(),
                    "drawdown_pct":    risk.get_total_drawdown_pct(),
                    "open_positions":  len(positions),
                    "max_positions":   settings.max_total_positions,
                },
                news=news_upcoming,
                equity_curve=equity_curve,
                logs=list(LOG_BUFFER),
                pair_stats=pair_stats,
                pair_positions=pair_positions,
                symbols=settings.symbols,
            )

            # ── Equity snapshot ────────────────────────────────────────────
            if account and db.conn:
                db.save_equity_snapshot(
                    balance=account.get("balance", 0),
                    equity=account.get("equity", 0),
                    drawdown=risk.get_total_drawdown_pct(),
                )

            # ── Monitor open positions (SL/TP, breakeven, time limit) ──────
            for pos in positions:
                _manage_position(pos, feed, risk, executor, notifier, db, mode)

            # ── Friday close — close all pairs ─────────────────────────────
            if risk.should_close_friday() and positions:
                logger.info("Friday close — closing all pairs")
                for pos in list(positions):
                    _close_position(pos, feed, executor, notifier, db, mode, reason="friday_close")
                time.sleep(60)
                continue

            # ── Global risk guards ─────────────────────────────────────────
            can_trade, reason = risk.can_trade()
            if not can_trade:
                logger.info(f"Trading blocked: {reason}")
                time.sleep(60)
                continue

            # ── News filter ────────────────────────────────────────────────
            if news_active:
                logger.info(f"News block: {news_msg}")
                time.sleep(60)
                continue

            # ── Session guard ──────────────────────────────────────────────
            if not (strategy.is_london_session() or strategy.is_ny_session()):
                h, m = now.hour, now.minute
                if h < settings.london_session_start:
                    mins = (settings.london_session_start - h) * 60 - m
                    next_msg = f"London opens in {mins // 60}h {mins % 60}m"
                elif settings.london_session_end <= h < settings.ny_session_start:
                    mins = (settings.ny_session_start - h) * 60 - m
                    next_msg = f"NY opens in {mins // 60}h {mins % 60}m"
                else:
                    next_msg = "London opens tomorrow at 07:00 UTC"
                logger.info(
                    f"Bot heartbeat | time={now.strftime('%H:%M')} UTC "
                    f"session=WAITING | {next_msg}"
                )
                time.sleep(60)
                continue

            # ── Multi-pair signal loop ─────────────────────────────────────
            # Refresh positions after any management actions above
            positions = feed.get_positions()

            if len(positions) >= settings.max_total_positions:
                logger.info(
                    f"Max positions reached "
                    f"({len(positions)}/{settings.max_total_positions}) — skipping signal check"
                )
                time.sleep(60)
                continue

            for symbol in settings.symbols:
                # Cap total positions
                if len(positions) >= settings.max_total_positions:
                    logger.info(
                        f"Max positions reached mid-loop "
                        f"({len(positions)}/{settings.max_total_positions})"
                    )
                    break

                # Skip if this pair already has an open position
                pair_pos = [p for p in positions if p.get("symbol") == symbol]
                if pair_pos:
                    logger.debug(f"{symbol} position already open — skipping")
                    continue

                signal = strategy.get_signal(symbol)
                logger.info(f"{symbol} signal: {signal.get('direction')} | {signal.get('reason', '')}")

                if signal.get("direction") not in ("BUY", "SELL"):
                    continue

                # Calculate lot size using sl_pips
                lot_size = risk.calculate_lot_size(symbol, signal["sl_pips"])
                balance  = account.get("balance", 5000.0) if account else 5000.0

                trade = executor.execute_signal(signal, lot_size)
                if trade:
                    logger.info(
                        f"Trade opened | {symbol} {signal['direction']} {lot_size} lots "
                        f"entry={signal['entry']} SL={signal['stop_loss']} "
                        f"TP={signal['take_profit']} [{signal.get('session')}]"
                    )
                    notifier.send_signal(signal, lot_size, balance)

                # Refresh position count
                positions = feed.get_positions()

        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break
        except Exception as e:
            logger.error(f"Main loop error: {e}", exc_info=True)

        time.sleep(60)


def _manage_position(pos, feed, risk, executor, notifier, db, mode):
    if risk.should_close_by_time(pos):
        logger.info(f"Time limit reached for ticket {pos['ticket']} ({pos.get('symbol')})")
        _close_position(pos, feed, executor, notifier, db, mode, reason="time_limit")
        return

    if mode == "paper":
        tick = feed.get_tick(pos["symbol"])
        if tick:
            if pos["type"] == "BUY":
                if tick["bid"] <= pos["sl"]:
                    _close_position(pos, feed, executor, notifier, db, mode,
                                    reason="stop_loss", close_price=pos["sl"])
                    return
                if tick["bid"] >= pos["tp"]:
                    _close_position(pos, feed, executor, notifier, db, mode,
                                    reason="take_profit", close_price=pos["tp"])
                    return
            else:
                if tick["ask"] >= pos["sl"]:
                    _close_position(pos, feed, executor, notifier, db, mode,
                                    reason="stop_loss", close_price=pos["sl"])
                    return
                if tick["ask"] <= pos["tp"]:
                    _close_position(pos, feed, executor, notifier, db, mode,
                                    reason="take_profit", close_price=pos["tp"])
                    return

    new_sl = risk.check_breakeven(pos)
    if new_sl:
        if mode == "paper":
            feed.update_sl(pos["ticket"], new_sl)
        else:
            executor.modify_sl_mt5(pos["ticket"], new_sl)
        logger.info(
            f"Breakeven set | {pos.get('symbol')} ticket={pos['ticket']} new_sl={new_sl}"
        )


def _close_position(pos, feed, executor, notifier, db, mode, reason="manual", close_price=None):
    symbol = pos.get("symbol", settings.symbol)
    tick   = feed.get_tick(symbol)
    if close_price is None and tick:
        close_price = tick["bid"] if pos["type"] == "BUY" else tick["ask"]

    if mode == "paper":
        closed = feed.close_position(pos["ticket"], close_price or 0.0, reason)
        pnl = closed["profit"] if closed else 0.0
    else:
        executor.close_position_mt5(pos, reason)
        tick = feed.get_tick(symbol)
        cp   = (tick["bid"] if pos["type"] == "BUY" else tick["ask"]) if tick else 0.0
        pair = PAIR_SETTINGS.get(symbol, {})
        pip_size = pair.get('pip_size', 0.01)
        pip_val  = pair.get('pip_value', 1.0)
        if pos["type"] == "BUY":
            pips = (cp - pos["price_open"]) / pip_size
        else:
            pips = (pos["price_open"] - cp) / pip_size
        pnl = round(pips * pip_val * pos["volume"], 2)

    risk_mgr_pnl = pnl   # passed separately to avoid circular import
    notifier.send_close(pos, pnl, reason)
    if db.conn:
        db.close_trade(pos["ticket"], close_price or 0.0, pnl, reason, symbol=symbol)
    logger.info(
        f"Position closed | {symbol} ticket={pos['ticket']} pnl=${pnl:.2f} reason={reason}"
    )


def _send_daily_report(db, risk, notifier):
    daily_pnl   = db.get_daily_pnl() if db.conn else 0.0
    trades      = db.get_trades(limit=100) if db.conn else []
    today_trades = [t for t in trades if t.get("status") == "closed"]
    wins         = sum(1 for t in today_trades if (t.get("pnl") or 0) > 0)
    notifier.send_daily_report({
        "daily_pnl":    daily_pnl,
        "daily_trades": len(today_trades),
        "daily_wins":   wins,
        "drawdown_pct": risk.get_total_drawdown_pct(),
    })


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Multi-Pair London Breakout Bot")
    group  = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--paper",    action="store_true", help="Paper trading mode")
    group.add_argument("--live",     action="store_true", help="Live MT5 trading")
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
