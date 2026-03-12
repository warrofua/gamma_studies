import sqlite3
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import List, Tuple

DB_PATH = os.environ.get("AUTOGEX_DB_PATH", str(Path.home() / "autogex_state.db"))


def init_db() -> None:
    """Create all four tables if they don't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                trade_id              TEXT PRIMARY KEY,
                date                  TEXT,
                direction             TEXT,
                symbol                TEXT,
                strike                REAL,
                expiration            TEXT,
                entry_time            TEXT,
                entry_price           REAL,
                entry_qty             INTEGER,
                exit_time             TEXT,
                exit_price            REAL,
                exit_qty              INTEGER,
                exit_reason           TEXT,
                realized_pnl          REAL,
                conviction            INTEGER,
                signals_json          TEXT,
                gex_snapshot_json     TEXT,
                gex_snapshot_exit_json TEXT,
                regime_at_entry       TEXT,
                regime_at_exit        TEXT,
                tranche               TEXT,
                hold_seconds          INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                trade_id         TEXT PRIMARY KEY,
                symbol           TEXT,
                direction        TEXT,
                strike           REAL,
                entry_price      REAL,
                entry_time       TEXT,
                total_qty        INTEGER,
                remaining_qty    INTEGER,
                tranche_a_closed INTEGER,
                high_water_mark  REAL,
                current_stop     REAL,
                conviction       INTEGER,
                signals_json     TEXT,
                gex_entry_json   TEXT,
                current_price    REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS engine_state (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                date          TEXT PRIMARY KEY,
                total_pnl     REAL,
                trade_count   INTEGER,
                win_count     INTEGER,
                loss_count    INTEGER,
                avg_winner    REAL,
                avg_loser     REAL,
                max_drawdown  REAL,
                largest_win   REAL,
                largest_loss  REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gex_snapshots (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                fetched_at TEXT NOT NULL,
                data_json  TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gex_snapshots_fetched_at ON gex_snapshots(fetched_at)"
        )
        # Migration: add current_price column to existing DBs
        try:
            conn.execute("ALTER TABLE positions ADD COLUMN current_price REAL")
        except Exception:
            pass  # column already exists

        # Migration: add capital risk and tranche linkage columns to trades
        _ALLOWED_MIGRATION_COLS = {"capital_risked", "initial_stop_price", "parent_trade_id"}
        _ALLOWED_MIGRATION_TYPES = {"REAL", "TEXT"}
        for _col, _coltype in [
            ("capital_risked", "REAL"),
            ("initial_stop_price", "REAL"),
            ("parent_trade_id", "TEXT"),
        ]:
            assert _col in _ALLOWED_MIGRATION_COLS, f"Unexpected migration column: {_col}"
            assert _coltype in _ALLOWED_MIGRATION_TYPES, f"Unexpected migration type: {_coltype}"
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {_col} {_coltype}")
            except Exception:
                pass  # column already exists

        # Cache table for EOD reports (also serves as idempotency lock)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS eod_reports (
                date          TEXT PRIMARY KEY,
                report_json   TEXT,
                generated_at  TEXT
            )
        """)

        # Index for date-range queries on trades (weekly/monthly P&L)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(date)"
        )
        conn.commit()


def _serialize_value(value):
    """Serialize value to JSON if it's a dict or list, otherwise return as-is."""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _deserialize_value(value):
    """Attempt to deserialize a JSON string, return original value if not JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def insert_trade(trade: dict) -> None:
    """Insert a row into trades. JSON-serialize any dict/list values."""
    with sqlite3.connect(DB_PATH) as conn:
        columns = list(trade.keys())
        placeholders = ",".join(["?" for _ in columns])
        values = [_serialize_value(trade[col]) for col in columns]

        query = f"INSERT INTO trades ({','.join(columns)}) VALUES ({placeholders})"
        conn.execute(query, values)
        conn.commit()


def update_trade_exit(
    trade_id: str,
    exit_time: str,
    exit_price: float,
    exit_qty: int,
    exit_reason: str,
    realized_pnl: float,
    regime_at_exit: str,
    gex_snapshot_exit: dict,
    hold_seconds: int,
) -> None:
    """UPDATE the trades row for trade_id with exit fields."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            UPDATE trades
            SET exit_time = ?, exit_price = ?, exit_qty = ?, exit_reason = ?,
                realized_pnl = ?, regime_at_exit = ?, gex_snapshot_exit_json = ?,
                hold_seconds = ?
            WHERE trade_id = ?
            """,
            (
                exit_time,
                exit_price,
                exit_qty,
                exit_reason,
                realized_pnl,
                regime_at_exit,
                _serialize_value(gex_snapshot_exit),
                hold_seconds,
                trade_id,
            ),
        )
        conn.commit()


def upsert_position(position: dict) -> None:
    """INSERT OR REPLACE into positions."""
    with sqlite3.connect(DB_PATH) as conn:
        columns = list(position.keys())
        placeholders = ",".join(["?" for _ in columns])
        values = [_serialize_value(position[col]) for col in columns]

        query = f"""
            INSERT OR REPLACE INTO positions ({','.join(columns)})
            VALUES ({placeholders})
        """
        conn.execute(query, values)
        conn.commit()


def delete_position(trade_id: str) -> None:
    """DELETE from positions where trade_id = ?."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM positions WHERE trade_id = ?", (trade_id,))
        conn.commit()


def get_open_positions() -> list[dict]:
    """SELECT all rows from positions, return as list of dicts."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT * FROM positions")
        col_names = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(col_names, row)) for row in rows]


def set_engine_state(key: str, value) -> None:
    """INSERT OR REPLACE into engine_state. JSON-serialize value if not a string."""
    with sqlite3.connect(DB_PATH) as conn:
        serialized_value = _serialize_value(value) if not isinstance(value, str) else value
        updated_at = datetime.utcnow().isoformat()
        conn.execute(
            """
            INSERT OR REPLACE INTO engine_state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, serialized_value, updated_at),
        )
        conn.commit()


def get_engine_state(key: str, default=None):
    """SELECT value from engine_state where key matches. Deserialize JSON if possible."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT value FROM engine_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        if row is None:
            return default
        return _deserialize_value(row[0])


def get_trades_for_date(date_str: str) -> list[dict]:
    """SELECT all rows from trades where date = ?, return as list of dicts."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT * FROM trades WHERE date = ?", (date_str,))
        col_names = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(col_names, row)) for row in rows]


def upsert_daily_summary(summary: dict) -> None:
    """INSERT OR REPLACE into daily_summary."""
    with sqlite3.connect(DB_PATH) as conn:
        columns = list(summary.keys())
        placeholders = ",".join(["?" for _ in columns])
        values = [summary[col] for col in columns]

        query = f"""
            INSERT OR REPLACE INTO daily_summary ({','.join(columns)})
            VALUES ({placeholders})
        """
        conn.execute(query, values)
        conn.commit()


def get_daily_summary(date_str: str) -> dict | None:
    """SELECT from daily_summary where date = ?. Return dict or None."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT * FROM daily_summary WHERE date = ?", (date_str,))
        col_names = [description[0] for description in cursor.description]
        row = cursor.fetchone()
        if row is None:
            return None
        return dict(zip(col_names, row))


def compute_daily_summary(date_str: str) -> dict:
    """Compute summary from trades table for the given date and upsert it."""
    with sqlite3.connect(DB_PATH) as conn:
        # Get all trades for the date
        cursor = conn.execute(
            "SELECT realized_pnl, exit_time FROM trades WHERE date = ?",
            (date_str,),
        )
        rows = cursor.fetchall()

        trades = [{"realized_pnl": row[0], "exit_time": row[1]} for row in rows]

        # Compute all closed trades (where exit_time IS NOT NULL)
        closed_trades = [t for t in trades if t["exit_time"] is not None]
        closed_pnls = [t["realized_pnl"] for t in closed_trades]

        # Compute summary metrics
        total_pnl = sum(closed_pnls) if closed_pnls else 0.0
        trade_count = len(trades)

        winners = [p for p in closed_pnls if p > 0]
        losers = [p for p in closed_pnls if p < 0]

        win_count = len(winners)
        loss_count = len(losers)
        avg_winner = sum(winners) / len(winners) if winners else 0.0
        avg_loser = sum(losers) / len(losers) if losers else 0.0

        max_drawdown = min(losers) if losers else 0.0
        largest_win = max(winners) if winners else 0.0
        largest_loss = min(losers) if losers else 0.0

        summary = {
            "date": date_str,
            "total_pnl": total_pnl,
            "trade_count": trade_count,
            "win_count": win_count,
            "loss_count": loss_count,
            "avg_winner": avg_winner,
            "avg_loser": avg_loser,
            "max_drawdown": max_drawdown,
            "largest_win": largest_win,
            "largest_loss": largest_loss,
        }

        # Upsert into database
        upsert_daily_summary(summary)
        return summary


# ---------------------------------------------------------------------------
# SPY GEX snapshot store (for backtesting)
# ---------------------------------------------------------------------------

def store_spy_snapshot(data_json: dict, fetched_at: datetime) -> None:
    """Persist a raw SPY option chain JSON snapshot for later backtesting."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO gex_snapshots (fetched_at, data_json) VALUES (?, ?)",
            (fetched_at.isoformat(), json.dumps(data_json)),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# EOD report store (idempotency lock + dashboard cache)
# ---------------------------------------------------------------------------

def eod_report_exists(date_str: str) -> bool:
    """Return True if an EOD report has already been generated for date_str."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT 1 FROM eod_reports WHERE date = ?", (date_str,)
        )
        return cursor.fetchone() is not None


def upsert_eod_report(date_str: str, report_json: str) -> None:
    """INSERT OR REPLACE the EOD report JSON for a given date."""
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO eod_reports (date, report_json, generated_at)
               VALUES (?, ?, ?)""",
            (date_str, report_json, now),
        )
        conn.commit()


def get_eod_report(date_str: str) -> dict | None:
    """Return the cached EOD report for date_str, or None if not found / malformed."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT report_json FROM eod_reports WHERE date = ?", (date_str,)
        )
        row = cursor.fetchone()
    if row is None:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def get_trade_count_for_date(date_str: str) -> int:
    """Count entry rows (tranche='full') for date_str. Avoids double-counting tranche A rows."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE date = ? AND tranche = 'full'",
            (date_str,),
        )
        return cursor.fetchone()[0] or 0


def get_weekly_pnl(date_str: str) -> float:
    """Sum realized_pnl for closed trades from Monday of date_str's week through date_str."""
    from datetime import timedelta
    d = date.fromisoformat(date_str)
    monday = d - timedelta(days=d.weekday())
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """SELECT COALESCE(SUM(realized_pnl), 0.0)
               FROM trades
               WHERE date >= ? AND date <= ? AND exit_time IS NOT NULL""",
            (monday.isoformat(), date_str),
        )
        return float(cursor.fetchone()[0] or 0.0)


def get_monthly_pnl(date_str: str) -> float:
    """Sum realized_pnl for closed trades from 1st of month through date_str."""
    d = date.fromisoformat(date_str)
    first_of_month = d.replace(day=1)
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """SELECT COALESCE(SUM(realized_pnl), 0.0)
               FROM trades
               WHERE date >= ? AND date <= ? AND exit_time IS NOT NULL""",
            (first_of_month.isoformat(), date_str),
        )
        return float(cursor.fetchone()[0] or 0.0)


# ---------------------------------------------------------------------------
# SPY GEX snapshot store (for backtesting)
# ---------------------------------------------------------------------------

def load_spy_snapshots(start: date, end: date) -> List[Tuple[int, dict, datetime]]:
    """Return SPY snapshots with fetched_at between start and end (inclusive), oldest first."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            """
            SELECT id, data_json, fetched_at FROM gex_snapshots
            WHERE fetched_at >= ? AND fetched_at < date(?, '+1 day')
            ORDER BY fetched_at ASC
            """,
            (start.isoformat(), end.isoformat()),
        )
        rows = cursor.fetchall()

    result = []
    for row_id, data_str, fa_str in rows:
        try:
            data = json.loads(data_str)
            fa = datetime.fromisoformat(fa_str)
            result.append((row_id, data, fa))
        except Exception:
            pass
    return result
