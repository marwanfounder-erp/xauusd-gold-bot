"""
XAUUSD Backtest Engine
Run: python backtest/backtest_engine.py
     python main.py --backtest

Data source priority:
  1. Alpaca XAUUSD  — forex v1beta3 (actual gold spot, 24/7)
  2. Alpaca GLD     — stocks v2, scaled ×10 (US hours only — London session absent)
  3. yfinance GC=F  — Gold futures, 24/7 (reliable offline fallback)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
SPREAD           = 0.20          # realistic Gold spread ($)
SLIPPAGE         = 0.10          # realistic slippage ($)
STARTING_BALANCE = 10_000.0
RISK_PER_TRADE   = 0.01          # 1%
RR_RATIO         = 2.0
RSI_PERIOD       = 14
RSI_BUY          = 65.0   # raised from 60 — NY-only tighter filter
RSI_SELL         = 35.0   # lowered from 40 — NY-only tighter filter

# Previous run reference (RSI 60/40, London+NY combined) — used in comparison table
_PREV_NY = {"total": 142, "wr": 52.8, "pf": 1.36, "avg_win": 77.63,
            "avg_loss": 63.78, "max_dd": 0.064, "net_pnl_pct": 15.5}
EMA_PERIOD       = 20
BREAKOUT_BUFFER  = 0.20
MIN_RANGE        = 8.0           # matches live config
MAX_RANGE        = 100.0         # matches live config
PIP_SIZE         = 0.01
PIP_VALUE        = 1.0           # $1 per pip per lot
MAX_LOT          = 1.0
MAX_TRADE_HOURS  = 20

LONDON_START = 7
LONDON_END   = 10
NY_START     = 13
NY_END       = 16
ASIAN_END    = 7

ALPACA_DATA_URL = "https://data.alpaca.markets"


# ── Data download ─────────────────────────────────────────────────────────────

def _load_alpaca_keys() -> tuple[str, str]:
    """Read Alpaca keys from environment, falling back to .env file."""
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret  = os.environ.get("ALPACA_SECRET_KEY", "")
    if api_key and secret:
        return api_key, secret
    # Try python-dotenv first
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        load_dotenv(env_path, override=False)
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret  = os.environ.get("ALPACA_SECRET_KEY", "")
    except ImportError:
        # Manual parse if dotenv not available
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("ALPACA_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("ALPACA_SECRET_KEY="):
                        secret = line.split("=", 1)[1].strip().strip('"').strip("'")
    return api_key, secret


def _alpaca_headers() -> dict:
    api_key, secret = _load_alpaca_keys()
    if not api_key:
        print("  WARNING: ALPACA_API_KEY not set — Alpaca sources will be skipped")
    return {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}


def _fetch_alpaca_pages(url: str, headers: dict, params: dict,
                        bars_key: Optional[str] = None) -> list:
    """Fetch all pages from an Alpaca bars endpoint, following next_page_token."""
    all_bars = []
    page_token = None
    while True:
        p = {**params}
        if page_token:
            p["page_token"] = page_token
        resp = requests.get(url, headers=headers, params=p, timeout=30)
        if resp.status_code == 401:
            raise ValueError("Alpaca 401 — invalid API key")
        if resp.status_code == 422:
            raise ValueError(f"Alpaca 422 — bad params: {resp.text[:200]}")
        if resp.status_code != 200:
            raise ValueError(f"Alpaca HTTP {resp.status_code}: {resp.text[:200]}")
        data      = resp.json()
        bars      = data.get("bars", {}).get(bars_key, []) if bars_key else data.get("bars", [])
        all_bars.extend(bars)
        page_token = data.get("next_page_token")
        if not page_token:
            break
    return all_bars


def _bars_to_df(bars: list) -> pd.DataFrame:
    df = pd.DataFrame(bars)
    df["t"] = pd.to_datetime(df["t"], utc=True)
    df = df.set_index("t")
    df.index = df.index.tz_localize(None)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                             "c": "close", "v": "volume"})
    needed = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    return df[needed].sort_index()


def _download_alpaca_xauusd(headers: dict, start: str, end: str) -> Optional[pd.DataFrame]:
    print("  Trying Alpaca XAUUSD (forex v1beta3)...")
    try:
        bars = _fetch_alpaca_pages(
            f"{ALPACA_DATA_URL}/v1beta3/forex/bars",
            headers,
            {"symbols": "XAUUSD", "timeframe": "1Hour",
             "start": start, "end": end, "limit": 1000, "sort": "asc"},
            bars_key="XAUUSD",
        )
        if not bars:
            print("  Alpaca XAUUSD: no bars returned")
            return None
        df = _bars_to_df(bars)
        print(f"  Alpaca XAUUSD OK: {len(df):,} bars "
              f"({df.index[0].date()} → {df.index[-1].date()})")
        return df
    except Exception as e:
        print(f"  Alpaca XAUUSD failed: {e}")
        return None


def _download_alpaca_gld(headers: dict, start: str, end: str) -> Optional[pd.DataFrame]:
    print("  Trying Alpaca GLD (Gold ETF — US hours only, scaled ×10)...")
    print("  ⚠  GLD trades 09:30–16:00 ET only — Asian & London sessions will have no bars")
    try:
        bars = _fetch_alpaca_pages(
            f"{ALPACA_DATA_URL}/v2/stocks/GLD/bars",
            headers,
            {"timeframe": "1Hour", "start": start, "end": end,
             "limit": 1000, "adjustment": "raw", "sort": "asc"},
        )
        if not bars:
            print("  Alpaca GLD: no bars returned")
            return None
        df = _bars_to_df(bars)
        for col in ["open", "high", "low", "close"]:
            df[col] = (df[col] * 10).round(2)
        print(f"  Alpaca GLD (×10) OK: {len(df):,} bars "
              f"({df.index[0].date()} → {df.index[-1].date()})")
        return df
    except Exception as e:
        print(f"  Alpaca GLD failed: {e}")
        return None


def _download_yfinance() -> Optional[pd.DataFrame]:
    print("  Trying yfinance GC=F (Gold futures, 24/7)...")
    try:
        import yfinance as yf
        ticker = yf.Ticker("GC=F")
        df = ticker.history(period="2y", interval="1h")
        if df.empty:
            print("  yfinance GC=F: empty response")
            return None
        df.columns = [c.lower() for c in df.columns]
        df.index   = df.index.tz_localize(None) if df.index.tzinfo else df.index
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        print(f"  yfinance GC=F OK: {len(df):,} bars "
              f"({df.index[0].date()} → {df.index[-1].date()})")
        return df
    except Exception as e:
        print(f"  yfinance failed: {e}")
        return None


def download_data(source: Optional[str] = None) -> tuple[pd.DataFrame, str]:
    """
    Returns (dataframe, source_label).

    source=None      — auto: Alpaca XAUUSD → Alpaca GLD → yfinance GC=F
    source="alpaca"  — Alpaca XAUUSD → Alpaca GLD only (error if both fail)
    source="yfinance"— skip Alpaca entirely, go straight to yfinance GC=F
    """
    if source == "yfinance":
        df = _download_yfinance()
        if df is not None and len(df) > 100:
            return df, "yfinance GC=F (Gold futures, 24/7)"
        raise ValueError("yfinance GC=F download failed")

    end_dt   = datetime.utcnow()
    start_dt = end_dt - timedelta(days=730)
    start    = start_dt.strftime("%Y-%m-%dT00:00:00Z")
    end      = end_dt.strftime("%Y-%m-%dT00:00:00Z")
    headers  = _alpaca_headers()

    df = _download_alpaca_xauusd(headers, start, end)
    if df is not None and len(df) > 100:
        return df, "Alpaca XAUUSD (forex)"

    df = _download_alpaca_gld(headers, start, end)
    if df is not None and len(df) > 100:
        return df, "Alpaca GLD ×10 (US hours only — NY session only)"

    if source == "alpaca":
        raise ValueError("Alpaca sources failed and --source alpaca was specified")

    # Auto fallback to yfinance
    df = _download_yfinance()
    if df is not None and len(df) > 100:
        return df, "yfinance GC=F (Gold futures, 24/7)"

    raise ValueError("All three data sources failed — cannot run backtest")


# ── Indicators ────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta    = series.diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
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
    low  = float(asian["low"].min())
    rng  = high - low
    return {"high": high, "low": low, "range_dollars": rng,
            "valid": MIN_RANGE <= rng <= MAX_RANGE}


def get_london_range(day_df: pd.DataFrame) -> Optional[dict]:
    mask   = (day_df.index.hour >= LONDON_START) & (day_df.index.hour < LONDON_END)
    london = day_df[mask]
    if len(london) < 1:
        return None
    return {"high": float(london["high"].max()), "low": float(london["low"].min())}


# ── Lot size ──────────────────────────────────────────────────────────────────

def calc_lot_size(balance: float, sl_dollars: float) -> float:
    risk_amount = balance * RISK_PER_TRADE
    sl_pips     = sl_dollars / PIP_SIZE
    if sl_pips <= 0:
        return 0.01
    lot = risk_amount / (sl_pips * PIP_VALUE)
    return round(max(0.01, min(lot, MAX_LOT)), 2)


# ── Backtest core ─────────────────────────────────────────────────────────────

def run_backtest(source: Optional[str] = None):
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    W = 68
    print("\n" + "═" * W)
    print("  XAUUSD GOLD BOT — BACKTEST ENGINE")
    print("  NY Session Only  (London disabled, RSI buy ≥65 / sell ≤35)")
    print("═" * W)
    print("\nFetching data...")

    df, source = download_data(source)

    print(f"\nData source : {source}")
    print(f"Period      : {df.index[0].date()} → {df.index[-1].date()}")
    print(f"Bars loaded : {len(df):,}")

    df["rsi"]   = calc_rsi(df["close"])
    df["ema20"] = calc_ema(df["close"], span=EMA_PERIOD)

    balance    = STARTING_BALANCE
    trades: list[dict] = []
    open_trade: Optional[dict] = None

    dates = df.index.normalize().unique()
    print(f"\nRunning backtest over {len(dates)} trading days...\n")

    for day in dates:
        day_df = df[df.index.normalize() == day]
        if len(day_df) < 5:
            continue

        asian_range  = get_asian_range(day_df)
        london_range = None

        for idx, row in day_df.iterrows():
            hour    = idx.hour
            price   = row["close"]
            rsi_val = row["rsi"]
            ema_val = row["ema20"]

            # ── Manage open trade ──────────────────────────────────────────
            if open_trade:
                sl         = open_trade["sl"]
                tp         = open_trade["tp"]
                direction  = open_trade["direction"]
                lot        = open_trade["lot"]
                hours_open = (idx - open_trade["open_time"]).total_seconds() / 3600
                hit_sl = hit_tp = time_out = False
                close_px = price

                if direction == "BUY":
                    if row["low"] <= sl:
                        hit_sl   = True
                        close_px = sl - SLIPPAGE
                    elif row["high"] >= tp:
                        hit_tp   = True
                        close_px = tp - SLIPPAGE
                else:
                    if row["high"] >= sl:
                        hit_sl   = True
                        close_px = sl + SLIPPAGE
                    elif row["low"] <= tp:
                        hit_tp   = True
                        close_px = tp + SLIPPAGE

                if hours_open >= MAX_TRADE_HOURS and not (hit_sl or hit_tp):
                    time_out = True
                    close_px = price

                if hit_sl or hit_tp or time_out:
                    entry = open_trade["entry"]
                    pnl   = ((close_px - entry) if direction == "BUY"
                             else (entry - close_px)) * lot * 100
                    pnl     = round(pnl, 2)
                    balance += pnl
                    open_trade.update({
                        "close_price":   close_px,
                        "close_time":    idx,
                        "pnl":           pnl,
                        "close_reason":  "tp" if hit_tp else ("sl" if hit_sl else "timeout"),
                        "balance_after": balance,
                    })
                    trades.append(open_trade)
                    open_trade = None
                    continue

            if open_trade:
                continue

            trend = ("bullish" if price > ema_val * 1.001
                     else "bearish" if price < ema_val * 0.999
                     else "neutral")

            # ── London session — entries disabled, NY only ────────────────
            # (asian_range still computed above for get_london_range context)

            # ── NY breakout ────────────────────────────────────────────────
            if NY_START <= hour < NY_END:
                if london_range is None:
                    london_range = get_london_range(day_df)
                if london_range:
                    ask = price + SPREAD / 2 + SLIPPAGE
                    bid = price - SPREAD / 2 - SLIPPAGE

                    if ask > london_range["high"] + BREAKOUT_BUFFER \
                            and rsi_val > RSI_BUY and trend != "bearish":
                        sl      = london_range["low"] - BREAKOUT_BUFFER
                        sl_dist = ask - sl
                        tp      = ask + sl_dist * RR_RATIO
                        open_trade = {
                            "direction": "BUY", "entry": ask, "sl": sl, "tp": tp,
                            "lot": calc_lot_size(balance, sl_dist), "open_time": idx,
                            "session": "NY", "rsi": round(rsi_val, 1), "range": 0,
                        }

                    elif bid < london_range["low"] - BREAKOUT_BUFFER \
                            and rsi_val < RSI_SELL and trend != "bullish":
                        sl      = london_range["high"] + BREAKOUT_BUFFER
                        sl_dist = sl - bid
                        tp      = bid - sl_dist * RR_RATIO
                        open_trade = {
                            "direction": "SELL", "entry": bid, "sl": sl, "tp": tp,
                            "lot": calc_lot_size(balance, sl_dist), "open_time": idx,
                            "session": "NY", "rsi": round(rsi_val, 1), "range": 0,
                        }

    # Close any trade still open at end of data
    if open_trade:
        open_trade.update({
            "close_price":   float(df["close"].iloc[-1]),
            "close_time":    df.index[-1],
            "pnl":           0.0,
            "close_reason":  "end_of_data",
            "balance_after": balance,
        })
        trades.append(open_trade)

    _print_report(trades, STARTING_BALANCE, balance, df, source)


# ── Stats helpers ─────────────────────────────────────────────────────────────

def _session_stats(trades: list, start_balance: float) -> dict:
    total = len(trades)
    if total == 0:
        return {"total": 0, "wr": 0.0, "pf": 0.0, "avg_win": 0.0,
                "avg_loss": 0.0, "max_dd": 0.0, "net_pnl": 0.0, "net_pnl_pct": 0.0}

    wins         = [t for t in trades if t.get("pnl", 0) > 0]
    losses       = [t for t in trades if t.get("pnl", 0) <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    net_pnl      = sum(t.get("pnl", 0) for t in trades)

    # Drawdown: run this session's trades chronologically from start_balance
    chron   = sorted(trades, key=lambda x: x.get("close_time", datetime.min))
    peak    = start_balance
    max_dd  = 0.0
    running = start_balance
    for t in chron:
        running += t.get("pnl", 0)
        if running > peak:
            peak = running
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak)

    return {
        "total":       total,
        "wr":          len(wins) / total * 100,
        "pf":          gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "avg_win":     gross_profit / len(wins)   if wins   else 0.0,
        "avg_loss":    gross_loss   / len(losses) if losses else 0.0,
        "max_dd":      max_dd,
        "net_pnl":     net_pnl,
        "net_pnl_pct": net_pnl / start_balance * 100,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def _print_report(trades: list, start_balance: float, end_balance: float,
                  df: pd.DataFrame, source: str) -> dict:
    W = 68

    if not trades:
        print("No trades generated — check data source coverage and strategy parameters.")
        return {}

    london_trades = [t for t in trades if t.get("session") == "LONDON"]
    ny_trades     = [t for t in trades if t.get("session") == "NY"]

    combined = _session_stats(trades,        start_balance)
    london   = _session_stats(london_trades, start_balance)
    ny       = _session_stats(ny_trades,     start_balance)

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

    months_count   = max(len(monthly), 1)
    monthly_return = ((end_balance / start_balance) ** (1 / months_count) - 1) * 100

    def pf_str(v: float) -> str:
        return f"{v:.2f}" if v != float("inf") else "∞"

    def dd_str(v: float) -> str:
        return f"{v:.1%}"

    # ── Header ────────────────────────────────────────────────────────────────
    print("\n" + "═" * W)
    print("  XAUUSD GOLD BOT — BACKTEST RESULTS")
    print("  NY Session Only  (London disabled, RSI buy ≥65 / sell ≤35)")
    print("═" * W)
    print(f"  Data source    : {source}")
    print(f"  Period         : {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Bars           : {len(df):,}")
    print(f"  Starting       : ${start_balance:,.2f}")
    print(f"  Final          : ${end_balance:,.2f}")
    print(f"  Net P&L        : ${combined['net_pnl']:+,.2f}  ({combined['net_pnl_pct']:+.1f}%)")
    print(f"  Monthly avg    : {monthly_return:+.2f}%")
    print(f"  Assumptions    : spread ${SPREAD:.2f}  |  slippage ${SLIPPAGE:.2f}  |  risk 1%/trade")
    print("─" * W)

    # ── Session table ─────────────────────────────────────────────────────────
    C, L, N = 13, 13, 13
    print(f"  {'':22}  {'COMBINED':>{C}}  {'LONDON':>{L}}  {'NY':>{N}}")
    print(f"  {'':22}  {'07–10 UTC + 13–16 UTC':>{C+2+L+2+N}}")
    print("  " + "─" * (W - 2))

    rows = [
        ("Total Trades",
         str(combined["total"]),      str(london["total"]),      str(ny["total"])),
        ("Win Rate",
         f"{combined['wr']:.1f}%",    f"{london['wr']:.1f}%",    f"{ny['wr']:.1f}%"),
        ("Profit Factor",
         pf_str(combined["pf"]),      pf_str(london["pf"]),      pf_str(ny["pf"])),
        ("Avg Win  $",
         f"${combined['avg_win']:.2f}",
         f"${london['avg_win']:.2f}",
         f"${ny['avg_win']:.2f}"),
        ("Avg Loss $",
         f"${combined['avg_loss']:.2f}",
         f"${london['avg_loss']:.2f}",
         f"${ny['avg_loss']:.2f}"),
        ("Max Drawdown",
         dd_str(combined["max_dd"]),  dd_str(london["max_dd"]),  dd_str(ny["max_dd"])),
        ("Net P&L",
         f"{combined['net_pnl_pct']:+.1f}%",
         f"{london['net_pnl_pct']:+.1f}%",
         f"{ny['net_pnl_pct']:+.1f}%"),
    ]

    for label, c, lv, nv in rows:
        print(f"  {label:<22}  {c:>{C}}  {lv:>{L}}  {nv:>{N}}")

    # ── Monthly breakdown ─────────────────────────────────────────────────────
    print("─" * W)
    print("  MONTHLY BREAKDOWN")
    print(f"  {'Month':<10}  {'Trades':>7}  {'WR':>7}  {'P&L':>11}  {'Balance':>12}")
    print("  " + "─" * (W - 2))
    running_bal = start_balance
    for month in sorted(monthly.keys()):
        m           = monthly[month]
        wr          = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0.0
        running_bal += m["pnl"]
        print(f"  {month:<10}  {m['trades']:>7}  {wr:>6.0f}%  "
              f"{m['pnl']:>+11.2f}  ${running_bal:>11,.2f}")

    # ── Verdict ───────────────────────────────────────────────────────────────
    print("═" * W)
    wr_ok  = combined["wr"]     >= 45.0
    dd_ok  = combined["max_dd"] <= 0.08
    passed = wr_ok and dd_ok
    verdict = ("PASS ✓  Safe to deploy for paper trading"
               if passed else
               "REVIEW ✗  Check parameters before deploying")
    print(f"\n  Verdict  : {verdict}")
    print(f"  Threshold: Win Rate ≥45%  (got {combined['wr']:.1f}%)  |  "
          f"Max Drawdown ≤8%  (got {combined['max_dd']:.1%})")

    # ── NY comparison: previous (RSI 60/40, London+NY) vs new (RSI 65/35, NY only)
    p = _PREV_NY
    n = ny
    print("─" * W)
    print("  NY SESSION  —  BEFORE vs AFTER  (RSI 60/40, London+NY  →  RSI 65/35, NY only)")
    print(f"  {'':22}  {'BEFORE':>12}  {'AFTER':>12}  {'CHANGE':>12}")
    print("  " + "─" * (W - 2))

    def _delta_str(new, old, fmt=".1f", unit=""):
        d = new - old
        sign = "+" if d >= 0 else ""
        return f"{sign}{d:{fmt}}{unit}"

    cmp_rows = [
        ("Total Trades",
         str(p["total"]), str(n["total"]),
         _delta_str(n["total"], p["total"], ".0f")),
        ("Win Rate",
         f"{p['wr']:.1f}%", f"{n['wr']:.1f}%",
         _delta_str(n["wr"], p["wr"], ".1f", " pp")),
        ("Profit Factor",
         f"{p['pf']:.2f}", pf_str(n["pf"]),
         _delta_str(n["pf"] if n["pf"] != float("inf") else p["pf"], p["pf"], ".2f")),
        ("Avg Win  $",
         f"${p['avg_win']:.2f}", f"${n['avg_win']:.2f}",
         _delta_str(n["avg_win"], p["avg_win"], ".2f", "")),
        ("Avg Loss $",
         f"${p['avg_loss']:.2f}", f"${n['avg_loss']:.2f}",
         _delta_str(n["avg_loss"], p["avg_loss"], ".2f", "")),
        ("Max Drawdown",
         f"{p['max_dd']:.1%}", f"{n['max_dd']:.1%}",
         _delta_str(n["max_dd"] * 100, p["max_dd"] * 100, ".1f", " pp")),
        ("Net P&L",
         f"+{p['net_pnl_pct']:.1f}%", f"{n['net_pnl_pct']:+.1f}%",
         _delta_str(n["net_pnl_pct"], p["net_pnl_pct"], ".1f", " pp")),
    ]
    for label, bv, av, dv in cmp_rows:
        print(f"  {label:<22}  {bv:>12}  {av:>12}  {dv:>12}")

    print("═" * W + "\n")

    return {"combined": combined, "london": london, "ny": ny, "monthly": monthly}


if __name__ == "__main__":
    run_backtest()
