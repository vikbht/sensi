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

CREATE TABLE IF NOT EXISTS scan_lease (
    id INTEGER PRIMARY KEY CHECK (id = 1),   -- single row
    instance_id TEXT NOT NULL,
    heartbeat TEXT NOT NULL DEFAULT (datetime('now'))
);
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
        _migrate(conn)
        _local.conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created before a column existed."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    if "next_earnings" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN next_earnings TEXT")
    if "prev_close" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN prev_close REAL")
    if "short_pct_float" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN short_pct_float REAL")
        conn.execute("ALTER TABLE snapshots ADD COLUMN days_to_cover REAL")
    if "atm_dte" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN atm_dte INTEGER")
    conn.commit()


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
               put_volume, pc_ratio, net_gex, peak_gamma_strike, skew, next_earnings,
               prev_close, short_pct_float, days_to_cover, atm_dte)
           VALUES (:symbol, :spot, :atm_iv, :hv20, :hv10, :call_volume,
               :put_volume, :pc_ratio, :net_gex, :peak_gamma_strike, :skew,
               :next_earnings, :prev_close, :short_pct_float, :days_to_cover,
               :atm_dte)""",
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
                  s.peak_gamma_strike, s.skew, s.scanned_at, s.next_earnings,
                  s.prev_close, s.short_pct_float, s.days_to_cover, s.atm_dte,
                  (SELECT COUNT(*) FROM signals sig WHERE sig.symbol = w.symbol
                     AND sig.created_at >= datetime('now', '-1 day')) AS signals_24h,
                  (SELECT COUNT(*) FROM signals sig WHERE sig.symbol = w.symbol
                     AND sig.kind = 'confluence'
                     AND sig.created_at >= datetime('now', '-1 day')) AS confluence_24h
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


def purge_orphan_snapshots() -> int:
    """Drop snapshots for symbols no longer on the watchlist — age-based
    retention alone leaves removed tickers lingering up to 30 days."""
    conn = get_conn()
    n = conn.execute(
        "DELETE FROM snapshots WHERE symbol NOT IN (SELECT symbol FROM watchlist)"
    ).rowcount
    conn.commit()
    return n


# --- scan lease: exactly one process should run the scheduled sweep ---

def claim_lease(instance_id: str) -> None:
    """Unconditionally take ownership — newest startup / manual scan wins."""
    conn = get_conn()
    conn.execute(
        """INSERT INTO scan_lease(id, instance_id, heartbeat)
           VALUES (1, ?, datetime('now'))
           ON CONFLICT(id) DO UPDATE SET
               instance_id=excluded.instance_id, heartbeat=excluded.heartbeat""",
        (instance_id,))
    conn.commit()


def try_acquire_lease(instance_id: str, stale_seconds: int) -> tuple[bool, str]:
    """Acquire/refresh the scan lease. Returns (is_owner, current_owner_id).

    We own scanning if the lease is empty, already ours, or the current
    owner's heartbeat has gone stale (its process died). Otherwise another
    live instance owns it and we stay passive. BEGIN IMMEDIATE serializes
    the read-modify-write across processes sharing the DB file.
    """
    conn = get_conn()
    conn.commit()  # ensure no implicit transaction is open
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """SELECT instance_id,
                      (julianday('now') - julianday(heartbeat)) * 86400 AS age
               FROM scan_lease WHERE id = 1""").fetchone()
        if row is None or row["instance_id"] == instance_id or row["age"] >= stale_seconds:
            conn.execute(
                """INSERT INTO scan_lease(id, instance_id, heartbeat)
                   VALUES (1, ?, datetime('now'))
                   ON CONFLICT(id) DO UPDATE SET
                       instance_id=excluded.instance_id, heartbeat=excluded.heartbeat""",
                (instance_id,))
            conn.commit()
            return True, instance_id
        owner = row["instance_id"]
        conn.commit()
        return False, owner
    except Exception:
        conn.rollback()
        raise


def lease_info() -> dict | None:
    row = get_conn().execute(
        """SELECT instance_id,
                  (julianday('now') - julianday(heartbeat)) * 86400 AS age_seconds
           FROM scan_lease WHERE id = 1""").fetchone()
    return dict(row) if row else None


def last_snapshot_before(symbol: str, before_utc: str) -> dict | None:
    row = get_conn().execute(
        """SELECT * FROM snapshots WHERE symbol = ? AND scanned_at < ?
           ORDER BY scanned_at DESC, id DESC LIMIT 1""",
        (symbol, before_utc),
    ).fetchone()
    return dict(row) if row else None


def signals_since(symbol: str, since_utc: str) -> list[dict]:
    rows = get_conn().execute(
        "SELECT * FROM signals WHERE symbol = ? AND created_at >= ? ORDER BY id",
        (symbol, since_utc),
    ).fetchall()
    return [dict(r) for r in rows]


def distinct_signal_kinds_since(symbol: str, minutes: int) -> list[str]:
    """Distinct non-confluence signal kinds for a symbol in the window."""
    rows = get_conn().execute(
        """SELECT DISTINCT kind FROM signals WHERE symbol = ?
           AND kind != 'confluence' AND created_at >= datetime('now', ?)
           ORDER BY kind""",
        (symbol, f"-{int(minutes)} minutes"),
    ).fetchall()
    return [r["kind"] for r in rows]


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
