"""
XAUUSD Backtest Engine
Run: python backtest/backtest_engine.py
     python main.py --backtest

Strategy (v6):
  - London session (07:00–10:00 UTC) — Asian range breakout
      H4+Daily must BOTH agree (strictly bullish/bearish)
  - NY session     (13:00–16:00 UTC) — London range breakout
      H4+Daily must BOTH agree (strictly bullish/bearish)
  - Fixed $0.20 breakout buffer
  - SL at opposite range boundary minus buffer
  - Single TP at 2R
  - Breakeven SL move at 50% to TP (via check_breakeven in live bot)
  - H4 + Daily EMA20 trend filter (both must agree)
  - ATR computed and logged per-trade (not used for SL/entry)
  - RSI 65/35 thresholds
  - 1.0% risk per trade

Data source: yfinance GC=F (Gold futures, 24/7, UTC-normalised)
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
SPREAD            = 0.20
SLIPPAGE          = 0.10
STARTING_BALANCE  = 10_000.0
RISK_PER_TRADE    = 0.01           # 1.0%
RR_RATIO          = 2.0
RSI_PERIOD        = 14
RSI_BUY           = 65.0
RSI_SELL          = 35.0
EMA_PERIOD        = 20
ATR_PERIOD        = 14
BREAKOUT_BUFFER    = 0.20           # fixed dollar buffer (not ATR-based)
MONTHLY_LOSS_LIMIT = 0.04           # halt if monthly loss exceeds 4% of month-start balance
MIN_RANGE         = 8.0
MAX_RANGE         = 100.0
PIP_SIZE          = 0.01
PIP_VALUE         = 1.0            # $1 per pip per lot
MAX_LOT           = 1.0
MAX_TRADE_HOURS   = 20

LONDON_START = 7
LONDON_END   = 10
NY_START     = 13
NY_END       = 16
ASIAN_END    = 7

ALPACA_DATA_URL = "https://data.alpaca.markets"


# ── Data download ─────────────────────────────────────────────────────────────

def _load_alpaca_keys() -> tuple[str, str]:
    api_key = os.environ.get("ALPACA_API_KEY", "")
    secret  = os.environ.get("ALPACA_SECRET_KEY", "")
    if api_key and secret:
        return api_key, secret
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
        load_dotenv(env_path, override=False)
        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret  = os.environ.get("ALPACA_SECRET_KEY", "")
    except ImportError:
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
    all_bars   = []
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
        data       = resp.json()
        bars       = data.get("bars", {}).get(bars_key, []) if bars_key else data.get("bars", [])
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
        df     = ticker.history(period="2y", interval="1h")
        if df.empty:
            print("  yfinance GC=F: empty response")
            return None
        df.columns = [c.lower() for c in df.columns]
        # Convert to UTC before stripping timezone (yfinance uses America/New_York)
        if df.index.tzinfo is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df = df[~df.index.duplicated(keep="first")].sort_index()
        print(f"  yfinance GC=F OK: {len(df):,} bars "
              f"({df.index[0].date()} → {df.index[-1].date()})")
        return df
    except Exception as e:
        print(f"  yfinance failed: {e}")
        return None


def download_data(source: Optional[str] = None) -> tuple[pd.DataFrame, str]:
    """
    Returns (dataframe, source_label).

    source=None or "yfinance" — yfinance GC=F (default)
    source="alpaca"           — Alpaca XAUUSD → Alpaca GLD only
    """
    if source in (None, "yfinance"):
        df = _download_yfinance()
        if df is not None and len(df) > 100:
            return df, "yfinance GC=F (Gold futures, 24/7)"
        if source == "yfinance":
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

    raise ValueError("All data sources failed — cannot run backtest")


# ── Indicators ────────────────────────────────────────────────────────────────

def calc_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta    = series.diff()
    gain     = delta.where(delta > 0, 0.0)
    loss     = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = avg_loss.where(avg_loss != 0, 0.0001)
    rs       = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def calc_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high  = df["high"]
    low   = df["low"]
    close = df["close"].shift(1)
    tr    = pd.concat(
        [high - low, (high - close).abs(), (low - close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(span=period, min_periods=period).mean()


def _build_trend_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Pre-compute H4 and Daily EMA20 values aligned to the H1 index.
    Adds columns: h4_close, h4_ema20, d_close, d_ema20.
    """
    df = df.copy()

    df_h4 = df.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    df_h4["ema20"] = df_h4["close"].ewm(span=EMA_PERIOD).mean()
    df["h4_close"] = df_h4["close"].reindex(df.index, method="ffill")
    df["h4_ema20"] = df_h4["ema20"].reindex(df.index, method="ffill")

    df_d = df.resample("1d", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    df_d["ema20"] = df_d["close"].ewm(span=EMA_PERIOD).mean()
    df["d_close"] = df_d["close"].reindex(df.index, method="ffill")
    df["d_ema20"] = df_d["ema20"].reindex(df.index, method="ffill")

    return df


def _trend_from_row(row) -> str:
    """Derive H4+Daily trend from pre-computed columns in a single H1 row."""
    h4_c = row.get("h4_close")
    h4_e = row.get("h4_ema20")
    d_c  = row.get("d_close")
    d_e  = row.get("d_ema20")

    if any(v is None or (isinstance(v, float) and pd.isna(v))
           for v in [h4_c, h4_e, d_c, d_e]):
        return "neutral"

    h4_trend = ("bullish" if h4_c > h4_e * 1.001
                else "bearish" if h4_c < h4_e * 0.999
                else "neutral")
    d_trend  = ("bullish" if d_c  > d_e  * 1.001
                else "bearish" if d_c  < d_e  * 0.999
                else "neutral")

    if h4_trend == "bullish" and d_trend == "bullish":
        return "bullish"
    if h4_trend == "bearish" and d_trend == "bearish":
        return "bearish"
    return "neutral"


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
    print("  XAUUSD GOLD BOT — BACKTEST ENGINE  (v6)")
    print("  London+NY  |  fixed $0.20 buffer  |  2R TP  |  H4+Daily trend  |  RSI 65/35  |  1% risk  |  4% CB")
    print("═" * W)
    print("\nFetching data...")

    df, source_label = download_data(source)

    print(f"\nData source : {source_label}")
    print(f"Period      : {df.index[0].date()} → {df.index[-1].date()}")
    print(f"Bars loaded : {len(df):,}")
    print("\nComputing indicators...")

    df["rsi"]   = calc_rsi(df["close"])
    df["atr14"] = calc_atr(df)
    df = _build_trend_columns(df)

    balance    = STARTING_BALANCE
    trades: list[dict] = []
    open_trade: Optional[dict] = None

    # Monthly circuit breaker state
    cb_month_key         = ""
    cb_monthly_loss      = 0.0
    cb_monthly_halted    = False
    cb_monthly_start_bal = balance
    cb_halted_months: list[str] = []

    dates = df.index.normalize().unique()
    print(f"Running backtest over {len(dates)} trading days...\n")

    for day in dates:
        # ── Monthly CB rollover ────────────────────────────────────────────────
        day_key = day.strftime("%Y-%m")
        if day_key != cb_month_key:
            cb_month_key         = day_key
            cb_monthly_loss      = 0.0
            cb_monthly_halted    = False
            cb_monthly_start_bal = balance

        day_df = df[df.index.normalize() == day]
        if len(day_df) < 5:
            continue

        asian_range  = get_asian_range(day_df)
        london_range = None

        for idx, row in day_df.iterrows():
            hour    = idx.hour
            price   = float(row["close"])
            rsi_val = float(row["rsi"])   if not pd.isna(row["rsi"])   else 50.0
            atr_val = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0.0

            # ── Manage open trade ──────────────────────────────────────────────
            if open_trade:
                sl        = open_trade["sl"]
                tp        = open_trade["tp"]
                direction = open_trade["direction"]
                lot       = open_trade["lot"]
                hours_open = (idx - open_trade["open_time"]).total_seconds() / 3600
                hit_sl = hit_tp = time_out = False
                close_px = price

                if direction == "BUY":
                    if float(row["low"]) <= sl:
                        hit_sl   = True
                        close_px = sl - SLIPPAGE
                    elif float(row["high"]) >= tp:
                        hit_tp   = True
                        close_px = tp - SLIPPAGE
                else:
                    if float(row["high"]) >= sl:
                        hit_sl   = True
                        close_px = sl + SLIPPAGE
                    elif float(row["low"]) <= tp:
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
                    # Monthly CB: accumulate loss and trigger halt if limit hit
                    if pnl < 0 and not cb_monthly_halted:
                        cb_monthly_loss += abs(pnl)
                        if cb_monthly_loss >= cb_monthly_start_bal * MONTHLY_LOSS_LIMIT:
                            cb_monthly_halted = True
                            cb_halted_months.append(cb_month_key)
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

            # ── LONDON SESSION (07:00–10:00 UTC) — Asian range breakout ────────
            if LONDON_START <= hour < LONDON_END:
                if cb_monthly_halted:
                    continue
                if not asian_range or not asian_range["valid"]:
                    continue

                trend = _trend_from_row(row)
                ask   = price + SPREAD / 2 + SLIPPAGE
                bid   = price - SPREAD / 2 - SLIPPAGE

                if ask > asian_range["high"] + BREAKOUT_BUFFER \
                        and rsi_val > RSI_BUY and trend == "bullish":
                    entry   = ask
                    sl      = asian_range["low"] - BREAKOUT_BUFFER
                    sl_dist = entry - sl
                    tp      = entry + sl_dist * RR_RATIO
                    lot     = calc_lot_size(balance, sl_dist)
                    open_trade = {
                        "direction": "BUY",  "entry": entry, "sl": sl, "tp": tp,
                        "lot": lot, "open_time": idx, "session": "LONDON",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, 2),
                        "range": round(asian_range["high"] - asian_range["low"], 2),
                    }

                elif bid < asian_range["low"] - BREAKOUT_BUFFER \
                        and rsi_val < RSI_SELL and trend == "bearish":
                    entry   = bid
                    sl      = asian_range["high"] + BREAKOUT_BUFFER
                    sl_dist = sl - entry
                    tp      = entry - sl_dist * RR_RATIO
                    lot     = calc_lot_size(balance, sl_dist)
                    open_trade = {
                        "direction": "SELL", "entry": entry, "sl": sl, "tp": tp,
                        "lot": lot, "open_time": idx, "session": "LONDON",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, 2),
                        "range": round(asian_range["high"] - asian_range["low"], 2),
                    }

            # ── NY SESSION (13:00–16:00 UTC) — London range breakout ─────────────
            elif NY_START <= hour < NY_END:
                if cb_monthly_halted:
                    continue
                if london_range is None:
                    london_range = get_london_range(day_df)
                if not london_range:
                    continue

                trend = _trend_from_row(row)
                ask   = price + SPREAD / 2 + SLIPPAGE
                bid   = price - SPREAD / 2 - SLIPPAGE

                if ask > london_range["high"] + BREAKOUT_BUFFER \
                        and rsi_val > RSI_BUY and trend == "bullish":
                    entry   = ask
                    sl      = london_range["low"] - BREAKOUT_BUFFER
                    sl_dist = entry - sl
                    tp      = entry + sl_dist * RR_RATIO
                    lot     = calc_lot_size(balance, sl_dist)
                    open_trade = {
                        "direction": "BUY", "entry": entry, "sl": sl, "tp": tp,
                        "lot": lot, "open_time": idx, "session": "NY",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, 2),
                        "range": round(london_range["high"] - london_range["low"], 2),
                    }

                elif bid < london_range["low"] - BREAKOUT_BUFFER \
                        and rsi_val < RSI_SELL and trend == "bearish":
                    entry   = bid
                    sl      = london_range["high"] + BREAKOUT_BUFFER
                    sl_dist = sl - entry
                    tp      = entry - sl_dist * RR_RATIO
                    lot     = calc_lot_size(balance, sl_dist)
                    open_trade = {
                        "direction": "SELL", "entry": entry, "sl": sl, "tp": tp,
                        "lot": lot, "open_time": idx, "session": "NY",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, 2),
                        "range": round(london_range["high"] - london_range["low"], 2),
                    }

    # Close any trade still open at end of data
    if open_trade:
        entry     = open_trade["entry"]
        direction = open_trade["direction"]
        lot       = open_trade["lot"]
        last_px   = float(df["close"].iloc[-1])
        pnl       = ((last_px - entry) if direction == "BUY"
                     else (entry - last_px)) * lot * 100
        pnl = round(pnl, 2)
        balance += pnl
        open_trade.update({
            "close_price":   last_px,
            "close_time":    df.index[-1],
            "pnl":           pnl,
            "close_reason":  "end_of_data",
            "balance_after": balance,
        })
        trades.append(open_trade)

    _print_report(trades, STARTING_BALANCE, balance, df, source_label, cb_halted_months)


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
                  df: pd.DataFrame, source: str,
                  cb_halted_months: Optional[list] = None) -> dict:
    W = 74

    if not trades:
        print("No trades generated — check data source coverage and strategy parameters.")
        return {}

    london_trades = [t for t in trades if t.get("session") == "LONDON"]
    ny_trades     = [t for t in trades if t.get("session") == "NY"]

    stats_all    = _session_stats(trades,        start_balance)
    stats_london = _session_stats(london_trades, start_balance)
    stats_ny     = _session_stats(ny_trades,     start_balance)

    # ── Monthly accumulators ──────────────────────────────────────────────────
    def _accum(bucket: dict, t: dict) -> None:
        ct = t.get("close_time")
        if not ct:
            return
        key = ct.strftime("%Y-%m") if isinstance(ct, datetime) else str(ct)[:7]
        bucket.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
        bucket[key]["trades"] += 1
        if t.get("pnl", 0) > 0:
            bucket[key]["wins"] += 1
        bucket[key]["pnl"] += t.get("pnl", 0)

    monthly_all: dict    = {}
    monthly_london: dict = {}
    monthly_ny: dict     = {}
    for t in trades:        _accum(monthly_all,    t)
    for t in london_trades: _accum(monthly_london, t)
    for t in ny_trades:     _accum(monthly_ny,     t)

    months_count  = max(len(monthly_all), 1)
    monthly_avg_a = stats_all["net_pnl_pct"]    / months_count
    monthly_avg_l = stats_london["net_pnl_pct"] / months_count
    monthly_avg_n = stats_ny["net_pnl_pct"]     / months_count

    def _best_worst(monthly: dict) -> tuple[str, str]:
        if not monthly:
            return "—", "—"
        best  = max(monthly.items(), key=lambda x: x[1]["pnl"])
        worst = min(monthly.items(), key=lambda x: x[1]["pnl"])
        return (f"{best[0]} {best[1]['pnl']:+.0f}",
                f"{worst[0]} {worst[1]['pnl']:+.0f}")

    best_a, worst_a = _best_worst(monthly_all)
    best_l, worst_l = _best_worst(monthly_london)
    best_n, worst_n = _best_worst(monthly_ny)

    def pf_str(v: float) -> str:
        return f"{v:.2f}" if v != float("inf") else "∞"

    # ── Header ────────────────────────────────────────────────────────────────
    print("\n" + "═" * W)
    print("  XAUUSD GOLD BOT — BACKTEST RESULTS  (v4: London + NY)")
    print("  London+NY  |  fixed $0.20 buffer  |  2R TP  |  H4+Daily trend  |  1% risk")
    print("═" * W)
    print(f"  Data source : {source}")
    print(f"  Period      : {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  Bars        : {len(df):,}")
    print(f"  Starting    : ${start_balance:,.2f}")
    print(f"  Final       : ${end_balance:,.2f}")
    print(f"  Assumptions : spread ${SPREAD:.2f}  |  slippage ${SLIPPAGE:.2f}  |  risk {RISK_PER_TRADE:.0%}/trade")
    if cb_halted_months:
        print(f"  CB Halted   : {len(cb_halted_months)} month(s) → {', '.join(cb_halted_months)}")
    else:
        print(f"  CB Halted   : none")
    print("─" * W)

    # ── 3-column results table ────────────────────────────────────────────────
    MC, CC, LC, NC = 15, 20, 14, 12   # metric | combined | london | ny

    def trow(label: str, vc: str, vl: str, vn: str) -> str:
        return f"│ {label:<{MC-1}}│ {vc:<{CC-1}}│ {vl:<{LC-1}}│ {vn:<{NC-1}}│"

    top    = f"┌{'─'*MC}┬{'─'*CC}┬{'─'*LC}┬{'─'*NC}┐"
    hdr    = f"│{'Metric':^{MC}}│{'Combined':^{CC}}│{'London':^{LC}}│{'NY':^{NC}}│"
    mid    = f"├{'─'*MC}┼{'─'*CC}┼{'─'*LC}┼{'─'*NC}┤"
    bottom = f"└{'─'*MC}┴{'─'*CC}┴{'─'*LC}┴{'─'*NC}┘"

    rows = [
        ("Trades",
         str(stats_all["total"]),
         str(stats_london["total"]),
         str(stats_ny["total"])),
        ("Win Rate",
         f"{stats_all['wr']:.1f}%",
         f"{stats_london['wr']:.1f}%",
         f"{stats_ny['wr']:.1f}%"),
        ("Profit Factor",
         pf_str(stats_all["pf"]),
         pf_str(stats_london["pf"]),
         pf_str(stats_ny["pf"])),
        ("Avg Win $",
         f"${stats_all['avg_win']:.2f}",
         f"${stats_london['avg_win']:.2f}",
         f"${stats_ny['avg_win']:.2f}"),
        ("Avg Loss $",
         f"${stats_all['avg_loss']:.2f}",
         f"${stats_london['avg_loss']:.2f}",
         f"${stats_ny['avg_loss']:.2f}"),
        ("Max Drawdown",
         f"{stats_all['max_dd']:.1%}",
         f"{stats_london['max_dd']:.1%}",
         f"{stats_ny['max_dd']:.1%}"),
        ("Net P&L",
         f"{stats_all['net_pnl_pct']:+.1f}% (${stats_all['net_pnl']:+,.0f})",
         f"{stats_london['net_pnl_pct']:+.1f}%",
         f"{stats_ny['net_pnl_pct']:+.1f}%"),
        ("Monthly Avg",
         f"{monthly_avg_a:+.2f}%",
         f"{monthly_avg_l:+.2f}%",
         f"{monthly_avg_n:+.2f}%"),
        ("Best Month",  best_a,  best_l,  best_n),
        ("Worst Month", worst_a, worst_l, worst_n),
    ]

    print(top)
    print(hdr)
    print(mid)
    for label, vc, vl, vn in rows:
        print(trow(label, vc, vl, vn))
    print(bottom)

    # ── Monthly breakdown ─────────────────────────────────────────────────────
    all_months  = sorted(set(list(monthly_all.keys()) +
                             list(monthly_london.keys()) +
                             list(monthly_ny.keys())))
    print("─" * W)
    print("  MONTHLY BREAKDOWN  (24 months)")
    print(f"  {'Month':<10}  {'Combined':>10}  {'London':>10}  {'NY':>10}  "
          f"{'Trades':>7}  {'Balance':>12}")
    print("  " + "─" * (W - 2))
    running_bal = start_balance
    for month in all_months:
        ma = monthly_all.get(month,    {"trades": 0, "wins": 0, "pnl": 0.0})
        ml = monthly_london.get(month, {"trades": 0, "wins": 0, "pnl": 0.0})
        mn = monthly_ny.get(month,     {"trades": 0, "wins": 0, "pnl": 0.0})
        running_bal += ma["pnl"]
        print(f"  {month:<10}  {ma['pnl']:>+10.2f}  {ml['pnl']:>+10.2f}  "
              f"{mn['pnl']:>+10.2f}  {ma['trades']:>7}  ${running_bal:>11,.2f}")

    # ── London verdict ────────────────────────────────────────────────────────
    print("═" * W)
    combined_dd = stats_all["max_dd"]
    wr_ok       = stats_all["wr"]  >= 45.0
    dd_ok       = combined_dd      <= 0.08

    print(f"\n  Combined DD : {combined_dd:.1%}  (threshold ≤8.0%)")
    print(f"  Win Rate    : {stats_all['wr']:.1f}%  (threshold ≥45%)")

    if dd_ok:
        print("  London      : ENABLED ✓  Combined DD within safe limit")
    else:
        print(f"  London      : DISABLED ✗  Combined DD {combined_dd:.1%} exceeds 8%")
        print("                → Revert London session block in bot/strategy.py to disabled")

    passed  = wr_ok and dd_ok
    verdict = ("PASS ✓  Safe to run both sessions"
               if passed else
               "REVIEW ✗  Check parameters or disable London")
    print(f"\n  Verdict     : {verdict}")
    print("═" * W + "\n")

    return {
        "stats": stats_all, "monthly": monthly_all,
        "stats_london": stats_london, "stats_ny": stats_ny,
    }


if __name__ == "__main__":
    run_backtest()
