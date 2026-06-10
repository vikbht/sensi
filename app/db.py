"""SQLite persistence: watchlist, per-scan snapshots, and emitted signals."""
import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "sensi.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
    symbol TEXT PRIMARY KEY,
    added_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    scanned_at TEXT DEFAULT (datetime('now')),
    spot REAL,
    atm_iv REAL,
    hv20 REAL,
    hv10 REAL,
    call_volume INTEGER,
    put_volume INTEGER,
    pc_ratio REAL,
    net_gex REAL,
    peak_gamma_strike REAL,
    skew REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol_time ON snapshots(symbol, scanned_at DESC);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,        -- info | warning | critical
    message TEXT NOT NULL,
    value REAL,
    details TEXT                    -- JSON blob with supporting data
);
CREATE INDEX IF NOT EXISTS idx_signals_time ON signals(created_at DESC);
"""


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # Scanner thread and API threads hit the DB concurrently
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(SCHEMA)
        _local.conn = conn
    return conn


# --- watchlist ---

def list_watchlist() -> list[str]:
    rows = get_conn().execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
    return [r["symbol"] for r in rows]


def add_symbol(symbol: str) -> None:
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO watchlist(symbol) VALUES (?)", (symbol.upper(),))
    conn.commit()


def remove_symbol(symbol: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol.upper(),))
    conn.commit()


# --- snapshots ---

def insert_snapshot(s: dict) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO snapshots(symbol, spot, atm_iv, hv20, hv10, call_volume,
               put_volume, pc_ratio, net_gex, peak_gamma_strike, skew)
           VALUES (:symbol, :spot, :atm_iv, :hv20, :hv10, :call_volume,
               :put_volume, :pc_ratio, :net_gex, :peak_gamma_strike, :skew)""",
        s,
    )
    conn.commit()


def recent_snapshots(symbol: str, limit: int, since_utc: str | None = None) -> list[dict]:
    """Latest snapshots, newest first. `since_utc` scopes to the current
    session so baselines don't mix in yesterday's (or the weekend's) data."""
    q = "SELECT * FROM snapshots WHERE symbol = ?"
    args: list = [symbol]
    if since_utc:
        q += " AND scanned_at >= ?"
        args.append(since_utc)
    q += " ORDER BY scanned_at DESC, id DESC LIMIT ?"
    rows = get_conn().execute(q, (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def latest_metrics() -> list[dict]:
    """One row per watchlist symbol: its newest snapshot (NULL columns if never
    scanned) plus a 24h signal count. Single query so the UI scales with the list."""
    rows = get_conn().execute(
        """SELECT w.symbol AS symbol, s.spot, s.atm_iv, s.hv20, s.hv10,
                  s.call_volume, s.put_volume, s.pc_ratio, s.net_gex,
                  s.peak_gamma_strike, s.skew, s.scanned_at,
                  (SELECT COUNT(*) FROM signals sig WHERE sig.symbol = w.symbol
                     AND sig.created_at >= datetime('now', '-1 day')) AS signals_24h
           FROM watchlist w
           LEFT JOIN snapshots s ON s.id = (
               SELECT id FROM snapshots WHERE symbol = w.symbol
               ORDER BY scanned_at DESC, id DESC LIMIT 1)
           ORDER BY w.symbol"""
    ).fetchall()
    return [dict(r) for r in rows]


def snapshot_history(symbol: str, limit: int = 200) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM snapshots WHERE symbol = ? ORDER BY scanned_at ASC, id ASC LIMIT ?",
        (symbol, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# --- signals ---

def insert_signal(symbol: str, kind: str, severity: str, message: str,
                  value: float | None = None, details: str | None = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO signals(symbol, kind, severity, message, value, details) VALUES (?,?,?,?,?,?)",
        (symbol, kind, severity, message, value, details),
    )
    conn.commit()


def purge_old(snapshot_days: int, signal_days: int) -> tuple[int, int]:
    """Delete rows older than the retention windows. Returns rows removed."""
    conn = get_conn()
    snaps = conn.execute(
        "DELETE FROM snapshots WHERE scanned_at < datetime('now', ?)",
        (f"-{int(snapshot_days)} days",)).rowcount
    sigs = conn.execute(
        "DELETE FROM signals WHERE created_at < datetime('now', ?)",
        (f"-{int(signal_days)} days",)).rowcount
    conn.commit()
    return snaps, sigs


def signal_fired_recently(symbol: str, kind: str, cooldown_minutes: int) -> bool:
    row = get_conn().execute(
        """SELECT 1 FROM signals WHERE symbol = ? AND kind = ?
           AND created_at >= datetime('now', ?) LIMIT 1""",
        (symbol, kind, f"-{int(cooldown_minutes)} minutes"),
    ).fetchone()
    return row is not None


def recent_signals(limit: int = 100, symbol: str | None = None) -> list[dict]:
    q = "SELECT * FROM signals"
    args: tuple = ()
    if symbol:
        q += " WHERE symbol = ?"
        args = (symbol.upper(),)
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    rows = get_conn().execute(q, args + (limit,)).fetchall()
    return [dict(r) for r in rows]
