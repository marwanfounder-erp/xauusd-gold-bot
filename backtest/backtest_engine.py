"""
Multi-Pair London Breakout Backtest Engine
Run: python backtest/backtest_engine.py
     python main.py --backtest

Strategy:
  - London session (07:00–10:00 UTC) — Asian range breakout
  - NY session     (13:00–16:00 UTC) — London range breakout
  - H4 + Daily EMA20 trend filter (both must agree)
  - RSI 60/40 thresholds
  - 0.5% risk per trade, 2R TP
  - 4% monthly circuit breaker

Pairs: XAUUSD, GBPUSD, USDJPY
Data: yfinance
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

# ── Strategy constants ────────────────────────────────────────────────────────
STARTING_BALANCE   = 10_000.0
RISK_PER_TRADE     = 0.005          # 0.5%
RR_RATIO           = 2.0
RSI_BUY            = 60.0
RSI_SELL           = 40.0
EMA_PERIOD         = 20
ATR_PERIOD         = 14
RSI_PERIOD         = 14
MONTHLY_LOSS_LIMIT = 0.04
MAX_TRADE_HOURS    = 20
SLIPPAGE_PCT       = 0.0001         # 0.01% slippage as fraction of price

LONDON_START = 7
LONDON_END   = 10
NY_START     = 13
NY_END       = 16
ASIAN_END    = 7

# ── Per-pair configuration ────────────────────────────────────────────────────
PAIR_CONFIGS = {
    'XAUUSD': {
        'pip_size':     0.01,
        'pip_value':    1.0,
        'min_range':    300,     # pips (= $3.00 min Asian range)
        'max_range':    1500,    # pips (= $15.00 max Asian range)
        'buffer':       20,      # pips (= $0.20 breakout buffer)
        'spread':       30,      # pips (= $0.30 spread)
        'yahoo_symbol': 'GC=F',
        'max_lot':      1.0,
        'price_dec':    2,
    },
    'GBPUSD': {
        'pip_size':     0.0001,
        'pip_value':    10.0,
        'min_range':    15,      # pips
        'max_range':    60,      # pips
        'buffer':       2,       # pips
        'spread':       1.5,     # pips
        'yahoo_symbol': 'GBPUSD=X',
        'max_lot':      1.0,
        'price_dec':    5,
    },
    'USDJPY': {
        'pip_size':     0.01,
        'pip_value':    9.0,     # approx $9 per pip
        'min_range':    15,      # pips
        'max_range':    60,      # pips
        'buffer':       2,       # pips
        'spread':       1.5,     # pips
        'yahoo_symbol': 'JPY=X',
        'max_lot':      1.0,
        'price_dec':    3,
    },
}

ALPACA_DATA_URL = "https://data.alpaca.markets"


# ── Data download ─────────────────────────────────────────────────────────────

def _download_yfinance(symbol: str, yahoo_sym: str) -> Optional[pd.DataFrame]:
    print(f"  Trying yfinance {yahoo_sym} for {symbol}...")
    try:
        import yfinance as yf
        ticker = yf.Ticker(yahoo_sym)
        df     = ticker.history(period="2y", interval="1h")
        if df.empty:
            print(f"  yfinance {yahoo_sym}: empty response")
            return None
        df.columns = [c.lower() for c in df.columns]
        if df.index.tzinfo is not None:
            df.index = df.index.tz_convert("UTC").tz_localize(None)
        df = df[["open", "high", "low", "close", "volume"]].dropna()
        df = df[~df.index.duplicated(keep="first")].sort_index()
        print(f"  {symbol} OK: {len(df):,} bars "
              f"({df.index[0].date()} → {df.index[-1].date()})")
        return df
    except Exception as e:
        print(f"  yfinance {yahoo_sym} failed: {e}")
        return None


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


def _download_alpaca_xauusd() -> Optional[pd.DataFrame]:
    print("  Trying Alpaca XAUUSD (forex v1beta3)...")
    try:
        api_key, secret = _load_alpaca_keys()
        if not api_key:
            print("  Alpaca: no API key")
            return None
        headers  = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret}
        end_dt   = datetime.utcnow()
        start_dt = end_dt - timedelta(days=730)
        params   = {
            "symbols":   "XAUUSD", "timeframe": "1Hour",
            "start":     start_dt.strftime("%Y-%m-%dT00:00:00Z"),
            "end":       end_dt.strftime("%Y-%m-%dT00:00:00Z"),
            "limit":     1000, "sort": "asc",
        }
        all_bars = []
        page_token = None
        while True:
            p = {**params}
            if page_token:
                p["page_token"] = page_token
            resp = requests.get(
                f"{ALPACA_DATA_URL}/v1beta3/forex/bars",
                headers=headers, params=p, timeout=30,
            )
            if resp.status_code != 200:
                raise ValueError(f"Alpaca HTTP {resp.status_code}")
            data       = resp.json()
            bars       = data.get("bars", {}).get("XAUUSD", [])
            all_bars.extend(bars)
            page_token = data.get("next_page_token")
            if not page_token:
                break

        if not all_bars:
            return None
        df = pd.DataFrame(all_bars)
        df["t"] = pd.to_datetime(df["t"], utc=True)
        df = df.set_index("t")
        df.index = df.index.tz_localize(None)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        print(f"  Alpaca XAUUSD OK: {len(df):,} bars")
        return df
    except Exception as e:
        print(f"  Alpaca XAUUSD failed: {e}")
        return None


def download_data(symbol: str, source: Optional[str] = None) -> tuple[pd.DataFrame, str]:
    cfg      = PAIR_CONFIGS[symbol]
    yahoo_sym = cfg['yahoo_symbol']

    df = _download_yfinance(symbol, yahoo_sym)
    if df is not None and len(df) > 100:
        return df, f"yfinance {yahoo_sym}"

    if symbol == "XAUUSD" and source != "yfinance":
        df = _download_alpaca_xauusd()
        if df is not None and len(df) > 100:
            return df, "Alpaca XAUUSD (forex)"

    raise ValueError(f"All data sources failed for {symbol}")


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
    h4_c = row.get("h4_close")
    h4_e = row.get("h4_ema20")
    d_c  = row.get("d_close")
    d_e  = row.get("d_ema20")

    if any(v is None or (isinstance(v, float) and pd.isna(v))
           for v in [h4_c, h4_e, d_c, d_e]):
        return "neutral"

    h4_t = ("bullish" if h4_c > h4_e * 1.001 else
             "bearish" if h4_c < h4_e * 0.999 else "neutral")
    d_t  = ("bullish" if d_c  > d_e  * 1.001 else
             "bearish" if d_c  < d_e  * 0.999 else "neutral")

    if h4_t == "bullish" and d_t == "bullish":
        return "bullish"
    if h4_t == "bearish" and d_t == "bearish":
        return "bearish"
    return "neutral"


# ── Range helpers ─────────────────────────────────────────────────────────────

def get_asian_range(day_df: pd.DataFrame, pip_size: float,
                    min_range: float, max_range: float) -> Optional[dict]:
    asian = day_df[day_df.index.hour < ASIAN_END]
    if len(asian) < 3:
        return None
    high       = float(asian["high"].max())
    low        = float(asian["low"].min())
    range_pips = (high - low) / pip_size
    return {
        "high":       high,
        "low":        low,
        "range_pips": range_pips,
        "valid":      min_range <= range_pips <= max_range,
    }


def get_london_range(day_df: pd.DataFrame) -> Optional[dict]:
    mask   = (day_df.index.hour >= LONDON_START) & (day_df.index.hour < LONDON_END)
    london = day_df[mask]
    if len(london) < 1:
        return None
    return {"high": float(london["high"].max()), "low": float(london["low"].min())}


# ── Lot size ──────────────────────────────────────────────────────────────────

def calc_lot_size(balance: float, sl_pips: float, pip_value: float,
                  max_lot: float = 1.0) -> float:
    risk_amount = balance * RISK_PER_TRADE
    if sl_pips <= 0:
        return 0.01
    lot = risk_amount / (sl_pips * pip_value)
    return round(max(0.01, min(lot, max_lot)), 2)


# ── Single-pair backtest ──────────────────────────────────────────────────────

def run_pair_backtest(symbol: str, df: pd.DataFrame) -> list[dict]:
    """Run London+NY breakout strategy on one pair's H1 data. Returns trade list."""
    cfg      = PAIR_CONFIGS[symbol]
    pip_size = cfg['pip_size']
    pip_val  = cfg['pip_value']
    spread_p = cfg['spread'] * pip_size
    buffer_p = cfg['buffer'] * pip_size
    max_lot  = cfg['max_lot']

    df["rsi"]   = calc_rsi(df["close"])
    df["atr14"] = calc_atr(df)
    df          = _build_trend_columns(df)

    balance    = STARTING_BALANCE
    trades: list[dict] = []
    open_trade: Optional[dict] = None

    cb_month_key         = ""
    cb_monthly_loss      = 0.0
    cb_monthly_halted    = False
    cb_monthly_start_bal = balance
    cb_halted_months: list[str] = []

    dates = df.index.normalize().unique()

    for day in dates:
        day_key = day.strftime("%Y-%m")
        if day_key != cb_month_key:
            cb_month_key         = day_key
            cb_monthly_loss      = 0.0
            cb_monthly_halted    = False
            cb_monthly_start_bal = balance

        day_df = df[df.index.normalize() == day]
        if len(day_df) < 5:
            continue

        asian_range  = get_asian_range(day_df, pip_size, cfg['min_range'], cfg['max_range'])
        london_range = None

        for idx, row in day_df.iterrows():
            hour    = idx.hour
            price   = float(row["close"])
            rsi_val = float(row["rsi"])   if not pd.isna(row["rsi"])   else 50.0
            atr_val = float(row["atr14"]) if not pd.isna(row["atr14"]) else 0.0

            # ── Manage open trade ──────────────────────────────────────────
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
                        close_px = sl - spread_p * 0.5
                    elif float(row["high"]) >= tp:
                        hit_tp   = True
                        close_px = tp - spread_p * 0.5
                else:
                    if float(row["high"]) >= sl:
                        hit_sl   = True
                        close_px = sl + spread_p * 0.5
                    elif float(row["low"]) <= tp:
                        hit_tp   = True
                        close_px = tp + spread_p * 0.5

                if hours_open >= MAX_TRADE_HOURS and not (hit_sl or hit_tp):
                    time_out = True
                    close_px = price

                if hit_sl or hit_tp or time_out:
                    entry = open_trade["entry"]
                    if direction == "BUY":
                        pips = (close_px - entry) / pip_size
                    else:
                        pips = (entry - close_px) / pip_size
                    pnl     = round(pips * pip_val * lot, 2)
                    balance += pnl
                    if pnl < 0 and not cb_monthly_halted:
                        cb_monthly_loss += abs(pnl)
                        if cb_monthly_loss >= cb_monthly_start_bal * MONTHLY_LOSS_LIMIT:
                            cb_monthly_halted = True
                            cb_halted_months.append(cb_month_key)
                    open_trade.update({
                        "close_price":   round(close_px, cfg['price_dec']),
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

            # ── LONDON SESSION — Asian range breakout ──────────────────────
            if LONDON_START <= hour < LONDON_END:
                if cb_monthly_halted or not asian_range or not asian_range["valid"]:
                    continue

                trend = _trend_from_row(row)
                ask   = price + spread_p / 2
                bid   = price - spread_p / 2

                if ask > asian_range["high"] + buffer_p and rsi_val > RSI_BUY and trend == "bullish":
                    entry   = ask
                    sl      = asian_range["low"] - buffer_p
                    sl_dist = entry - sl
                    tp      = entry + sl_dist * RR_RATIO
                    sl_pips = sl_dist / pip_size
                    lot     = calc_lot_size(balance, sl_pips, pip_val, max_lot)
                    open_trade = {
                        "symbol": symbol, "direction": "BUY",
                        "entry": round(entry, cfg['price_dec']),
                        "sl": round(sl, cfg['price_dec']),
                        "tp": round(tp, cfg['price_dec']),
                        "lot": lot, "open_time": idx, "session": "LONDON",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, cfg['price_dec']),
                        "range": round(asian_range["range_pips"], 1),
                    }

                elif bid < asian_range["low"] - buffer_p and rsi_val < RSI_SELL and trend == "bearish":
                    entry   = bid
                    sl      = asian_range["high"] + buffer_p
                    sl_dist = sl - entry
                    tp      = entry - sl_dist * RR_RATIO
                    sl_pips = sl_dist / pip_size
                    lot     = calc_lot_size(balance, sl_pips, pip_val, max_lot)
                    open_trade = {
                        "symbol": symbol, "direction": "SELL",
                        "entry": round(entry, cfg['price_dec']),
                        "sl": round(sl, cfg['price_dec']),
                        "tp": round(tp, cfg['price_dec']),
                        "lot": lot, "open_time": idx, "session": "LONDON",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, cfg['price_dec']),
                        "range": round(asian_range["range_pips"], 1),
                    }

            # ── NY SESSION — London range breakout ─────────────────────────
            elif NY_START <= hour < NY_END:
                if cb_monthly_halted:
                    continue
                if london_range is None:
                    london_range = get_london_range(day_df)
                if not london_range:
                    continue

                trend = _trend_from_row(row)
                ask   = price + spread_p / 2
                bid   = price - spread_p / 2

                if ask > london_range["high"] + buffer_p and rsi_val > RSI_BUY and trend == "bullish":
                    entry   = ask
                    sl      = london_range["low"] - buffer_p
                    sl_dist = entry - sl
                    tp      = entry + sl_dist * RR_RATIO
                    sl_pips = sl_dist / pip_size
                    lot     = calc_lot_size(balance, sl_pips, pip_val, max_lot)
                    open_trade = {
                        "symbol": symbol, "direction": "BUY",
                        "entry": round(entry, cfg['price_dec']),
                        "sl": round(sl, cfg['price_dec']),
                        "tp": round(tp, cfg['price_dec']),
                        "lot": lot, "open_time": idx, "session": "NY",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, cfg['price_dec']),
                        "range": round((london_range["high"] - london_range["low"]) / pip_size, 1),
                    }

                elif bid < london_range["low"] - buffer_p and rsi_val < RSI_SELL and trend == "bearish":
                    entry   = bid
                    sl      = london_range["high"] + buffer_p
                    sl_dist = sl - entry
                    tp      = entry - sl_dist * RR_RATIO
                    sl_pips = sl_dist / pip_size
                    lot     = calc_lot_size(balance, sl_pips, pip_val, max_lot)
                    open_trade = {
                        "symbol": symbol, "direction": "SELL",
                        "entry": round(entry, cfg['price_dec']),
                        "sl": round(sl, cfg['price_dec']),
                        "tp": round(tp, cfg['price_dec']),
                        "lot": lot, "open_time": idx, "session": "NY",
                        "rsi": round(rsi_val, 1), "atr": round(atr_val, cfg['price_dec']),
                        "range": round((london_range["high"] - london_range["low"]) / pip_size, 1),
                    }

    # Close any trade still open at end of data
    if open_trade:
        last_px = float(df["close"].iloc[-1])
        entry   = open_trade["entry"]
        lot     = open_trade["lot"]
        direction = open_trade["direction"]
        if direction == "BUY":
            pips = (last_px - entry) / pip_size
        else:
            pips = (entry - last_px) / pip_size
        pnl = round(pips * pip_val * lot, 2)
        balance += pnl
        open_trade.update({
            "close_price":   round(last_px, cfg['price_dec']),
            "close_time":    df.index[-1],
            "pnl":           pnl,
            "close_reason":  "end_of_data",
            "balance_after": balance,
        })
        trades.append(open_trade)

    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────

def _session_stats(trades: list, start_balance: float) -> dict:
    if not trades:
        return {"total": 0, "wr": 0.0, "pf": 0.0, "avg_win": 0.0,
                "avg_loss": 0.0, "max_dd": 0.0, "net_pnl": 0.0, "net_pnl_pct": 0.0}
    wins         = [t for t in trades if t.get("pnl", 0) > 0]
    losses       = [t for t in trades if t.get("pnl", 0) <= 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss   = abs(sum(t["pnl"] for t in losses))
    net_pnl      = sum(t.get("pnl", 0) for t in trades)
    chron        = sorted(trades, key=lambda x: x.get("close_time", datetime.min))
    peak = running = start_balance
    max_dd = 0.0
    for t in chron:
        running += t.get("pnl", 0)
        if running > peak:
            peak = running
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak)
    return {
        "total":       len(trades),
        "wr":          len(wins) / len(trades) * 100,
        "pf":          gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "avg_win":     gross_profit / len(wins)   if wins   else 0.0,
        "avg_loss":    gross_loss   / len(losses) if losses else 0.0,
        "max_dd":      max_dd,
        "net_pnl":     net_pnl,
        "net_pnl_pct": net_pnl / start_balance * 100,
    }


def pf_str(v: float) -> str:
    return f"{v:.2f}" if v != float("inf") else "∞"


# ── Main backtest ─────────────────────────────────────────────────────────────

def run_backtest(source: Optional[str] = None):
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    W = 74
    print("\n" + "═" * W)
    print("  MULTI-PAIR LONDON BREAKOUT BOT — BACKTEST ENGINE")
    print("  XAUUSD · GBPUSD · USDJPY  |  London+NY  |  0.5% risk  |  2R TP  |  RSI 60/40")
    print("═" * W)

    symbols      = list(PAIR_CONFIGS.keys())
    pair_trades  = {}
    pair_sources = {}
    all_trades   = []

    for symbol in symbols:
        print(f"\n{'─'*W}")
        print(f"  Fetching data: {symbol}")
        try:
            df, src = download_data(symbol, source)
            pair_sources[symbol] = src
            print(f"  Running backtest for {symbol}...")
            trades = run_pair_backtest(symbol, df)
            pair_trades[symbol] = trades
            all_trades.extend(trades)
            stats = _session_stats(trades, STARTING_BALANCE)
            print(
                f"  {symbol}: {stats['total']} trades | "
                f"WR={stats['wr']:.1f}% | "
                f"MaxDD={stats['max_dd']:.1%} | "
                f"Return={stats['net_pnl_pct']:+.1f}%"
            )
        except Exception as e:
            print(f"  {symbol} FAILED: {e}")
            pair_trades[symbol] = []
            pair_sources[symbol] = "failed"

    _print_combined_report(pair_trades, all_trades, pair_sources)
    return pair_trades


def _print_combined_report(pair_trades: dict, all_trades: list, pair_sources: dict):
    W   = 74
    SB  = STARTING_BALANCE

    symbols = list(PAIR_CONFIGS.keys())
    stats   = {sym: _session_stats(pair_trades.get(sym, []), SB) for sym in symbols}
    stats_combined = _session_stats(all_trades, SB * len(symbols))

    months_count = max(
        len({t["close_time"].strftime("%Y-%m")
             for sym in symbols for t in pair_trades.get(sym, [])
             if t.get("close_time")}),
        1
    )

    print("\n" + "═" * W)
    print("  MULTI-PAIR BACKTEST RESULTS")
    print("  London+NY  |  0.5% risk/trade  |  2R TP  |  H4+Daily EMA20  |  RSI 60/40")
    print("═" * W)
    for sym in symbols:
        print(f"  {sym:<8} : {pair_sources.get(sym, '—')}")
    print(f"  Starting balance : ${SB:,.2f} per pair  (${SB * len(symbols):,.2f} combined)")
    print("─" * W)

    # Per-pair table
    hdr = f"  {'Pair':<10}{'Trades':>7}{'WinRate':>9}{'MaxDD':>8}{'NetPnL':>10}{'Return':>9}{'Avg/Mo':>8}"
    print(hdr)
    print("  " + "─" * (W - 2))

    for sym in symbols:
        s = stats[sym]
        mo_avg = s['net_pnl_pct'] / months_count
        end_bal = SB + s['net_pnl']
        print(
            f"  {sym:<10}{s['total']:>7}{s['wr']:>8.1f}%"
            f"{s['max_dd']*100:>7.1f}%  ${s['net_pnl']:>+9,.2f}"
            f"  {s['net_pnl_pct']:>+7.1f}%  {mo_avg:>+6.2f}%"
        )

    # Combined row
    sc = stats_combined
    mo_avg_c = sc['net_pnl_pct'] / months_count
    print("  " + "─" * (W - 2))
    print(
        f"  {'COMBINED':<10}{sc['total']:>7}{sc['wr']:>8.1f}%"
        f"{sc['max_dd']*100:>7.1f}%  ${sc['net_pnl']:>+9,.2f}"
        f"  {sc['net_pnl_pct']:>+7.1f}%  {mo_avg_c:>+6.2f}%"
    )

    # Monthly breakdown (combined)
    monthly: dict = {}
    for t in all_trades:
        ct = t.get("close_time")
        if not ct:
            continue
        key = ct.strftime("%Y-%m") if isinstance(ct, datetime) else str(ct)[:7]
        monthly.setdefault(key, {"trades": 0, "wins": 0, "pnl": 0.0})
        monthly[key]["trades"] += 1
        if t.get("pnl", 0) > 0:
            monthly[key]["wins"] += 1
        monthly[key]["pnl"] += t.get("pnl", 0)

    if monthly:
        print("─" * W)
        print("  COMBINED MONTHLY BREAKDOWN")
        print(f"  {'Month':<10}  {'P&L':>10}  {'Trades':>7}  {'WinRate':>8}")
        print("  " + "─" * (W - 2))
        for month in sorted(monthly):
            m = monthly[month]
            wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
            print(f"  {month:<10}  {m['pnl']:>+10.2f}  {m['trades']:>7}  {wr:>7.1f}%")

    # Verdict
    print("═" * W)
    wr_ok = sc["wr"] >= 45.0
    dd_ok = sc["max_dd"] <= 0.08
    print(f"\n  Combined DD  : {sc['max_dd']:.1%}  (threshold ≤8.0%)")
    print(f"  Win Rate     : {sc['wr']:.1f}%  (threshold ≥45%)")
    verdict = "PASS ✓  Safe to deploy" if (wr_ok and dd_ok) else "REVIEW ✗  Check parameters"
    print(f"  Verdict      : {verdict}")
    print("═" * W + "\n")


if __name__ == "__main__":
    run_backtest()
