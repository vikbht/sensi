"""Black-Scholes greeks. Yahoo supplies per-contract IV, so no solver is needed."""
import math


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1(spot: float, strike: float, t: float, iv: float, r: float) -> float:
    return (math.log(spot / strike) + (r + 0.5 * iv * iv) * t) / (iv * math.sqrt(t))


def gamma(spot: float, strike: float, t_years: float, iv: float, r: float = 0.045) -> float:
    """Per-share gamma; identical for calls and puts."""
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = _d1(spot, strike, t_years, iv, r)
    return _norm_pdf(d1) / (spot * iv * math.sqrt(t_years))


def delta(spot: float, strike: float, t_years: float, iv: float,
          is_call: bool, r: float = 0.045) -> float:
    if t_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = _d1(spot, strike, t_years, iv, r)
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return nd1 if is_call else nd1 - 1.0
