import logging
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import psycopg2
    import psycopg2.extras
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False
    logger.warning("psycopg2 not installed — database features disabled")


CREATE_TRADES_SQL = """
CREATE TABLE IF NOT EXISTS {prefix}trades (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(10)   NOT NULL DEFAULT 'XAUUSD',
    direction       VARCHAR(4)    NOT NULL,
    lot_size        FLOAT,
    entry_price     FLOAT,
    stop_loss       FLOAT,
    take_profit     FLOAT,
    close_price     FLOAT,
    sl_dollars      FLOAT,
    pnl             FLOAT,
    rsi             FLOAT,
    trend           VARCHAR(10),
    session         VARCHAR(10),
    confidence      VARCHAR(10),
    status          VARCHAR(10)   DEFAULT 'open',
    mode            VARCHAR(10)   DEFAULT 'paper',
    ticket          BIGINT,
    open_time       TIMESTAMPTZ,
    close_time      TIMESTAMPTZ,
    close_reason    VARCHAR(50),
    created_at      TIMESTAMPTZ   DEFAULT NOW()
);
"""

CREATE_EQUITY_SQL = """
CREATE TABLE IF NOT EXISTS {prefix}equity (
    id          SERIAL PRIMARY KEY,
    symbol      VARCHAR(10) NOT NULL DEFAULT 'XAUUSD',
    balance     FLOAT,
    equity      FLOAT,
    drawdown    FLOAT,
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);
"""

CREATE_PAPER_BALANCE_SQL = """
CREATE TABLE IF NOT EXISTS {prefix}paper_balance (
    symbol      VARCHAR(10) PRIMARY KEY,
    balance     FLOAT NOT NULL DEFAULT 5000.0,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

CREATE_BOT_LOGS_SQL = """
CREATE TABLE IF NOT EXISTS {prefix}bot_logs (
    id          SERIAL PRIMARY KEY,
    symbol      VARCHAR(10) NOT NULL DEFAULT 'XAUUSD',
    message     TEXT NOT NULL,
    logged_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS {prefix}bot_logs_symbol_idx ON {prefix}bot_logs (symbol, logged_at DESC);
"""


class Database:
    def __init__(self, config):
        self.config = config
        self.prefix = config.db_table_prefix
        self.conn = None

    def connect(self) -> bool:
        if not PSYCOPG2_AVAILABLE:
            logger.warning("psycopg2 not available — running without DB")
            return False
        if not self.config.database_url:
            logger.warning("DATABASE_URL not set — running without DB")
            return False
        try:
            self.conn = psycopg2.connect(self.config.database_url)
            self.conn.autocommit = True
            self._create_tables()
            logger.info("Database connected")
            return True
        except Exception as e:
            logger.error(f"Database connect error: {e}")
            self.conn = None
            return False

    def _create_tables(self):
        if not self.conn:
            return
        with self.conn.cursor() as cur:
            cur.execute(CREATE_TRADES_SQL.format(prefix=self.prefix))
            cur.execute(CREATE_EQUITY_SQL.format(prefix=self.prefix))
            cur.execute(CREATE_PAPER_BALANCE_SQL.format(prefix=self.prefix))
            cur.execute(CREATE_BOT_LOGS_SQL.format(prefix=self.prefix))

    def save_trade(self, trade: dict) -> Optional[int]:
        if not self.conn:
            return None
        sql = f"""
            INSERT INTO {self.prefix}trades
              (symbol, direction, lot_size, entry_price, stop_loss, take_profit,
               sl_dollars, rsi, trend, session, confidence, status, mode, ticket, open_time)
            VALUES
              (%(symbol)s, %(direction)s, %(lot_size)s, %(entry_price)s, %(stop_loss)s,
               %(take_profit)s, %(sl_dollars)s, %(rsi)s, %(trend)s, %(session)s,
               %(confidence)s, %(status)s, %(mode)s, %(ticket)s, %(open_time)s)
            RETURNING id
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, trade)
                row = cur.fetchone()
                return row[0] if row else None
        except Exception as e:
            logger.error(f"save_trade error: {e}")
            return None

    def close_trade(self, ticket: int, close_price: float, pnl: float, reason: str):
        if not self.conn:
            return
        sql = f"""
            UPDATE {self.prefix}trades
            SET close_price=%(close_price)s, pnl=%(pnl)s,
                close_reason=%(reason)s, close_time=%(close_time)s, status='closed'
            WHERE ticket=%(ticket)s AND symbol=%(symbol)s
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, {
                    "close_price": close_price,
                    "pnl": pnl,
                    "reason": reason,
                    "close_time": datetime.utcnow(),
                    "ticket": ticket,
                    "symbol": self.config.symbol,
                })
        except Exception as e:
            logger.error(f"close_trade error: {e}")

    def get_trades(self, limit: int = 100, status: str = None) -> list:
        if not self.conn:
            return []
        where = f"WHERE symbol='{self.config.symbol}'"
        if status:
            where += f" AND status='{status}'"
        sql = f"""
            SELECT id, symbol, direction, lot_size, entry_price, stop_loss, take_profit,
                   close_price, pnl, session, confidence, status, open_time, close_time,
                   close_reason, rsi, trend
            FROM {self.prefix}trades
            {where}
            ORDER BY open_time DESC LIMIT {limit}
        """
        try:
            with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"get_trades error: {e}")
            return []

    def get_daily_pnl(self, day: date = None) -> float:
        if not self.conn:
            return 0.0
        day = day or date.today()
        sql = f"""
            SELECT COALESCE(SUM(pnl), 0.0) FROM {self.prefix}trades
            WHERE symbol='{self.config.symbol}'
              AND status='closed'
              AND DATE(close_time) = '{day}'
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                row = cur.fetchone()
                return float(row[0]) if row else 0.0
        except Exception as e:
            logger.error(f"get_daily_pnl error: {e}")
            return 0.0

    def save_equity_snapshot(self, balance: float, equity: float, drawdown: float):
        if not self.conn:
            return
        sql = f"""
            INSERT INTO {self.prefix}equity (symbol, balance, equity, drawdown)
            VALUES ('{self.config.symbol}', {balance}, {equity}, {drawdown})
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
        except Exception as e:
            logger.error(f"save_equity_snapshot error: {e}")

    def get_equity_curve(self, limit: int = 200) -> list:
        if not self.conn:
            return []
        sql = f"""
            SELECT balance, equity, drawdown, recorded_at
            FROM {self.prefix}equity
            WHERE symbol='{self.config.symbol}'
            ORDER BY recorded_at DESC LIMIT {limit}
        """
        try:
            with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            logger.error(f"get_equity_curve error: {e}")
            return []

    def get_open_positions(self) -> list:
        """Load open paper positions so they survive Vercel restarts."""
        if not self.conn:
            return []
        sql = f"""
            SELECT ticket, direction, lot_size, entry_price, stop_loss, take_profit,
                   open_time, session
            FROM {self.prefix}trades
            WHERE symbol='{self.config.symbol}' AND status='open' AND mode='paper'
            ORDER BY open_time ASC
        """
        try:
            with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql)
                rows = cur.fetchall()
            result = []
            for r in rows:
                result.append({
                    "ticket": r["ticket"],
                    "symbol": self.config.symbol,
                    "type": r["direction"],
                    "volume": r["lot_size"],
                    "price_open": r["entry_price"],
                    "sl": r["stop_loss"],
                    "tp": r["take_profit"],
                    "profit": 0.0,
                    "unrealized_profit": 0.0,
                    "time": r["open_time"].replace(tzinfo=None) if r["open_time"] else None,
                    "session": r["session"],
                })
            return result
        except Exception as e:
            logger.error(f"get_open_positions error: {e}")
            return []

    def update_sl(self, ticket: int, new_sl: float, symbol: str):
        if not self.conn:
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {self.prefix}trades SET stop_loss=%s WHERE ticket=%s AND symbol=%s",
                    (new_sl, ticket, symbol)
                )
        except Exception as e:
            logger.error(f"update_sl error: {e}")

    def get_paper_balance(self, symbol: str) -> Optional[float]:
        if not self.conn:
            return None
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    f"SELECT balance FROM {self.prefix}paper_balance WHERE symbol=%s",
                    (symbol,)
                )
                row = cur.fetchone()
                return float(row[0]) if row else None
        except Exception:
            return None

    def update_paper_balance(self, symbol: str, balance: float):
        if not self.conn:
            return
        sql = f"""
            INSERT INTO {self.prefix}paper_balance (symbol, balance)
            VALUES (%(symbol)s, %(balance)s)
            ON CONFLICT (symbol) DO UPDATE SET balance=%(balance)s, updated_at=NOW()
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, {"symbol": symbol, "balance": balance})
        except Exception as e:
            logger.error(f"update_paper_balance error: {e}")

    def save_log(self, message: str):
        if not self.conn:
            return
        try:
            with self.conn.cursor() as cur:
                cur.execute(
                    f"INSERT INTO {self.prefix}bot_logs (symbol, message) VALUES (%s, %s)",
                    (self.config.symbol, message),
                )
                # Keep only the last 500 rows per symbol to avoid unbounded growth
                cur.execute(
                    f"""
                    DELETE FROM {self.prefix}bot_logs
                    WHERE symbol=%s AND id NOT IN (
                        SELECT id FROM {self.prefix}bot_logs
                        WHERE symbol=%s ORDER BY logged_at DESC LIMIT 500
                    )
                    """,
                    (self.config.symbol, self.config.symbol),
                )
        except Exception as e:
            logger.error(f"save_log error: {e}")

    def get_logs(self, limit: int = 100) -> list:
        if not self.conn:
            return []
        sql = f"""
            SELECT message FROM {self.prefix}bot_logs
            WHERE symbol='{self.config.symbol}'
            ORDER BY logged_at DESC LIMIT {limit}
        """
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
            return [r[0] for r in reversed(rows)]
        except Exception as e:
            logger.error(f"get_logs error: {e}")
            return []

    def disconnect(self):
        if self.conn:
            self.conn.close()
            self.conn = None
