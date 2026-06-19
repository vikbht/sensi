"""IV rank and percentile — where today's IV sits in its own history.

rank     = position in the trailing high-low range (0 = at the low, 100 = at
           the high); sensitive to outliers.
percentile = share of prior sessions whose IV was at or below today's; robust.
"""
import statistics


def clean_series(pairs: list[tuple[str, float]]) -> list[tuple[str, float]]:
    """Drop implausible IVs (Yahoo glitches — e.g. a stray 3% reading on a
    90%-IV name) that would otherwise blow out the rank range. Outliers are
    judged against the median, so normal variation is untouched. The current
    (last) reading is always kept."""
    vals = [v for _, v in pairs if v and v > 0.01]
    if len(vals) < 3:
        return pairs
    med = statistics.median(vals)
    lo, hi = 0.2 * med, 5 * med
    return [p for i, p in enumerate(pairs)
            if i == len(pairs) - 1 or (p[1] and lo <= p[1] <= hi)]


def rank_and_pctile(values: list[float]) -> dict | None:
    """`values` = daily ATM IV history, oldest first, current last."""
    if len(values) < 2:
        return None
    cur, lo, hi = values[-1], min(values), max(values)
    rank = (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0
    hist = values[:-1]
    pctile = sum(1 for v in hist if v <= cur) / len(hist) * 100 if hist else None
    return {
        "rank": round(rank, 1),
        "pctile": round(pctile, 1) if pctile is not None else None,
        "min": lo, "max": hi, "n": len(values),
    }
