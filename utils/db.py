"""
SQLite database initialization and migration.
"""
import sqlite3
import logging

from config import DB_FILE

log = logging.getLogger(__name__)


def init_db() -> sqlite3.Connection:
    """Create tables, run migrations, set WAL mode. Returns a connection."""
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            market_name TEXT    NOT NULL,
            token_id    TEXT,
            direction   TEXT    NOT NULL,
            price       REAL    NOT NULL,
            amount      REAL    NOT NULL,
            fee         REAL    NOT NULL,
            order_type  TEXT    NOT NULL,
            tags        TEXT    NOT NULL,
            notes       TEXT,
            resolved    INTEGER DEFAULT 0,
            outcome     TEXT,
            pnl         REAL,
            close_ts    TEXT,
            end_date    TEXT,
            liquidity   REAL,
            volume_24h  REAL,
            market_slug TEXT,
            market_tags TEXT,
            whale_size  REAL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            markets_checked INTEGER,
            signals_found   INTEGER,
            trades_placed   INTEGER
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            strategy    TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            market_name TEXT    NOT NULL,
            direction   TEXT    NOT NULL,
            price       REAL    NOT NULL,
            amount      REAL    NOT NULL,
            fee         REAL,
            confidence  REAL,
            notes       TEXT,
            resolved    INTEGER DEFAULT 0,
            outcome     TEXT,
            pnl         REAL,
            close_ts    TEXT,
            end_date    TEXT,
            liquidity   REAL,
            volume_24h  REAL,
            market_slug TEXT,
            market_tags TEXT
        )
    """)
    # Migrate existing shadow_trades table if columns are missing
    for col, definition in [
        ("confidence",  "REAL"),
        ("notes",       "TEXT"),
        ("fee",         "REAL"),
        ("end_date",    "TEXT"),
        ("liquidity",   "REAL"),
        ("volume_24h",  "REAL"),
        ("market_slug", "TEXT"),
        ("market_tags", "TEXT"),
    ]:
        try:
            con.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    # Migrate trades table
    for col, definition in [
        ("mode",        "TEXT DEFAULT 'paper'"),
        ("order_id",    "TEXT"),
        ("end_date",    "TEXT"),
        ("liquidity",   "REAL"),
        ("volume_24h",  "REAL"),
        ("market_slug", "TEXT"),
        ("market_tags", "TEXT"),
        ("whale_size",  "REAL"),
        ("confidence",  "REAL"),
    ]:
        try:
            con.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    # Indexes for common queries
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_shadow_resolved ON shadow_trades(resolved)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_shadow_market_id ON shadow_trades(market_id)")
    con.execute("PRAGMA journal_mode=WAL")
    con.commit()
    return con
