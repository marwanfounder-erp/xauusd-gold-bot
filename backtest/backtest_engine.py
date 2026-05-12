"""
XAUUSD Backtest Engine
Run: python backtest/backtest_engine.py
     python main.py --backtest
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
SYMBOL = "GC=F"          # Gold futures on Yahoo Finance
SPREAD = 0.012           # $0.012 spread
SLIPPAGE = 0.005         # $0.005 slippage
STARTING_BALANCE = 10_000.0
RISK_PER_TRADE = 0.01    # 1%
RR_RATIO = 2.0
RSI_PERIOD = 14
RSI_BUY = 60.0
RSI_SELL = 40.0
EMA_PERIOD = 20
BREAKOUT_BUFFER = 0.20
MIN_RANGE = 3.0
MAX_RANGE = 15.0
PIP_SIZE = 0.01
PIP_VALUE = 1.0
MAX_LOT = 1.0
MAX_TRADE_HOURS = 20

LONDON_START = 7
LONDON_END = 10
NY_START = 13
NY_END = 16
ASIAN_END = 7


# ── Data download ─────────────────────────────────────────────────────────────

def download_data(period: str = "2y", interval: str = "1h") -> pd.DataFrame:
    print(f"Downloading XAUUSD (GC=F) data | period={period} interval={interval}")
    ticker = yf.Ticker(SYMBOL)
    df = ticker.history(period=period, interval=interval)
    if df.empty:
        raise ValueError("No data downloaded — check yfinance and GC=F availability")
    df.columns = [c.lower() for c in df.columns]
    df.index = df.index.tz_localize(None) if df.index.tzinfo else df.index
    df = df[["open", "high", "low", "close", "volume"]].dropna()
    print(f"Data loaded: {len(df)} bars | {df.index[0].date()} → {df.index[-1].date()}")
    return df


# ── Indicators ────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = avg_loss.where(avg_loss != 0, 0.0001)
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_ema(series: pd.Series, span: int = EMA_PERIOD) -> pd.Series:
    return series.ewm(span=span).mean()


# ── Range helpers ─────────────────────────────────────────────────────────────

def get_asian_range(day_df: pd.DataFrame) -> Optional[dict]:
    asian = day_df[day_df.index.hour < ASIAN_END]
    if len(asian) < 3:
        return None
    high = float(asian["high"].max())
    low = float(asian["low"].min())
    rng = high - low
    return {"high": high, "low": low, "range_dollars": rng,
            "valid": MIN_RANGE <= rng <= MAX_RANGE}


def get_london_range(day_df: pd.DataFrame) -> Optional[dict]:
    mask = (day_df.index.hour >= LONDON_START) & (day_df.index.hour < LONDON_END)
    london = day_df[mask]
    if len(london) < 1:
        return None
    return {"high": float(london["high"].max()), "low": float(london["low"].min())}


# ── Lot size ──────────────────────────────────────────────────────────────────

def calc_lot_size(balance: float, sl_dollars: float) -> float:
    risk_amount = balance * RISK_PER_TRADE
    sl_pips = sl_dollars / PIP_SIZE
    if sl_pips <= 0:
        return 0.01
    lot = risk_amount / (sl_pips * PIP_VALUE)
    return round(max(0.01, min(lot, MAX_LOT)), 2)


# ── Backtest core ─────────────────────────────────────────────────────────────

def run_backtest():
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    df = download_data(period="2y", interval="1h")

    df["rsi"] = calc_rsi(df["close"])
    df["ema20"] = calc_ema(df["close"], span=EMA_PERIOD)

    balance = STARTING_BALANCE
    trades = []
    open_trade: Optional[dict] = None

    dates = df.index.normalize().unique()
    print(f"\nRunning backtest over {len(dates)} trading days...\n")

    for day in dates:
        day_df = df[df.index.normalize() == day]
        if len(day_df) < 5:
            continue

        asian_range = get_asian_range(day_df)
        london_range = None

        for idx, row in day_df.iterrows():
            hour = idx.hour
            price = row["close"]
            rsi_val = row["rsi"]
            ema_val = row["ema20"]

            # ── Manage open trade ──────────────────────────────────────────
            if open_trade:
                entry = open_trade["entry"]
                sl = open_trade["sl"]
                tp = open_trade["tp"]
                direction = open_trade["direction"]
                lot = open_trade["lot"]
                open_time = open_trade["open_time"]

                # Time limit
                hours_open = (idx - open_time).total_seconds() / 3600
                hit_sl = hit_tp = time_out = False

                if direction == "BUY":
                    if row["low"] <= sl:
                        hit_sl = True
                        close_px = sl - SLIPPAGE
                    elif row["high"] >= tp:
                        hit_tp = True
                        close_px = tp - SLIPPAGE
                elif direction == "SELL":
                    if row["high"] >= sl:
                        hit_sl = True
                        close_px = sl + SLIPPAGE
                    elif row["low"] <= tp:
                        hit_tp = True
                        close_px = tp + SLIPPAGE

                if hours_open >= MAX_TRADE_HOURS and not (hit_sl or hit_tp):
                    time_out = True
                    close_px = price

                if hit_sl or hit_tp or time_out:
                    if direction == "BUY":
                        pnl = (close_px - entry) * lot * 100
                    else:
                        pnl = (entry - close_px) * lot * 100

                    pnl = round(pnl, 2)
                    balance += pnl
                    reason = "tp" if hit_tp else ("sl" if hit_sl else "timeout")
                    open_trade["close_price"] = close_px
                    open_trade["close_time"] = idx
                    open_trade["pnl"] = pnl
                    open_trade["close_reason"] = reason
                    open_trade["balance_after"] = balance
                    trades.append(open_trade)
                    open_trade = None
                    continue

            # ── Skip if position open ──────────────────────────────────────
            if open_trade:
                continue

            trend = "bullish" if price > ema_val * 1.001 else \
                    ("bearish" if price < ema_val * 0.999 else "neutral")

            # ── London breakout ────────────────────────────────────────────
            if LONDON_START <= hour < LONDON_END and asian_range and asian_range["valid"]:
                buf = BREAKOUT_BUFFER
                buy_lvl = asian_range["high"] + buf
                sell_lvl = asian_range["low"] - buf
                ask = price + SPREAD / 2 + SLIPPAGE
                bid = price - SPREAD / 2 - SLIPPAGE

                if ask > buy_lvl and rsi_val > RSI_BUY and trend != "bearish":
                    sl = asian_range["low"] - buf
                    sl_dist = ask - sl
                    tp = ask + sl_dist * RR_RATIO
                    lot = calc_lot_size(balance, sl_dist)
                    open_trade = {
                        "direction": "BUY", "entry": ask, "sl": sl, "tp": tp,
                        "lot": lot, "open_time": idx, "session": "LONDON",
                        "rsi": round(rsi_val, 1), "range": asian_range["range_dollars"],
                    }

                elif bid < sell_lvl and rsi_val < RSI_SELL and trend != "bullish":
                    sl = asian_range["high"] + buf
                    sl_dist = sl - bid
                    tp = bid - sl_dist * RR_RATIO
                    lot = calc_lot_size(balance, sl_dist)
                    open_trade = {
                        "direction": "SELL", "entry": bid, "sl": sl, "tp": tp,
                        "lot": lot, "open_time": idx, "session": "LONDON",
                        "rsi": round(rsi_val, 1), "range": asian_range["range_dollars"],
                    }

            # ── NY breakout ────────────────────────────────────────────────
            elif NY_START <= hour < NY_END:
                if london_range is None:
                    london_range = get_london_range(day_df)
                if london_range:
                    buf = BREAKOUT_BUFFER
                    ask = price + SPREAD / 2 + SLIPPAGE
                    bid = price - SPREAD / 2 - SLIPPAGE

                    if ask > london_range["high"] + buf and rsi_val > RSI_BUY and trend != "bearish":
                        sl = london_range["low"] - buf
                        sl_dist = ask - sl
                        tp = ask + sl_dist * RR_RATIO
                        lot = calc_lot_size(balance, sl_dist)
                        open_trade = {
                            "direction": "BUY", "entry": ask, "sl": sl, "tp": tp,
                            "lot": lot, "open_time": idx, "session": "NY",
                            "rsi": round(rsi_val, 1), "range": 0,
                        }

                    elif bid < london_range["low"] - buf and rsi_val < RSI_SELL and trend != "bullish":
                        sl = london_range["high"] + buf
                        sl_dist = sl - bid
                        tp = bid - sl_dist * RR_RATIO
                        lot = calc_lot_size(balance, sl_dist)
                        open_trade = {
                            "direction": "SELL", "entry": bid, "sl": sl, "tp": tp,
                            "lot": lot, "open_time": idx, "session": "NY",
                            "rsi": round(rsi_val, 1), "range": 0,
                        }

    # Close any open trade at end of data
    if open_trade and trades or open_trade:
        open_trade["close_price"] = float(df["close"].iloc[-1])
        open_trade["close_time"] = df.index[-1]
        open_trade["pnl"] = 0.0
        open_trade["close_reason"] = "end_of_data"
        open_trade["balance_after"] = balance
        trades.append(open_trade)

    _print_report(trades, STARTING_BALANCE, balance, df)


def _print_report(trades: list, start_balance: float, end_balance: float, df: pd.DataFrame):
    if not trades:
        print("No trades generated.")
        return

    total = len(trades)
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    win_rate = len(wins) / total * 100
    total_pnl = sum(t.get("pnl", 0) for t in trades)
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Max drawdown
    peak = start_balance
    max_dd = 0.0
    running = start_balance
    for t in sorted(trades, key=lambda x: x.get("close_time", datetime.min)):
        running += t.get("pnl", 0)
        if running > peak:
            peak = running
        dd = (peak - running) / peak
        max_dd = max(max_dd, dd)

    # Monthly breakdown
    monthly: dict = {}
    for t in trades:
        ct = t.get("close_time")
        if ct:
            key = ct.strftime("%Y-%m") if isinstance(ct, datetime) else str(ct)[:7]
            monthly.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
            monthly[key]["trades"] += 1
            if t.get("pnl", 0) > 0:
                monthly[key]["wins"] += 1
            monthly[key]["pnl"] += t.get("pnl", 0)

    # Session breakdown
    london_trades = [t for t in trades if t.get("session") == "LONDON"]
    ny_trades = [t for t in trades if t.get("session") == "NY"]

    months_count = max(len(monthly), 1)
    monthly_return = ((end_balance / start_balance) ** (1 / months_count) - 1) * 100

    print("\n" + "═" * 60)
    print("  XAUUSD GOLD BOT — BACKTEST RESULTS")
    print("  London + NY Breakout Strategy")
    print("═" * 60)
    print(f"  Period:          {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Starting:        ${start_balance:,.2f}")
    print(f"  Final:           ${end_balance:,.2f}")
    print(f"  Net P&L:         ${total_pnl:+,.2f}  ({total_pnl/start_balance*100:+.1f}%)")
    print("─" * 60)
    print(f"  Total Trades:    {total}")
    print(f"  Win Rate:        {win_rate:.1f}%")
    print(f"  Profit Factor:   {profit_factor:.2f}")
    print(f"  Avg Win:         ${gross_profit/len(wins):.2f}" if wins else "  Avg Win:        —")
    print(f"  Avg Loss:        ${gross_loss/len(losses):.2f}" if losses else "  Avg Loss:        —")
    print(f"  Max Drawdown:    {max_dd:.1%}")
    print(f"  Monthly Return:  {monthly_return:.2f}%")
    print("─" * 60)
    print(f"  London trades:   {len(london_trades)}  (WR: {len([t for t in london_trades if t.get('pnl',0)>0])/max(len(london_trades),1)*100:.0f}%)")
    print(f"  NY trades:       {len(ny_trades)}  (WR: {len([t for t in ny_trades if t.get('pnl',0)>0])/max(len(ny_trades),1)*100:.0f}%)")
    print("─" * 60)
    print("  MONTHLY BREAKDOWN")
    print(f"  {'Month':<12}{'Trades':>8}{'WR':>8}{'P&L':>12}")
    print("  " + "-" * 40)
    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
        print(f"  {month:<12}{m['trades']:>8}{wr:>7.0f}%{m['pnl']:>+12.2f}")
    print("═" * 60)

    # Quick pass/fail
    passed = win_rate >= 45 and max_dd <= 0.08
    verdict = "PASS ✓ — Safe to deploy for paper trading" if passed else \
              "REVIEW ✗ — Check parameters before deploying"
    print(f"\n  Verdict: {verdict}")
    print("═" * 60 + "\n")

    return {
        "total_trades": total,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_drawdown": max_dd,
        "monthly_return": monthly_return,
        "net_pnl": total_pnl,
    }


if __name__ == "__main__":
    run_backtest()
