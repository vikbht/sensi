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
from datetime import datetime


def _dollars(x: float) -> str:
    """Signed, humanized dollars: +$205M, -$1.2B."""
    a = abs(x)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            body = f"${a / div:.1f}{suffix}"
            break
    else:
        body = f"${a:.0f}"
    return ("-" if x < 0 else "+") + body


def _fmt_drivers(drivers: list[dict], limit: int = 2) -> str:
    """'$100P Jun 20 (-$85.3K), $105P Jun 13 (-$22.1K)' from driver dicts."""
    parts = []
    for d in drivers[:limit]:
        try:
            exp = datetime.strptime(d["expiry"], "%Y-%m-%d").strftime("%b %d")
        except (ValueError, KeyError):
            exp = d.get("expiry", "?")
        side = "C" if d["type"] == "call" else "P"
        parts.append(f"${d['strike']:g}{side} {exp} ({_dollars(d['gex'])})")
    return ", ".join(parts)


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
            "message": (f"ATM IV {iv:.1%} is {ratio:.2f}x the 20d realized vol {hv:.1%} — "
                        f"options price in far more movement than the stock has "
                        f"delivered: either a catalyst is expected, or premium "
                        f"is rich for sellers"),
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
        # What the stock did alongside changes the meaning entirely
        spot, spot_base = snap.get("spot"), _baseline(history, "spot")
        px_note = ""
        if spot and spot_base:
            px_chg = (spot - spot_base) / spot_base
            if abs(px_chg) < 0.01:
                px_note = (f" with the stock flat ({px_chg:+.1%}) — vol getting "
                           f"bid without a price move often precedes news")
            elif px_chg < 0:
                px_note = (f" alongside a {px_chg:.1%} slide — looks like "
                           f"reactive hedging on the way down")
            else:
                px_note = (f" alongside a {px_chg:+.1%} rally — upside "
                           f"being chased")
        out.append({
            "kind": "iv_spike",
            "severity": sev,
            "message": (f"ATM IV climbing {base:.1%} → {iv:.1%} "
                        f"(+{chg:.0%} vs recent scans){px_note}"),
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

    n_calls = sum(1 for h in hits if h["type"] == "call")
    n_puts = len(hits) - n_calls
    split = (f"{n_calls} calls / {n_puts} puts" if n_calls and n_puts
             else "all calls" if n_calls else "all puts")

    spot = snap.get("spot")
    mny = ""
    if spot:
        d = (lead["strike"] - spot) / spot
        if abs(d) < 0.01:
            mny = ", at the money"
        else:
            otm = d > 0 if lead["type"] == "call" else d < 0
            mny = f", {abs(d):.0%} {'OTM' if otm else 'ITM'}"
    # Volume dwarfing a near-empty strike = a newly active line, not turnover
    tail = (" on a nearly empty strike — a freshly active line"
            if lead["open_interest"] < 100
            else " — volume far above OI means new positions opening, not closing")

    return [{
        "kind": "unusual_volume",
        "severity": "critical" if lead["vol_oi_pace"] >= th["uoa_vol_oi_ratio"] * 3 else "warning",
        "message": (f"{len(hits)} contract(s) running ≥ {th['uoa_vol_oi_ratio']}x OI{pace_note} "
                    f"({split}); biggest: {lead['type']}s ${lead['strike']:g} "
                    f"exp {lead['expiry']} ({lead['dte']}d{mny}), "
                    f"{lead['volume']:,} traded vs {lead['open_interest']:,} OI "
                    f"({lead['vol_oi_pace']}x{pace_note}){tail}"),
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
    cv = snap.get("call_volume") or 0
    pv = snap.get("put_volume") or 0
    if pc >= th["pc_ratio_high"]:
        return [{
            "kind": "put_call_ratio",
            "severity": "warning",
            "message": (f"Puts trading {pc:.1f}x calls today ({pv:,} vs {cv:,}) — "
                        f"one-sided downside flow: protection being bought "
                        f"or bearish bets building"),
            "value": pc,
            "details": None,
        }]
    if pc <= th["pc_ratio_low"]:
        ratio = cv / pv if pv else float("inf")
        ratio_txt = f"{ratio:.1f}x" if pv else "∞x"
        return [{
            "kind": "put_call_ratio",
            "severity": "warning",
            "message": (f"Calls trading {ratio_txt} puts today ({cv:,} vs {pv:,}) — "
                        f"one-sided upside flow: speculative call buying "
                        f"dominating the tape"),
            "value": pc,
            "details": None,
        }]
    return []


def detect_gamma(snap: dict, history: list[dict], th: dict,
                 ctx: dict | None = None) -> list[dict]:
    out = []
    gex = snap.get("net_gex")
    prev = history[0] if history else None
    prev_gex = prev.get("net_gex") if prev else None
    drivers = (ctx or {}).get("gex_drivers") or []
    # Below the materiality floor, dealer hedging is too small to move the
    # tape — a sign flip between two ~zero values is not a regime change
    material = (gex is not None and prev_gex
                and max(abs(gex), abs(prev_gex)) >= th.get("gamma_min_gex", 5e6))
    if material:
        flipped = (gex > 0) != (prev_gex > 0) and gex != 0
        swing = abs(gex - prev_gex) / abs(prev_gex)
        chg = (abs(gex) - abs(prev_gex)) / abs(prev_gex)
        driver_note = f", driven by {_fmt_drivers(drivers)}" if drivers else ""
        if flipped and swing >= th["gamma_change_pct"]:
            # A sign change is a regime change — the most consequential gamma
            # event, and invisible to the |gex| growth check below
            if gex < 0:
                msg = (f"Gamma regime flipped negative: net GEX {_dollars(prev_gex)} → "
                       f"{_dollars(gex)}{driver_note}. Dealer hedging now amplifies "
                       f"moves — expect faster, trendier price action and squeeze risk.")
                sev = "critical"
            else:
                msg = (f"Gamma regime flipped positive: net GEX {_dollars(prev_gex)} → "
                       f"{_dollars(gex)}{driver_note}. Dealer hedging now dampens "
                       f"moves — favors range-bound, pinned trading.")
                sev = "warning"
            out.append({
                "kind": "gamma_flip",
                "severity": sev,
                "message": msg,
                "value": round(swing, 3),
                "details": json.dumps({"net_gex": gex, "prev_gex": prev_gex,
                                       "drivers": drivers}),
            })
        elif chg >= th["gamma_change_pct"]:
            if gex > 0:
                flavor = ("stabilizing gamma — dealer hedging leans harder against "
                          "moves, favoring range-bound, pinned trading")
            else:
                flavor = ("destabilizing gamma — dealer hedging amplifies moves, "
                          "raising swing and squeeze risk")
            led_note = f"; led by {_fmt_drivers(drivers)}" if drivers else ""
            out.append({
                "kind": "gamma_build",
                "severity": "warning",
                "message": (f"Net GEX {_dollars(prev_gex)} → {_dollars(gex)} "
                            f"(+{chg:.0%} since last scan): building {flavor}{led_note}."),
                "value": round(chg, 3),
                "details": json.dumps({"net_gex": gex, "prev_gex": prev_gex,
                                       "drivers": drivers}),
            })
    peak, spot = snap.get("peak_gamma_strike"), snap.get("spot")
    if peak and spot:
        dist = abs(peak - spot) / spot
        if dist <= th["gamma_pin_distance_pct"]:
            out.append({
                "kind": "gamma_pin",
                "severity": "info",
                "message": (f"Peak gamma strike ${peak:g} sits {dist:.1%} from spot "
                            f"${spot:,.2f} — hedging flows tend to hold price near "
                            f"that strike into expiry: expect stickiness, with "
                            f"small moves away getting faded"),
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
        if shift > 0:
            read = ("downside protection getting bid — hedging demand or "
                    "fear rising; most telling if the stock isn't falling")
        else:
            read = ("calls getting bid relative to puts — upside being "
                    "chased; the footprint of speculation or accumulation")
        return [{
            "kind": "skew_shift",
            "severity": "warning",
            "message": (f"Skew (put−call IV) moved {base:+.1%} → {skew:+.1%} "
                        f"({shift * 100:+.1f} vol pts vs recent scans): {read}"),
            "value": round(shift, 4),
            "details": json.dumps({"skew": skew, "baseline_skew": base}),
        }]
    return []


def detect_squeeze_setup(snap: dict, contracts: list[dict], history: list[dict],
                         th: dict, ctx: dict) -> list[dict]:
    """Composite: short-interest fuel (mandatory) + enough flow conditions.

    Setups persist for days, so this kind carries a long cooldown upstream.
    """
    si = (ctx or {}).get("short_interest") or {}
    pct = si.get("pct_float")
    if not pct or pct < th["squeeze_min_short_float"]:
        return []

    conditions = []
    pc = snap.get("pc_ratio")
    if pc is not None and pc <= th["pc_ratio_low"] * 1.25:
        conditions.append(f"calls dominating flow (P/C {pc:.2f})")

    spot = snap.get("spot")
    pace = (ctx or {}).get("pace_divisor", 1.0)
    hot_calls = []
    for c in contracts:
        vol, oi = c.get("volume") or 0, c.get("open_interest") or 0
        if (c["type"] == "call" and spot and c["strike"] > spot
                and c["dte"] <= 14 and vol >= th["uoa_min_volume"] * pace
                and oi > 0 and (vol / pace) / oi >= th["uoa_vol_oi_ratio"]):
            hot_calls.append(c)
    if hot_calls:
        lead = max(hot_calls, key=lambda c: (c["volume"] / pace) / c["open_interest"])
        conditions.append(
            f"fresh OTM call buying (${lead['strike']:g}C {lead['expiry']}, "
            f"{lead['volume']:,} traded vs {lead['open_interest']:,} OI)")

    skew = snap.get("skew")
    # Strictly negative beyond noise: exactly-zero skew is usually paired
    # placeholder quotes, not a real inversion
    if skew is not None and skew <= -0.01:
        conditions.append(f"skew inverted ({skew * 100:+.1f} pts, calls over puts)")

    day_chg = None
    if spot and snap.get("prev_close"):
        day_chg = (spot - snap["prev_close"]) / snap["prev_close"]
    iv, base_iv = snap.get("atm_iv"), _baseline(history, "atm_iv")
    if (day_chg is not None and day_chg >= 0.02
            and iv and base_iv and iv > base_iv):
        conditions.append(f"price {day_chg:+.1%} with IV rising")

    if len(conditions) < th["squeeze_min_conditions"]:
        return []

    fuel = f"{pct:.0%} of float short"
    if si.get("days_to_cover"):
        fuel += f", {si['days_to_cover']:.1f} days to cover"
    gex = snap.get("net_gex")
    caveat = ""
    if gex and gex > 0:
        caveat = (" Naive GEX reads positive here, but in squeeze setups dealers "
                  "are likely short these calls — treat the dampening read as suspect.")
    return [{
        "kind": "squeeze_setup",
        "severity": "critical",
        "message": (f"Squeeze setup: {fuel}; " + "; ".join(conditions)
                    + f". Short-cover and dealer-hedging feedback loops both "
                      f"point the same way if this runs.{caveat}"),
        "value": float(len(conditions)),
        "details": json.dumps({"short_interest": si, "conditions": conditions}),
    }]


def detect_vol_compression(snap: dict, th: dict) -> list[dict]:
    """Coiled spring: realized vol collapsing and options not pricing expansion."""
    hv10, hv20, iv = snap.get("hv10"), snap.get("hv20"), snap.get("atm_iv")
    if not hv10 or not hv20 or hv20 <= 0:
        return []
    ratio = hv10 / hv20
    if ratio > th["vol_compression_ratio"]:
        return []
    if iv and iv / hv20 > th["vol_compression_max_ivhv"]:
        return []
    iv_note = f" and IV isn't pricing expansion ({iv / hv20:.2f}x 20d HV)" if iv else ""
    return [{
        "kind": "vol_compression",
        "severity": "info",
        "message": (f"Volatility compressing: 10d HV {hv10:.1%} is only "
                    f"{ratio:.2f}x the 20d {hv20:.1%}{iv_note} — coiled-spring "
                    f"setup; breaks from compression tend to be violent, in "
                    f"either direction"),
        "value": round(ratio, 3),
        "details": json.dumps({"hv10": hv10, "hv20": hv20, "atm_iv": iv}),
    }]


def run_all(snap: dict, contracts: list[dict], history: list[dict], th: dict,
            ctx: dict) -> list[dict]:
    signals = []
    signals += detect_iv_premium(snap, history, th)
    signals += detect_iv_spike(snap, history, th)
    signals += detect_unusual_volume(snap, contracts, th, ctx)
    signals += detect_pc_ratio(snap, history, th, ctx)
    signals += detect_gamma(snap, history, th, ctx)
    signals += detect_skew_shift(snap, history, th)
    signals += detect_squeeze_setup(snap, contracts, history, th, ctx)
    signals += detect_vol_compression(snap, th)
    return signals
