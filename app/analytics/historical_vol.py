"""Realized (historical) volatility from daily closes."""
import numpy as np
import pandas as pd

TRADING_DAYS = 252


def close_to_close_hv(closes: pd.Series, window: int) -> float | None:
    """Annualized close-to-close volatility over the trailing `window` sessions."""
    closes = closes.dropna()
    if len(closes) < window + 1:
        return None
    log_ret = np.log(closes / closes.shift(1)).dropna().tail(window)
    if len(log_ret) < window:
        return None
    return float(log_ret.std(ddof=1) * np.sqrt(TRADING_DAYS))
