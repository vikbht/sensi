"""Options Sensi — options opportunity scanner. Launch with ./run.sh"""
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, market_clock, scanner

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sensi")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

scheduler = BackgroundScheduler()
_scan_lock = threading.Lock()
_last_scan: dict = {"at": None, "results": None}


def _run_scan(force: bool = False):
    """Scheduled scans respect market hours; manual scans (force=True) always run."""
    if not force and config.load()["market_hours_only"] and not market_clock.is_open():
        log.info("market closed; skipping scheduled scan")
        return
    if not _scan_lock.acquire(blocking=False):
        log.warning("scan already in progress; skipping this tick")
        return
    try:
        results = scanner.scan_watchlist()
        _last_scan["at"] = datetime.now(timezone.utc).isoformat()
        _last_scan["results"] = results
    finally:
        _scan_lock.release()


def _scan_single(symbol: str):
    """Scan one symbol immediately (any hour) — used when it's added to the
    watchlist so its row gets price/metrics without waiting for a sweep."""
    with _scan_lock:
        try:
            scanner.scan_symbol(symbol, config.load())
        except Exception:
            log.exception("single-symbol scan failed for %s", symbol)


def _schedule_job(minutes: int):
    scheduler.add_job(_run_scan, "interval", minutes=minutes,
                      id="watchlist_scan", replace_existing=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = config.load()
    _schedule_job(cfg["scan_interval_minutes"])
    scheduler.add_job(scanner.generate_daily_wrap, "cron",
                      day_of_week="mon-fri", hour=16, minute=15,
                      timezone=market_clock.ET, id="daily_wrap",
                      replace_existing=True)
    scheduler.start()
    threading.Thread(target=_run_scan, daemon=True).start()  # scan on boot
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Options Sensi", lifespan=lifespan)


class SymbolBody(BaseModel):
    symbol: str


@app.get("/api/watchlist")
def get_watchlist():
    return db.list_watchlist()


@app.post("/api/watchlist")
def add_to_watchlist(body: SymbolBody):
    sym = body.symbol.strip().upper()
    if not sym or len(sym) > 10:
        raise HTTPException(400, "invalid symbol")
    db.add_symbol(sym)
    threading.Thread(target=_scan_single, args=(sym,), daemon=True).start()
    return db.list_watchlist()


@app.delete("/api/watchlist/{symbol}")
def remove_from_watchlist(symbol: str):
    db.remove_symbol(symbol)
    return db.list_watchlist()


@app.get("/api/metrics")
def get_metrics():
    return db.latest_metrics()


@app.get("/api/signals")
def get_signals(limit: int = 100, symbol: str | None = None):
    return db.recent_signals(limit=min(limit, 500), symbol=symbol)


@app.get("/api/snapshots/{symbol}")
def get_snapshots(symbol: str, limit: int = 200):
    return db.snapshot_history(symbol.upper(), limit=min(limit, 1000))


@app.get("/api/config")
def get_config():
    return config.load()


@app.put("/api/config")
def put_config(cfg: dict):
    merged = config.save(cfg)
    _schedule_job(merged["scan_interval_minutes"])
    return merged


@app.post("/api/scan")
def trigger_scan():
    threading.Thread(target=_run_scan, kwargs={"force": True}, daemon=True).start()
    return {"started": True}


@app.post("/api/wrap")
def trigger_wrap():
    threading.Thread(target=scanner.generate_daily_wrap,
                     kwargs={"force": True}, daemon=True).start()
    return {"started": True}


@app.get("/api/status")
def status():
    job = scheduler.get_job("watchlist_scan")
    return {
        "last_scan_at": _last_scan["at"],
        "last_scan_results": _last_scan["results"],
        "next_scan_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "scanning": _scan_lock.locked(),
        "market_open": market_clock.is_open(),
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
