"""US equity session clock (America/New_York).

Weekday + hours only; exchange holidays are not modeled, so a holiday scan
behaves like a normal session on stale data (the signal cooldown suppresses
the repeats that would otherwise produce).
"""
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SESSION_OPEN = time(9, 30)
SESSION_CLOSE = time(16, 0)
SESSION_MINUTES = 390.0  # 9:30 -> 16:00

# Below this fraction of the session, volume projections get too wild to trust
MIN_ELAPSED_FRACTION = 0.25


def now_et() -> datetime:
    return datetime.now(ET)


def is_open(dt: datetime | None = None) -> bool:
    dt = dt or now_et()
    if dt.weekday() >= 5:
        return False
    return SESSION_OPEN <= dt.time() < SESSION_CLOSE


def minutes_since_open(dt: datetime | None = None) -> float:
    """Minutes since today's 9:30 ET open. Negative pre-open; >390 post-close."""
    dt = dt or now_et()
    open_dt = dt.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute,
                         second=0, microsecond=0)
    return (dt - open_dt).total_seconds() / 60.0


def elapsed_fraction(dt: datetime | None = None) -> float:
    """Fraction of today's session elapsed, clamped to [0, 1]."""
    return min(max(minutes_since_open(dt) / SESSION_MINUTES, 0.0), 1.0)


def pace_divisor(dt: datetime | None = None) -> float:
    """Divide today's running volume by this to project full-day pace.

    Clamped so the first ~90 minutes don't project absurd multiples.
    """
    return max(elapsed_fraction(dt), MIN_ELAPSED_FRACTION)


def session_start_utc(dt: datetime | None = None) -> str:
    """Today's 9:30 ET open as a UTC 'YYYY-MM-DD HH:MM:SS' string, the same
    format SQLite's datetime('now') writes — used to scope baselines to the
    current session."""
    dt = dt or now_et()
    open_dt = dt.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute,
                         second=0, microsecond=0)
    return open_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
