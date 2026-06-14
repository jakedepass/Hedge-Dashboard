"""
db.py — SQLite persistence layer for paper trades.
Replaces paper_trades.jsonl across paper_trader.py, pnl_tracker.py,
position_closer.py, and server.py.

DB location: $DB_PATH env var, or ./paper_trades.db by default.
On Railway, set DB_PATH=/data/paper_trades.db (mounted volume).
"""

import json
import os
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.db"))

_CREATE = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    action          TEXT NOT NULL,
    dry_run         INTEGER DEFAULT 0,
    ticker          TEXT,
    side            TEXT,
    direction       TEXT,
    qty             INTEGER,
    entry_price     REAL,
    close_price     REAL,
    position_usd    REAL,
    realized_pnl    REAL,
    event_title     TEXT,
    pm_url          TEXT,
    pm_prob         REAL,
    pm_volume       REAL,
    category        TEXT,
    edge_estimate   REAL,
    final_score     REAL,
    pattern_score   REAL,
    verdict         TEXT,
    alpaca_order_id TEXT,
    alpaca_status   TEXT,
    source          TEXT DEFAULT 'script',
    open_timestamp  TEXT,
    closes_order_id TEXT,
    pm_status       TEXT,
    pm_close_date   TEXT
)
"""

_COLS = [
    "timestamp", "action", "dry_run", "ticker", "side", "direction",
    "qty", "entry_price", "close_price", "position_usd", "realized_pnl",
    "event_title", "pm_url", "pm_prob", "pm_volume", "category",
    "edge_estimate", "final_score", "pattern_score", "verdict",
    "alpaca_order_id", "alpaca_status", "source", "open_timestamp",
    "closes_order_id", "pm_status", "pm_close_date",
]


@contextmanager
def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with _conn() as c:
        c.execute(_CREATE)


def log_trade(record: dict):
    """Insert one trade or close record."""
    init_db()
    vals = [record.get(col) for col in _COLS]
    dry_idx = _COLS.index("dry_run")
    vals[dry_idx] = 1 if vals[dry_idx] else 0
    placeholders = ",".join("?" * len(_COLS))
    with _conn() as c:
        c.execute(
            f"INSERT INTO trades ({','.join(_COLS)}) VALUES ({placeholders})",
            vals,
        )


def get_trades(include_dry_runs: bool = False, action: str = None) -> list[dict]:
    """Return records ordered by insertion, optionally filtered."""
    init_db()
    q, params = "SELECT * FROM trades WHERE 1=1", []
    if not include_dry_runs:
        q += " AND dry_run = 0"
    if action:
        q += " AND action = ?"
        params.append(action)
    q += " ORDER BY id ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, params).fetchall()]


def get_open_trades(include_dry_runs: bool = False) -> list[dict]:
    """Return trades that haven't been closed yet."""
    init_db()
    dry = "" if include_dry_runs else "AND dry_run = 0"
    q = f"""
        SELECT * FROM trades
        WHERE action = 'trade' {dry}
        AND (alpaca_order_id IS NULL OR alpaca_order_id NOT IN (
            SELECT closes_order_id FROM trades
            WHERE action = 'close' AND closes_order_id IS NOT NULL
        ))
        ORDER BY id ASC
    """
    with _conn() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


def migrate_from_jsonl(jsonl_path: str = None) -> int:
    """One-time import of an existing paper_trades.jsonl into SQLite."""
    if jsonl_path is None:
        jsonl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.jsonl")
    if not os.path.exists(jsonl_path):
        return 0
    init_db()
    count = 0
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                log_trade(json.loads(line))
                count += 1
            except Exception:
                pass
    return count


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        path = sys.argv[2] if len(sys.argv) > 2 else None
        n = migrate_from_jsonl(path)
        print(f"Migrated {n} records into {DB_PATH}")
    else:
        init_db()
        rows = get_trades(include_dry_runs=True)
        print(f"{DB_PATH}: {len(rows)} records")
