"""Runtime configuration, persisted to config.json next to the project root."""
import json
import threading
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

DEFAULTS = {
    # Scanner cadence
    "scan_interval_minutes": 5,
    # Only run scheduled scans 9:30-16:00 ET Mon-Fri (manual "Scan now" always runs)
    "market_hours_only": True,
    # Age-based cleanup, applied after each sweep. Snapshots stay longer —
    # they're the raw history future detectors (IV rank etc.) will need;
    # signals are ephemeral alerts and just become clutter.
    "snapshot_retention_days": 30,
    "signal_retention_days": 5,
    # How many expirations to pull per symbol (keep small to stay under rate limits)
    "max_expirations": 3,
    # Skip expirations closer than this many days (expiry-day IV is noisy)
    "min_days_to_expiry": 2,
    # Signal thresholds
    "thresholds": {
        # ATM IV must exceed 20d HV by this ratio to flag an IV premium
        "iv_hv_ratio": 1.25,
        # Relative ATM IV change vs the average of recent snapshots (0.10 = +10%)
        "iv_spike_pct": 0.10,
        # Contract volume must be at least this multiple of open interest
        "uoa_vol_oi_ratio": 2.0,
        # ...and at least this many contracts, to ignore illiquid noise
        "uoa_min_volume": 500,
        # Put/call volume ratio bounds
        "pc_ratio_high": 2.0,
        "pc_ratio_low": 0.4,
        # P/C ratio is noise right at the open; wait this long into the session
        "pc_warmup_minutes": 45,
        # ...and require this much combined volume before trusting the ratio
        "pc_min_total_volume": 1000,
        # Relative change in net gamma exposure vs previous snapshot
        "gamma_change_pct": 0.25,
        # Flip/build signals need this much |GEX| ($ per 1% move) to matter;
        # below it, dealer hedging is too small to move the tape
        "gamma_min_gex": 5_000_000,
        # Strike with peak gamma must be within this % of spot to flag pin risk
        "gamma_pin_distance_pct": 0.02,
        # Change in put-call IV skew (vol points) vs recent average
        "skew_shift_pts": 0.04,
    },
    # How many recent snapshots form the comparison baseline
    "baseline_snapshots": 6,
    # Suppress repeat alerts of the same kind for a symbol within this window
    "signal_cooldown_minutes": 45,
    # Earnings within this many days = "event premium likely" tag on signals
    "earnings_window_days": 7,
    # IV signals with a KNOWN earnings date beyond this get the
    # "no obvious catalyst" tag (the interesting case)
    "no_catalyst_window_days": 14,
    # Confluence: this many distinct signal kinds on one symbol within the
    # window emits a critical meta-signal (own cooldown so it fires once
    # per cluster, not once per scan)
    "confluence_min_kinds": 3,
    "confluence_window_hours": 4,
    "confluence_cooldown_minutes": 240,
}

_lock = threading.Lock()


def load() -> dict:
    with _lock:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text())
            # Backfill any keys added after the file was written
            merged = {**DEFAULTS, **cfg}
            merged["thresholds"] = {**DEFAULTS["thresholds"], **cfg.get("thresholds", {})}
            return merged
        return json.loads(json.dumps(DEFAULTS))


def save(cfg: dict) -> dict:
    merged = {**DEFAULTS, **cfg}
    merged["thresholds"] = {**DEFAULTS["thresholds"], **cfg.get("thresholds", {})}
    with _lock:
        CONFIG_PATH.write_text(json.dumps(merged, indent=2))
    return merged
