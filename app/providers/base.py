"""Provider interface. Implement this against Polygon/Tradier/ORATS for
real-time or tick-level data; the bundled yfinance provider is free but delayed.
"""
from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class OptionsDataProvider(ABC):
    def get_next_earnings(self, symbol: str) -> date | None:
        """Next scheduled earnings date, or None if unknown/none scheduled.
        Optional — providers without calendar data can keep the default."""
        return None

    def get_short_interest(self, symbol: str) -> dict | None:
        """{'pct_float': 0.14, 'days_to_cover': 6.1} or None if unknown.
        Optional — FINRA data is bi-monthly, so staleness up to ~2 weeks
        is expected from any free source."""
        return None

    @abstractmethod
    def get_spot_and_history(self, symbol: str) -> tuple[float, pd.Series]:
        """Return (last price, daily close series covering >= 60 sessions)."""

    @abstractmethod
    def get_option_chain(self, symbol: str, dte_min: int, dte_max: int,
                         max_expirations: int) -> list[dict]:
        """Return a flat list of contract dicts for every expiration whose DTE
        is in [dte_min, dte_max], capped at max_expirations:
        {type: 'call'|'put', expiry: 'YYYY-MM-DD', dte: int, strike: float,
         iv: float, volume: int, open_interest: int, last: float}
        """
