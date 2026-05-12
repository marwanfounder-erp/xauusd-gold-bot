import os
from typing import Optional
from pydantic_settings import BaseSettings
from pydantic import field_validator


class Settings(BaseSettings):
    # MT5
    mt5_login: Optional[int] = None
    mt5_password: str = ""
    mt5_server: str = ""

    # Telegram (separate bot token for Gold)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Trading
    symbol: str = "XAUUSD"
    risk_per_trade: float = 0.01       # 1%
    max_daily_loss: float = 0.04       # 4%
    max_total_drawdown: float = 0.07   # 7%
    max_lot_size: float = 1.0
    max_open_positions: int = 1

    # Gold specific
    pip_size: float = 0.01             # Gold pip = $0.01
    pip_value: float = 1.0             # $1 per pip per lot
    breakout_buffer_dollars: float = 0.20
    min_range_dollars: float = 3.0
    max_range_dollars: float = 15.0

    # Strategy
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
    friday_close_hour_utc: int = 21
    news_filter_before_minutes: int = 30
    news_filter_after_minutes: int = 60
    order_filling_mode: int = 0        # FOK
    order_magic_id: int = 303030       # Different from EURUSD bot
    breakeven_trigger_ratio: float = 0.5   # move SL to BE at 50% to TP
    max_trade_hours: int = 20              # close after 20 hours

    # News — Finnhub
    finnhub_api_key: str = ""

    # Database
    database_url: str = ""
    db_table_prefix: str = "gold_"

    # Dashboard — Railway injects PORT env var; fall back to 8081 locally
    dashboard_port: int = int(os.environ.get("PORT", 8081))

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
