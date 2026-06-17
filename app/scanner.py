"""Scan orchestration: fetch data, compute metrics, persist snapshot, run detectors."""
import json
import logging
from datetime import date

from . import config, db, market_clock
from .analytics import signals as detectors
from .analytics.black_scholes import gamma as bs_gamma
from .analytics.historical_vol import close_to_close_hv
from .providers.yfinance_provider import YFinanceProvider

log = logging.getLogger("sensi.scanner")

provider = YFinanceProvider()

CONTRACT_MULTIPLIER = 100

# Earnings dates and short interest barely move — one lookup per symbol per day
_earnings_cache: dict[str, tuple[date, date | None]] = {}
_short_cache: dict[str, tuple[date, dict | None]] = {}

# Setup-style signals persist for days, so they get longer cooldowns
KIND_COOLDOWN_KEYS = {
    "squeeze_setup": "squeeze_cooldown_minutes",
    "vol_compression": "vol_compression_cooldown_minutes",
}


def _next_earnings(symbol: str) -> date | None:
    today = date.today()
    hit = _earnings_cache.get(symbol)
    if hit and hit[0] == today:
        return hit[1]
    earnings = provider.get_next_earnings(symbol)
    _earnings_cache[symbol] = (today, earnings)
    return earnings


def _short_interest(symbol: str) -> dict | None:
    today = date.today()
    hit = _short_cache.get(symbol)
    if hit and hit[0] == today:
        return hit[1]
    si = provider.get_short_interest(symbol)
    _short_cache[symbol] = (today, si)
    return si


def _prev_close(closes) -> float | None:
    """Previous session's close: skip the trailing bar when it's today's
    (still-forming or just-completed) session, so day change = spot vs
    yesterday's close."""
    if closes is None or len(closes) == 0:
        return None
    last_bar_date = closes.index[-1].date()
    if last_bar_date >= market_clock.now_et().date() and len(closes) > 1:
        return float(closes.iloc[-2])
    return float(closes.iloc[-1])


def _catalyst_note(days_to_earnings: int | None, kind: str, cfg: dict) -> str:
    """Suffix that puts a signal in calendar context.

    Unknown earnings dates (None) get no tag — absence of data is not
    absence of a catalyst.
    """
    d = days_to_earnings
    if d is None:
        return ""
    if 0 <= d <= cfg["earnings_window_days"]:
        return f" · earnings in {d}d — event premium likely"
    if kind in ("iv_premium", "iv_spike") and d > cfg["no_catalyst_window_days"]:
        return (f" · no earnings inside {cfg['no_catalyst_window_days']}d "
                f"(next in {d}d) — vol bid without an obvious catalyst")
    return ""


def _atm_iv(contracts: list[dict], spot: float) -> float | None:
    """Average IV of the call+put closest to spot in the nearest picked expiry."""
    nearest_exp = min((c["expiry"] for c in contracts), default=None)
    if nearest_exp is None:
        return None
    ivs = []
    for opt_type in ("call", "put"):
        candidates = [c for c in contracts
                      if c["expiry"] == nearest_exp and c["type"] == opt_type and c["iv"]]
        if candidates:
            atm = min(candidates, key=lambda c: abs(c["strike"] - spot))
            ivs.append(atm["iv"])
    return sum(ivs) / len(ivs) if ivs else None


def _nearest_dte(contracts: list[dict]) -> int | None:
    """DTE of the nearest picked expiry — the horizon ATM IV and skew measure."""
    dtes = [c["dte"] for c in contracts if c.get("dte") is not None]
    return min(dtes) if dtes else None


def _skew(contracts: list[dict], spot: float) -> float | None:
    """OTM put IV minus OTM call IV (~5% out), nearest picked expiry.

    Positive and rising = downside protection getting bid.
    """
    nearest_exp = min((c["expiry"] for c in contracts), default=None)
    if nearest_exp is None:
        return None
    puts = [c for c in contracts
            if c["expiry"] == nearest_exp and c["type"] == "put" and c["iv"]]
    calls = [c for c in contracts
             if c["expiry"] == nearest_exp and c["type"] == "call" and c["iv"]]
    if not puts or not calls:
        return None

    def pick(cands: list[dict], target: float) -> dict:
        # Prefer strikes with a live two-sided quote — a stale single strike
        # is what makes raw skew whipsaw — but fall back to nearest-by-strike
        # when nothing is live (after hours, illiquid names)
        live = [c for c in cands if c.get("has_quote", True)]
        pool = live or cands
        return min(pool, key=lambda c: abs(c["strike"] - target))

    otm_put = pick(puts, spot * 0.95)
    otm_call = pick(calls, spot * 1.05)
    return otm_put["iv"] - otm_call["iv"]


def _gamma_profile(contracts: list[dict], spot: float
                   ) -> tuple[float, float | None, list[dict]]:
    """Naive dealer GEX: assume dealers are long calls / short puts.

    Returns (net gamma exposure in $ per 1% move, strike with peak |gamma·OI|,
    top contracts on the net's side — the strikes driving the current regime).
    """
    net = 0.0
    by_strike: dict[float, float] = {}
    rows: list[dict] = []
    for c in contracts:
        if not c["iv"] or not c["open_interest"]:
            continue
        t_years = max(c["dte"], 1) / 365.0
        g = bs_gamma(spot, c["strike"], t_years, c["iv"])
        notional = g * c["open_interest"] * CONTRACT_MULTIPLIER * spot * spot * 0.01
        signed = notional if c["type"] == "call" else -notional
        net += signed
        by_strike[c["strike"]] = by_strike.get(c["strike"], 0.0) + abs(notional)
        rows.append({"type": c["type"], "strike": c["strike"],
                     "expiry": c["expiry"], "gex": round(signed)})
    peak = max(by_strike, key=by_strike.get) if by_strike else None
    drivers = sorted((r for r in rows if (r["gex"] >= 0) == (net >= 0)),
                     key=lambda r: abs(r["gex"]), reverse=True)[:5]
    return net, peak, drivers


def scan_symbol(symbol: str, cfg: dict) -> list[dict]:
    spot, closes = provider.get_spot_and_history(symbol)
    contracts = provider.get_option_chain(
        symbol, cfg["max_expirations"], cfg["min_days_to_expiry"])
    if not contracts:
        log.warning("%s: no option contracts returned", symbol)
        return []

    call_vol = sum(c["volume"] for c in contracts if c["type"] == "call")
    put_vol = sum(c["volume"] for c in contracts if c["type"] == "put")
    net_gex, peak_strike, gex_drivers = _gamma_profile(contracts, spot)
    earnings = _next_earnings(symbol)
    days_to_earnings = (earnings - date.today()).days if earnings else None
    short_interest = _short_interest(symbol) or {}

    snap = {
        "symbol": symbol,
        "spot": spot,
        "atm_iv": _atm_iv(contracts, spot),
        "hv20": close_to_close_hv(closes, 20),
        "hv10": close_to_close_hv(closes, 10),
        "call_volume": call_vol,
        "put_volume": put_vol,
        "pc_ratio": round(put_vol / call_vol, 3) if call_vol > 0 else None,
        "net_gex": net_gex,
        "peak_gamma_strike": peak_strike,
        "skew": _skew(contracts, spot),
        "atm_dte": _nearest_dte(contracts),
        "next_earnings": earnings.isoformat() if earnings else None,
        "prev_close": _prev_close(closes),
        "short_pct_float": short_interest.get("pct_float"),
        "days_to_cover": short_interest.get("days_to_cover"),
    }

    # Baseline = snapshots taken BEFORE this scan, scoped to today's session so
    # overnight/weekend staleness doesn't blind the comparison detectors
    history = db.recent_snapshots(symbol, cfg["baseline_snapshots"],
                                  since_utc=market_clock.session_start_utc())
    db.insert_snapshot(snap)

    ctx = {
        "elapsed_fraction": market_clock.elapsed_fraction(),
        "pace_divisor": market_clock.pace_divisor(),
        "minutes_since_open": market_clock.minutes_since_open(),
        "gex_drivers": gex_drivers,
        "short_interest": short_interest,
    }
    found = detectors.run_all(snap, contracts, history, cfg["thresholds"], ctx)
    emitted = []
    for sig in found:
        # Don't re-alert the same condition on every scan tick; setup-style
        # kinds use their own (longer) windows
        cooldown = cfg.get(KIND_COOLDOWN_KEYS.get(sig["kind"], ""),
                           cfg.get("signal_cooldown_minutes", 45))
        if db.signal_fired_recently(symbol, sig["kind"], cooldown):
            continue
        message = sig["message"] + _catalyst_note(days_to_earnings, sig["kind"], cfg)
        db.insert_signal(symbol, sig["kind"], sig["severity"], message,
                         sig.get("value"), sig.get("details"))
        log.info("%s [%s] %s", symbol, sig["kind"], message)
        emitted.append(sig)
    if emitted:
        confluence = _check_confluence(symbol, cfg)
        if confluence:
            emitted.append(confluence)
    return emitted


_SEV_RANK = {"critical": 3, "warning": 2, "info": 1}


def _headline(sigs: list[dict]) -> dict | None:
    """Most significant signal of the day: confluence beats severity beats recency."""
    real = [s for s in sigs if s["kind"] != "daily_wrap"]
    if not real:
        return None
    best = max(real, key=lambda s: (
        4 if s["kind"] == "confluence" else _SEV_RANK.get(s["severity"], 0), s["id"]))
    text = best["message"]
    if len(text) > 130:
        text = text[:127].rsplit(" ", 1)[0] + "…"
    return {"kind": best["kind"], "text": text}


def generate_daily_wrap(force: bool = False) -> bool:
    """One pinned card summarizing every watchlist name's day. Runs at the
    cron tick after the close; `force` (manual /api/wrap) bypasses the
    once-per-day guard."""
    cfg = config.load()
    symbols = db.list_watchlist()
    if not symbols:
        return False
    if not force and db.signal_fired_recently("MARKET", "daily_wrap", 18 * 60):
        return False

    session_start = market_clock.session_start_utc()
    rows, quiet, stuck = [], [], []
    total_signals = total_confluences = 0
    for sym in symbols:
        snaps = db.recent_snapshots(sym, 500, since_utc=session_start)
        sigs = db.signals_since(sym, session_start)
        total_signals += len(sigs)
        confluences = sum(1 for s in sigs if s["kind"] == "confluence")
        total_confluences += confluences
        if not sigs:
            quiet.append(sym)

        last = snaps[0] if snaps else None
        # IV day change references the prior session's close, not today's
        # first scan — Yahoo IVs in the opening minutes are placeholder junk
        prior = db.last_snapshot_before(sym, session_start)
        day_chg = iv_now = iv_chg_pts = None
        if last:
            if last.get("spot") and last.get("prev_close"):
                day_chg = (last["spot"] - last["prev_close"]) / last["prev_close"]
            iv_now = last.get("atm_iv")
            if iv_now and prior and prior.get("atm_iv"):
                iv_chg_pts = (iv_now - prior["atm_iv"]) * 100
            if confluences:
                stuck.append(f"{sym} confluence unresolved into tomorrow")
            elif (iv_now and last.get("hv20")
                    and iv_now / last["hv20"] >= cfg["thresholds"]["iv_hv_ratio"]):
                stuck.append(f"{sym} IV still {iv_now / last['hv20']:.2f}x HV at the close")
        rows.append({
            "symbol": sym,
            "day_chg": round(day_chg, 4) if day_chg is not None else None,
            "atm_iv": round(iv_now, 4) if iv_now is not None else None,
            "iv_chg_pts": round(iv_chg_pts, 1) if iv_chg_pts is not None else None,
            "signals": len(sigs),
            "confluence": confluences > 0,
            "headline": _headline(sigs),
        })

    day = market_clock.now_et().strftime("%a %b %d")
    summary = (f"Daily wrap {day} — {len(symbols)} names, {total_signals} signals, "
               f"{total_confluences} confluence(s). "
               + ("What stuck: " + "; ".join(stuck) + "." if stuck
                  else "Nothing left elevated at the close."))
    details = json.dumps({
        "date": day, "names": len(symbols), "signals": total_signals,
        "confluences": total_confluences, "rows": rows,
        "stuck": stuck, "quiet": quiet,
    })
    db.insert_signal("MARKET", "daily_wrap", "info", summary, float(total_signals), details)
    log.info("daily wrap generated: %s", summary)
    return True


def _check_confluence(symbol: str, cfg: dict) -> dict | None:
    """Several independent detectors agreeing beats any single alert.

    Fires (with its own long cooldown) when enough distinct signal kinds
    have hit one symbol inside the rolling window.
    """
    window_min = cfg["confluence_window_hours"] * 60
    kinds = db.distinct_signal_kinds_since(symbol, window_min)
    if len(kinds) < cfg["confluence_min_kinds"]:
        return None
    if db.signal_fired_recently(symbol, "confluence", cfg["confluence_cooldown_minutes"]):
        return None
    pretty = ", ".join(k.replace("_", " ") for k in kinds)
    message = (f"Confluence: {len(kinds)} independent signal types inside "
               f"{cfg['confluence_window_hours']}h — {pretty}. Multiple detectors "
               f"agreeing is far stronger evidence than any single alert; "
               f"this name deserves a close look.")
    db.insert_signal(symbol, "confluence", "critical", message,
                     float(len(kinds)), json.dumps({"kinds": kinds}))
    log.info("%s [confluence] %s", symbol, message)
    return {"kind": "confluence", "severity": "critical", "message": message}


def scan_watchlist() -> dict:
    cfg = config.load()
    symbols = db.list_watchlist()
    results: dict[str, object] = {}
    for sym in symbols:
        try:
            results[sym] = {"signals": len(scan_symbol(sym, cfg)), "ok": True}
        except Exception as e:  # one bad ticker must not kill the sweep
            log.exception("scan failed for %s", sym)
            results[sym] = {"ok": False, "error": str(e)}
    purged = db.purge_old(cfg["snapshot_retention_days"], cfg["signal_retention_days"])
    if any(purged):
        log.info("retention purge removed %d snapshots, %d signals", *purged)
    return results
