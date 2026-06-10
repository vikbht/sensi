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

    @abstractmethod
    def get_spot_and_history(self, symbol: str) -> tuple[float, pd.Series]:
        """Return (last price, daily close series covering >= 60 sessions)."""

    @abstractmethod
    def get_option_chain(self, symbol: str, max_expirations: int,
                         min_days_to_expiry: int) -> list[dict]:
        """Return a flat list of contract dicts:
        {type: 'call'|'put', expiry: 'YYYY-MM-DD', dte: int, strike: float,
         iv: float, volume: int, open_interest: int, last: float}
        """
