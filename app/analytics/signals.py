"""Signal detectors.

Each detector takes the freshly computed snapshot (plus supporting chain data),
the recent same-session snapshot history for that symbol, and a session context
(`ctx`: elapsed_fraction, pace_divisor, minutes_since_open), and returns zero
or more signal dicts: {kind, severity, message, value, details}.

Volume accumulates over the trading day, so raw volume-based thresholds are
biased: too strict at 10:00, too loose at 15:30. Detectors that use volume
project it to full-day pace via ctx["pace_divisor"].
"""
import json


def _baseline(history: list[dict], field: str) -> float | None:
    """Average of a field over prior snapshots (history excludes the current one)."""
    vals = [s[field] for s in history if s.get(field) is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def detect_iv_premium(snap: dict, history: list[dict], th: dict) -> list[dict]:
    out = []
    iv, hv = snap.get("atm_iv"), snap.get("hv20")
    if not iv or not hv or hv <= 0:
        return out
    ratio = iv / hv
    if ratio >= th["iv_hv_ratio"]:
        sev = "critical" if ratio >= th["iv_hv_ratio"] * 1.3 else "warning"
        out.append({
            "kind": "iv_premium",
            "severity": sev,
            "message": (f"ATM IV {iv:.1%} is {ratio:.2f}x 20d HV {hv:.1%} — "
                        f"options pricing in a move well above realized"),
            "value": round(ratio, 3),
            "details": json.dumps({"atm_iv": iv, "hv20": hv}),
        })
    return out


def detect_iv_spike(snap: dict, history: list[dict], th: dict) -> list[dict]:
    out = []
    iv = snap.get("atm_iv")
    base = _baseline(history, "atm_iv")
    if not iv or not base or base <= 0:
        return out
    chg = (iv - base) / base
    if chg >= th["iv_spike_pct"]:
        sev = "critical" if chg >= th["iv_spike_pct"] * 2 else "warning"
        out.append({
            "kind": "iv_spike",
            "severity": sev,
            "message": (f"ATM IV rising: {base:.1%} → {iv:.1%} "
                        f"(+{chg:.1%} vs recent scans)"),
            "value": round(chg, 4),
            "details": json.dumps({"atm_iv": iv, "baseline_iv": base}),
        })
    return out


def detect_unusual_volume(snap: dict, contracts: list[dict], th: dict,
                          ctx: dict) -> list[dict]:
    """Contracts on pace to trade a large multiple of their open interest.

    Both the vol/OI ratio and the minimum-volume floor are evaluated at
    projected full-day pace, so a burst at 10:30 and the same burst at 15:30
    are judged on equal footing.
    """
    pace = ctx.get("pace_divisor", 1.0)
    min_vol = th["uoa_min_volume"] * pace
    hits = []
    for c in contracts:
        vol, oi = c.get("volume") or 0, c.get("open_interest") or 0
        if vol < min_vol:
            continue
        ratio = (vol / pace) / oi if oi > 0 else float("inf")
        if ratio >= th["uoa_vol_oi_ratio"]:
            hits.append({**c, "vol_oi_pace": round(min(ratio, 999.0), 2)})
    if not hits:
        return []
    hits.sort(key=lambda c: c["vol_oi_pace"], reverse=True)
    top = hits[:5]
    lead = top[0]
    pace_note = "" if pace >= 1.0 else " at day pace"
    return [{
        "kind": "unusual_volume",
        "severity": "critical" if lead["vol_oi_pace"] >= th["uoa_vol_oi_ratio"] * 3 else "warning",
        "message": (f"{len(hits)} contract(s) running ≥ {th['uoa_vol_oi_ratio']}x OI{pace_note}; "
                    f"top: {lead['type']} ${lead['strike']:g} {lead['expiry']} "
                    f"vol {lead['volume']:,} ({lead['vol_oi_pace']}x OI{pace_note}) "
                    f"vs OI {lead['open_interest']:,}"),
        "value": lead["vol_oi_pace"],
        "details": json.dumps(top),
    }]


def detect_pc_ratio(snap: dict, history: list[dict], th: dict, ctx: dict) -> list[dict]:
    pc = snap.get("pc_ratio")
    if pc is None:
        return []
    # The ratio is meaningless on a handful of opening prints
    mins = ctx.get("minutes_since_open")
    if mins is not None and 0 <= mins < th["pc_warmup_minutes"]:
        return []
    total_vol = (snap.get("call_volume") or 0) + (snap.get("put_volume") or 0)
    if total_vol < th["pc_min_total_volume"]:
        return []
    if pc >= th["pc_ratio_high"]:
        return [{
            "kind": "put_call_ratio",
            "severity": "warning",
            "message": f"Put/call volume ratio elevated at {pc:.2f} — heavy put activity",
            "value": pc,
            "details": None,
        }]
    if pc <= th["pc_ratio_low"]:
        return [{
            "kind": "put_call_ratio",
            "severity": "warning",
            "message": f"Put/call volume ratio depressed at {pc:.2f} — heavy call activity",
            "value": pc,
            "details": None,
        }]
    return []


def detect_gamma(snap: dict, history: list[dict], th: dict) -> list[dict]:
    out = []
    gex = snap.get("net_gex")
    prev = history[0] if history else None
    if gex is not None and prev and prev.get("net_gex"):
        prev_gex = prev["net_gex"]
        if abs(prev_gex) > 0:
            chg = (abs(gex) - abs(prev_gex)) / abs(prev_gex)
            if chg >= th["gamma_change_pct"]:
                out.append({
                    "kind": "gamma_build",
                    "severity": "warning",
                    "message": (f"Net gamma exposure building: {prev_gex:,.0f} → {gex:,.0f} "
                                f"(+{chg:.0%} since last scan)"),
                    "value": round(chg, 3),
                    "details": json.dumps({"net_gex": gex, "prev_gex": prev_gex}),
                })
    peak, spot = snap.get("peak_gamma_strike"), snap.get("spot")
    if peak and spot:
        dist = abs(peak - spot) / spot
        if dist <= th["gamma_pin_distance_pct"]:
            out.append({
                "kind": "gamma_pin",
                "severity": "info",
                "message": (f"Peak gamma strike ${peak:g} sits {dist:.1%} from spot "
                            f"${spot:,.2f} — potential pinning / magnet into expiry"),
                "value": round(dist, 4),
                "details": json.dumps({"peak_strike": peak, "spot": spot}),
            })
    return out


def detect_skew_shift(snap: dict, history: list[dict], th: dict) -> list[dict]:
    skew = snap.get("skew")
    base = _baseline(history, "skew")
    if skew is None or base is None:
        return []
    shift = skew - base
    if abs(shift) >= th["skew_shift_pts"]:
        direction = "puts bid (downside fear)" if shift > 0 else "calls bid (upside chase)"
        return [{
            "kind": "skew_shift",
            "severity": "warning",
            "message": (f"IV skew moved {shift:+.1%} vs recent scans — {direction}"),
            "value": round(shift, 4),
            "details": json.dumps({"skew": skew, "baseline_skew": base}),
        }]
    return []


def run_all(snap: dict, contracts: list[dict], history: list[dict], th: dict,
            ctx: dict) -> list[dict]:
    signals = []
    signals += detect_iv_premium(snap, history, th)
    signals += detect_iv_spike(snap, history, th)
    signals += detect_unusual_volume(snap, contracts, th, ctx)
    signals += detect_pc_ratio(snap, history, th, ctx)
    signals += detect_gamma(snap, history, th)
    signals += detect_skew_shift(snap, history, th)
    return signals
