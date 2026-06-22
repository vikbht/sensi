"""Options Sensi — options opportunity scanner. Launch with ./run.sh"""
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, market_clock, scanner
from .analytics import iv_rank
from .analytics import outcomes as outcomes_mod

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sensi")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

scheduler = BackgroundScheduler()
_scan_lock = threading.Lock()
_last_scan: dict = {"at": None, "results": None, "duration": None}

# Identifies this process in the cross-process scan lease (issue #19): if a
# stale instance is still running against the same DB, only one of them sweeps.
INSTANCE_ID = uuid.uuid4().hex[:8]


def _lease_stale_seconds() -> int:
    """Treat the lease owner as dead after a few missed scan intervals."""
    return max(config.load()["scan_interval_minutes"] * 3, 15) * 60


def _run_scan(force: bool = False):
    """Scheduled scans respect market hours and the scan lease; manual scans
    (force=True) always run and take ownership of the lease for this instance."""
    if not force and config.load()["market_hours_only"] and not market_clock.is_open():
        log.info("market closed; skipping scheduled scan")
        return
    if force:
        db.claim_lease(INSTANCE_ID)
    else:
        is_owner, owner = db.try_acquire_lease(INSTANCE_ID, _lease_stale_seconds())
        if not is_owner:
            log.warning("another instance (%s) owns scanning; staying passive", owner)
            return
    if not _scan_lock.acquire(blocking=False):
        log.warning("scan already in progress; skipping this tick")
        return
    try:
        started = time.monotonic()
        results = scanner.scan_watchlist()
        _last_scan["at"] = datetime.now(timezone.utc).isoformat()
        _last_scan["results"] = results
        _last_scan["duration"] = round(time.monotonic() - started, 1)
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
    db.claim_lease(INSTANCE_ID)  # newest start wins; older instances go passive
    log.info("instance %s started; claimed scan lease", INSTANCE_ID)
    _schedule_job(cfg["scan_interval_minutes"])
    scheduler.add_job(scanner.generate_daily_wrap, "cron",
                      day_of_week="mon-fri", hour=16, minute=15,
                      timezone=market_clock.ET, id="daily_wrap",
                      replace_existing=True)
    scheduler.add_job(scanner.compute_outcomes, "cron",
                      day_of_week="mon-fri", hour=16, minute=30,
                      timezone=market_clock.ET, id="compute_outcomes",
                      replace_existing=True)
    scheduler.start()
    threading.Thread(target=_run_scan, daemon=True).start()  # scan on boot
    threading.Thread(target=scanner.compute_outcomes, daemon=True).start()  # seed/mature
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Options Sensi", lifespan=lifespan)


@app.middleware("http")
async def no_cache(request, call_next):
    """Force the browser to revalidate assets each load — otherwise a cached
    app.js/style.css silently outlives a frontend change (issue #25).
    StaticFiles still sends ETag/Last-Modified, so this is a cheap 304."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


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
    cfg = config.load()
    rows = db.latest_metrics()
    for r in rows:
        series = iv_rank.clean_series(db.daily_iv_series(r["symbol"], cfg["iv_rank_window_days"]))
        vals = [v for _, v in series]
        rk = iv_rank.rank_and_pctile(vals) if len(vals) >= cfg["iv_rank_min_sessions"] else None
        r["iv_rank"] = rk["rank"] if rk else None
        r["iv_pctile"] = rk["pctile"] if rk else None
        r["iv_sessions"] = len(vals)
    return rows


@app.get("/api/iv_history/{symbol}")
def iv_history(symbol: str):
    cfg = config.load()
    series = iv_rank.clean_series(db.daily_iv_series(symbol.upper(), cfg["iv_rank_window_days"]))
    vals = [v for _, v in series]
    rk = iv_rank.rank_and_pctile(vals) if len(vals) >= cfg["iv_rank_min_sessions"] else None
    return {
        "points": [{"date": d, "iv": v} for d, v in series],
        "min": min(vals) if vals else None, "max": max(vals) if vals else None,
        "rank": rk["rank"] if rk else None, "pctile": rk["pctile"] if rk else None,
        "sessions": len(vals),
    }


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


@app.get("/api/outcomes")
def get_outcomes():
    cfg = config.load()
    return outcomes_mod.aggregate(db.all_outcomes(), db.get_baselines(),
                                  cfg["outcome_min_samples"])


@app.post("/api/outcomes/compute")
def trigger_outcomes():
    threading.Thread(target=scanner.compute_outcomes, daemon=True).start()
    return {"started": True}


@app.get("/api/status")
def status():
    job = scheduler.get_job("watchlist_scan")
    lease = db.lease_info()
    owner = lease["instance_id"] if lease else None
    return {
        "last_scan_at": _last_scan["at"],
        "last_scan_results": _last_scan["results"],
        "last_scan_duration": _last_scan.get("duration"),
        "next_scan_at": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "scanning": _scan_lock.locked(),
        "market_open": market_clock.is_open(),
        "instance_id": INSTANCE_ID,
        "scan_owner": owner,
        "is_scan_owner": owner == INSTANCE_ID,
    }


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
