"""Outcome scoring: did a signal's forward move beat the name's baseline?

Three signal flavors are scored differently so "good" always means the same
thing (the detector was right):
  - direction (dir != 0): did price move the predicted way? signed return.
  - magnitude (dir == 0): did a move happen at all? |return| vs baseline |move|.
  - stillness (gamma_pin): did it stay calmer than usual? smaller |move| = good.
"""
from datetime import date

STILLNESS_KINDS = {"gamma_pin"}


def forward_returns(closes, entry_date: date, horizons=(1, 5)) -> dict:
    """N-trading-day close-to-close returns from the first session on/after
    entry_date. Missing horizon (not enough forward data yet) -> None."""
    dates = [d.date() for d in closes.index]
    vals = [float(x) for x in closes.values]
    entry_idx = next((i for i, d in enumerate(dates) if d >= entry_date), None)
    out: dict[int, float | None] = {h: None for h in horizons}
    if entry_idx is None or not vals[entry_idx]:
        return out
    base = vals[entry_idx]
    for h in horizons:
        j = entry_idx + h
        if j < len(vals):
            out[h] = vals[j] / base - 1.0
    return out


def compute_baseline(closes, window: int) -> tuple:
    """Avg |move| and avg signed move at +1d/+5d over the trailing window —
    what this name does at any random moment, the edge comparison."""
    vals = [float(x) for x in closes.values]
    n = len(vals)
    start = max(0, n - window - 5)
    f1, f5 = [], []
    for i in range(start, n):
        if not vals[i]:
            continue
        if i + 1 < n:
            f1.append(vals[i + 1] / vals[i] - 1.0)
        if i + 5 < n:
            f5.append(vals[i + 5] / vals[i] - 1.0)
    avg = lambda v: sum(v) / len(v) if v else None       # noqa: E731
    avgabs = lambda v: sum(abs(x) for x in v) / len(v) if v else None  # noqa: E731
    return avgabs(f1), avgabs(f5), avg(f1), avg(f5)


# --- aggregation ---

def _kind_type(kind: str, rows: list[dict]) -> str:
    if kind in STILLNESS_KINDS:
        return "stillness"
    if any(r["dir"] for r in rows):
        return "direction"
    return "magnitude"


def _score(rows: list[dict], baselines: dict, horizon: int, ktype: str) -> dict:
    """Return {n, edge, good, hit} for one horizon. `edge` is the displayed
    number; `good` is the goodness score (positive = detector was right)."""
    key, b_abs, b_ret = f"ret_{horizon}d", f"base_abs_{horizon}d", f"base_ret_{horizon}d"
    done = [r for r in rows if r[f"done_{horizon}d"] and r[key] is not None]
    n = len(done)
    if n == 0:
        return {"n": 0, "edge": None, "good": None, "hit": None}

    if ktype == "direction":
        # Only rows with a known direction can be scored directionally
        # (backfilled rows carry dir=0 and would dilute the signed average)
        done = [r for r in done if r["dir"]]
        n = len(done)
        if n == 0:
            return {"n": 0, "edge": None, "good": None, "hit": None}
        contrib = [r["dir"] * r[key] for r in done]
        base = [r["dir"] * (baselines.get(r["symbol"], {}).get(b_ret) or 0.0) for r in done]
        edge = sum(contrib) / n
        good = edge - sum(base) / n
        hit = sum(1 for r in done if (r[key] > 0) == (r["dir"] > 0)) / n
        return {"n": n, "edge": edge, "good": good, "hit": hit}

    mean_abs = sum(abs(r[key]) for r in done) / n
    mean_base = sum((baselines.get(r["symbol"], {}).get(b_abs) or 0.0) for r in done) / n
    diff = mean_abs - mean_base
    good = (mean_base - mean_abs) if ktype == "stillness" else diff
    return {"n": n, "edge": diff, "good": good, "hit": None}


def _verdict(s5: dict, s1: dict, min_samples: int) -> str:
    s, scale = (s5, 1.0) if s5["n"] >= min_samples else \
               ((s1, 0.4) if s1["n"] >= min_samples else (None, 0))
    if s is None or s["good"] is None:
        return "collecting"
    g = s["good"]
    if g >= 0.010 * scale:
        return "trust"
    if g >= 0.003 * scale:
        return "promising"
    if g > -0.003 * scale:
        return "weak"
    return "noise"


def aggregate(outcomes: list[dict], baselines: dict, min_samples: int) -> dict:
    by_kind: dict[str, list] = {}
    by_name: dict[str, list] = {}
    for o in outcomes:
        by_kind.setdefault(o["kind"], []).append(o)
        by_name.setdefault(o["symbol"], []).append(o)

    signals = []
    for kind, rows in by_kind.items():
        ktype = _kind_type(kind, rows)
        s5, s1 = _score(rows, baselines, 5, ktype), _score(rows, baselines, 1, ktype)
        signals.append({
            "kind": kind, "type": ktype, "n": len(rows),
            "edge_1d": s1["edge"], "edge_5d": s5["edge"],
            "good_5d": s5["good"], "hit": s5["hit"],
            "verdict": _verdict(s5, s1, min_samples),
        })
    signals.sort(key=lambda r: ({"trust": 0, "promising": 1, "weak": 2,
                                 "noise": 3, "collecting": 4}[r["verdict"]], -r["n"]))

    # The name table and heatmap use one horizon for consistency: +5d once
    # enough has matured, else +1d so the view is useful while 5d fills in.
    matured_5d = sum(1 for o in outcomes if o["done_5d"] and o["ret_5d"] is not None)
    h = 5 if matured_5d >= min_samples else 1
    rk, dk, bk = f"ret_{h}d", f"done_{h}d", f"base_abs_{h}d"

    names = []
    for sym, rows in by_name.items():
        s = _score(rows, baselines, h, "magnitude")  # name-level = did moves follow
        dir_rows = [r for r in rows if r["dir"] and r[dk] and r[rk] is not None]
        hit = (sum(1 for r in dir_rows if (r[rk] > 0) == (r["dir"] > 0)) / len(dir_rows)
               if dir_rows else None)
        best, best_good = None, None
        for kind in {r["kind"] for r in rows}:
            krows = [r for r in rows if r["kind"] == kind]
            ks = _score(krows, baselines, h, _kind_type(kind, krows))
            if ks["n"] >= 3 and ks["good"] is not None and (best_good is None or ks["good"] > best_good):
                best, best_good = kind, ks["good"]
        names.append({
            "symbol": sym, "fires": len(rows),
            "mean_abs": (sum(abs(r[rk]) for r in rows if r[dk] and r[rk] is not None) / s["n"]
                         if s["n"] else None),
            "base_abs": baselines.get(sym, {}).get(bk),
            "hit": hit, "top_signal": best,
            "verdict": _verdict(_score(rows, baselines, 5, "magnitude"),
                                _score(rows, baselines, 1, "magnitude"), min_samples),
        })
    names.sort(key=lambda r: -(r["mean_abs"] or 0))

    # heatmap: name x kind goodness, same horizon
    kinds_by_n = [k for k, _ in sorted(by_kind.items(), key=lambda kv: -len(kv[1]))]
    heat_rows = []
    for sym, rows in by_name.items():
        cells = {}
        for kind in kinds_by_n:
            krows = [r for r in rows if r["kind"] == kind]
            if not krows:
                continue
            ks = _score(krows, baselines, h, _kind_type(kind, krows))
            cells[kind] = {"good": ks["good"], "edge": ks["edge"], "n": ks["n"]}
        heat_rows.append({"symbol": sym, "cells": cells})
    heat_rows.sort(key=lambda r: -sum((c["good"] or 0) for c in r["cells"].values()))

    return {
        "by_signal": signals,
        "by_name": names,
        "heatmap": {"kinds": kinds_by_n, "rows": heat_rows},
        "min_samples": min_samples,
        "horizon": h,
    }
