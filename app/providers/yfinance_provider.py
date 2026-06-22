"""Free delayed data from Yahoo Finance via yfinance."""
import logging
import time
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from .base import OptionsDataProvider

log = logging.getLogger("sensi.yfinance")

_RETRIES = 3
_BACKOFF_S = 1.0


def _retry(fn, what: str):
    """yfinance flakes under rate-limiting; retry transient failures briefly."""
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 — transient, retried below
            last = e
            if attempt < _RETRIES - 1:
                time.sleep(_BACKOFF_S * (attempt + 1))
    log.warning("%s failed after %d attempts: %s", what, _RETRIES, last)
    raise last


def _f(v) -> float | None:
    """float or None — tolerates NaN, None, and unparseable values."""
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int:
    """int or 0 — tolerates NaN, None, and unparseable values."""
    f = _f(v)
    return int(f) if f is not None else 0


class YFinanceProvider(OptionsDataProvider):
    def get_spot_and_history(self, symbol: str) -> tuple[float, pd.Series]:
        t = yf.Ticker(symbol)
        hist = _retry(
            lambda: t.history(period="4mo", interval="1d", auto_adjust=True),
            f"{symbol} history")
        closes = hist["Close"].dropna() if not hist.empty else pd.Series(dtype=float)
        if closes.empty:
            raise ValueError(f"No price history for {symbol}")
        return float(closes.iloc[-1]), closes

    def get_next_earnings(self, symbol: str) -> date | None:
        try:
            cal = _retry(lambda: yf.Ticker(symbol).calendar, f"{symbol} calendar")
            dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if not dates:
                return None
            today = date.today()
            future = [d for d in dates if d >= today]
            return min(future) if future else None
        except Exception:
            return None  # unknown — caller must not claim "no catalyst"

    def get_short_interest(self, symbol: str) -> dict | None:
        try:
            info = _retry(lambda: yf.Ticker(symbol).get_info(), f"{symbol} info")
            pct = info.get("shortPercentOfFloat")
            dtc = info.get("shortRatio")
            if pct is None and dtc is None:
                return None
            return {"pct_float": _f(pct), "days_to_cover": _f(dtc)}
        except Exception:
            return None

    def get_option_chain(self, symbol: str, dte_min: int, dte_max: int,
                         max_expirations: int) -> list[dict]:
        t = yf.Ticker(symbol)
        try:
            expirations = _retry(lambda: t.options, f"{symbol} expirations") or ()
        except Exception:
            return []  # no chain at all — caller treats as "no contracts"

        today = date.today()
        contracts: list[dict] = []
        picked = 0
        for exp in expirations:  # yfinance returns these in ascending date order
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            if dte < dte_min:
                continue
            if dte > dte_max or picked >= max_expirations:
                break  # past the window (sorted) or hit the rate-limit cap
            try:
                chain = _retry(lambda e=exp: t.option_chain(e), f"{symbol} {exp}")
            except Exception:
                continue  # skip a bad expiry rather than failing the whole symbol
            picked += 1
            for opt_type, df in (("call", chain.calls), ("put", chain.puts)):
                for row in df.itertuples():
                    strike = _f(getattr(row, "strike", None))
                    if strike is None:
                        continue  # a contract with no strike is unusable
                    iv = _f(getattr(row, "impliedVolatility", None))
                    bid = _f(getattr(row, "bid", None))
                    ask = _f(getattr(row, "ask", None))
                    contracts.append({
                        "type": opt_type,
                        "expiry": exp,
                        "dte": dte,
                        "strike": strike,
                        "iv": iv if iv and iv > 0.001 else None,
                        "volume": _i(getattr(row, "volume", None)),
                        "open_interest": _i(getattr(row, "openInterest", None)),
                        "last": _f(getattr(row, "lastPrice", None)),
                        "bid": bid,
                        "ask": ask,
                        # A live two-sided quote is the cleanest staleness check
                        "has_quote": (bid is not None and bid > 0)
                        or (ask is not None and ask > 0),
                    })
        return contracts
