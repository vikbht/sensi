"""Free delayed data from Yahoo Finance via yfinance."""
from datetime import date, datetime

import pandas as pd
import yfinance as yf

from .base import OptionsDataProvider


class YFinanceProvider(OptionsDataProvider):
    def get_spot_and_history(self, symbol: str) -> tuple[float, pd.Series]:
        t = yf.Ticker(symbol)
        hist = t.history(period="4mo", interval="1d", auto_adjust=True)
        if hist.empty:
            raise ValueError(f"No price history for {symbol}")
        closes = hist["Close"]
        return float(closes.iloc[-1]), closes

    def get_next_earnings(self, symbol: str) -> date | None:
        try:
            cal = yf.Ticker(symbol).calendar
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
            info = yf.Ticker(symbol).get_info()
            pct = info.get("shortPercentOfFloat")
            dtc = info.get("shortRatio")
            if pct is None and dtc is None:
                return None
            return {
                "pct_float": float(pct) if pct is not None else None,
                "days_to_cover": float(dtc) if dtc is not None else None,
            }
        except Exception:
            return None

    def get_option_chain(self, symbol: str, max_expirations: int,
                         min_days_to_expiry: int) -> list[dict]:
        t = yf.Ticker(symbol)
        expirations = t.options or ()
        today = date.today()
        contracts: list[dict] = []
        picked = 0
        for exp in expirations:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            if dte < min_days_to_expiry:
                continue
            if picked >= max_expirations:
                break
            picked += 1
            chain = t.option_chain(exp)
            for opt_type, df in (("call", chain.calls), ("put", chain.puts)):
                for row in df.itertuples():
                    iv = getattr(row, "impliedVolatility", None)
                    contracts.append({
                        "type": opt_type,
                        "expiry": exp,
                        "dte": dte,
                        "strike": float(row.strike),
                        "iv": float(iv) if iv and iv > 0.001 else None,
                        "volume": int(row.volume) if pd.notna(row.volume) else 0,
                        "open_interest": int(row.openInterest) if pd.notna(row.openInterest) else 0,
                        "last": float(row.lastPrice) if pd.notna(row.lastPrice) else None,
                    })
        return contracts
