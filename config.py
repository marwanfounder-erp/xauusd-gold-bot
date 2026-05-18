import os
from typing import Optional, List
from pydantic_settings import BaseSettings
from pydantic import field_validator


PAIR_SETTINGS = {
    'XAUUSD': {
        'pip_size':   0.01,
        'pip_value':  1.0,
        'min_range':  300,       # pips (300 × $0.01 = $3.00 minimum Asian range)
        'max_range':  1500,      # pips (1500 × $0.01 = $15.00 maximum)
        'buffer':     20,        # pips (20 × $0.01 = $0.20 breakout buffer)
        'yahoo_symbol': 'GC=F',
        'spread':     30,        # pips (30 × $0.01 = $0.30 spread)
    },
    'GBPUSD': {
        'pip_size':   0.0001,
        'pip_value':  10.0,
        'min_range':  15,        # pips
        'max_range':  60,        # pips
        'buffer':     2,         # pips
        'yahoo_symbol': 'GBPUSD=X',
        'spread':     1.5,       # pips
    },
    'USDJPY': {
        'pip_size':   0.01,
        'pip_value':  9.0,       # approx $9 per pip per lot
        'min_range':  15,        # pips
        'max_range':  60,        # pips
        'buffer':     2,         # pips
        'yahoo_symbol': 'JPY=X',
        'spread':     1.5,       # pips
    },
}


class Settings(BaseSettings):
    # MT5
    mt5_login: Optional[int] = None
    mt5_password: str = ""
    mt5_server: str = ""

    # Telegram (separate bot token for Gold)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Multi-pair settings
    symbols: List[str] = ['XAUUSD', 'GBPUSD', 'USDJPY']
    max_total_positions: int = 2
    max_positions_per_pair: int = 1

    # Legacy single-symbol (kept for balance/equity tracking)
    symbol: str = "XAUUSD"

    # Risk
    risk_per_trade: float = 0.005        # 0.5% per trade
    max_daily_loss: float = 0.04         # 4%
    max_total_drawdown: float = 0.07     # 7%
    max_lot_size: float = 1.0
    max_open_positions: int = 2          # matches max_total_positions

    # Gold-specific (backward compat, overridden per-pair by PAIR_SETTINGS)
    pip_size: float = 0.01
    pip_value: float = 1.0
    breakout_buffer_dollars: float = 0.20
    min_range_dollars: float = 3.0
    max_range_dollars: float = 15.0

    # Strategy (shared across all pairs)
    rsi_period: int = 14
    rsi_buy_threshold: float = 60.0
    rsi_sell_threshold: float = 40.0
    ema_period: int = 20
    rr_ratio: float = 2.0

    # Sessions (UTC)
    asian_session_start: int = 0
    asian_session_end: int = 7
    london_session_start: int = 7
    london_session_end: int = 10
    ny_session_start: int = 13
    ny_session_end: int = 16

    # Risk / misc
    monthly_loss_limit: float = 0.04
    friday_close_hour_utc: int = 21
    news_filter_before_minutes: int = 30
    news_filter_after_minutes: int = 60
    order_filling_mode: int = 0
    order_magic_id: int = 303030
    breakeven_trigger_ratio: float = 0.5
    max_trade_hours: int = 20

    # News — Finnhub
    finnhub_api_key: str = ""

    # The5ers compliance
    the5ers_mode: bool = True
    the5ers_news_block_minutes: int = 2

    # Database
    database_url: str = ""
    db_table_prefix: str = "gold_"

    # Dashboard — Railway injects PORT env var; fall back to 8081 locally
    dashboard_port: int = int(os.environ.get("PORT", "8081"))

    @field_validator("dashboard_port", mode="before")
    @classmethod
    def _use_railway_port(cls, v):
        port_env = os.environ.get("PORT")
        if port_env:
            return int(port_env)
        return v

    @field_validator("symbols", mode="before")
    @classmethod
    def _parse_symbols(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
