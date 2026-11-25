"""Candidate generators for each strategy and timeframe.

Every candidate carries: credit/debit, capital (max loss or cash secured, per 1 lot),
return on capital, POP under implied vol, POP under realized vol, and the conservative POP
(the lower of the two), which is what ranking uses until the learning loop has enough data."""
import math

from .ev import ev_fields
from .options import buy_fill, leg_iv, liquid, sell_fill, sigma_distance
from .util import calendar_dte, prob_above, prob_between, years_to_expiry

STRATEGY_NAMES = {
    "PCS": "Put credit spread",
    "CCS": "Call credit spread",
    "IC": "Iron condor",
    "CSP": "Cash-secured put",
    "CC": "Buy-write (covered call)",
    "LCALL": "Deep-ITM LEAPS call",
}

SHORT_PREMIUM = {"PCS", "CCS", "IC", "CSP", "CC"}


def timeframe_for(dte, tf_cfg):
    for name, b in tf_cfg.items():
        if b["min_dte"] <= dte <= b["max_dte"]:
            return name
    return None


def _leg(o, side, iv):
    return {"cp": o["cp"], "K": o["K"], "side": side, "bid": o["bid"], "ask": o["ask"],
            "iv": round(iv, 4), "delta": o["delta"], "oi": o["oi"], "code": o["code"]}


def _pops(S, be, iv, rv, T, r, above=True):
    p_iv = prob_above(S, be, iv, T, r)
    p_rv = prob_above(S, be, rv, T, r)
    if not above:
        p_iv, p_rv = 1.0 - p_iv, 1.0 - p_rv
    return p_iv, p_rv, min(p_iv, p_rv)


def generate(tc, cfg):
    """tc: ticker context with keys sym, S, chain, iv30, rv, r, now, today, earnings_date."""
    S, r, now = tc["S"], tc["r"], tc["now"]
    lc = cfg["liquidity"]
    slip = cfg["slippage"]
    tf_cfg = cfg["timeframes"]
    sc = cfg["strategy"]
    out = []
    for exp, sides in tc["chain"].items():
        dte = calendar_dte(exp, tc["today"])
        tf = timeframe_for(dte, tf_cfg)
        if tf is None:
            continue
        T = years_to_expiry(exp, now)
        fb_iv = tc["iv30"] if tc["iv30"] and tc["iv30"] > 0.02 else 0.4
        rv = tc["rv"] if tc["rv"] and tc["rv"] > 0.02 else fb_iv
        earn = tc.get("earnings_date")
        earn_in = int(bool(earn and tc["today"] <= earn <= exp))
        base = {"ticker": tc["sym"], "expiry": exp, "dte": dte, "timeframe": tf, "T": T,
                "S": S, "rv": rv, "earn_in_window": earn_in}
        puts, calls = sides["P"], sides["C"]
        maxw = min(cfg["max_width_abs"], max(cfg["max_width_pct"] * S, 0.5))

        pcs = _vertical(puts, S, T, r, rv, fb_iv, lc, slip, sc, maxw, base, "PCS")
        ccs = _vertical(calls, S, T, r, rv, fb_iv, lc, slip, sc, maxw, base, "CCS")
        out.extend(pcs)
        out.extend(ccs)
        out.extend(_condors(pcs, ccs, S, T, r, rv, base))
        if S <= cfg["wheel_max_price"] * 1.5:
            out.extend(_csp(puts, S, T, r, rv, fb_iv, lc, slip, sc, cfg, base))
        if S <= cfg["wheel_max_price"]:
            out.extend(_buy_write(calls, S, T, r, rv, fb_iv, lc, slip, sc, base))
        if tf == "leaps":
            out.extend(_leaps_calls(calls, S, T, r, rv, fb_iv, lc, slip, sc, base))
    ev_cfg = cfg.get("ev") or {}
    if ev_cfg.get("enabled", True):                 # expected value in dollars per contract (see ev.py)
        for c in out:
            try:
                c.update(ev_fields(c, c["S"], c["T"], r, c.get("rv") or 0.0, ev_cfg))
            except (ValueError, ZeroDivisionError, OverflowError):
                pass
    return out


def _vertical(opts, S, T, r, rv, fb_iv, lc, slip, sc, maxw, base, kind):
    """Put credit spreads (kind=PCS) or call credit spreads (kind=CCS)."""
    res = []
    dmin, dmax = sc["short_delta_min"], sc["short_delta_max"]
    if kind == "PCS":
        shorts = [o for o in opts if o["K"] < S and dmin <= abs(o["delta"]) <= dmax and liquid(o, lc, True)]
    else:
        shorts = [o for o in opts if o["K"] > S and dmin <= abs(o["delta"]) <= dmax and liquid(o, lc, True)]
    for so in shorts:
        if kind == "PCS":
            longs = [o for o in opts if so["K"] - maxw <= o["K"] < so["K"]]
            longs = sorted(longs, key=lambda o: -o["K"])[: sc["max_long_legs"]]
        else:
            longs = [o for o in opts if so["K"] < o["K"] <= so["K"] + maxw]
            longs = sorted(longs, key=lambda o: o["K"])[: sc["max_long_legs"]]
        iv_s = leg_iv(so, fb_iv)
        for lo in longs:
            if not liquid(lo, lc, False):
                continue
            credit = sell_fill(so, slip) - buy_fill(lo, slip)
            width = abs(so["K"] - lo["K"])
            if credit < sc["min_credit"] or credit >= width:
                continue
            maxloss = width - credit
            if kind == "PCS":
                be = so["K"] - credit
                p_iv, p_rv, p_c = _pops(S, be, iv_s, rv, T, r, above=True)
                pmax = min(prob_above(S, so["K"], iv_s, T, r), prob_above(S, so["K"], rv, T, r))
                be_lo, be_hi = be, None
            else:
                be = so["K"] + credit
                p_iv, p_rv, p_c = _pops(S, be, iv_s, rv, T, r, above=False)
                pmax = min(1 - prob_above(S, so["K"], iv_s, T, r), 1 - prob_above(S, so["K"], rv, T, r))
                be_lo, be_hi = None, be
            c = dict(base)
            c.update({
                "strategy": kind, "legs": [_leg(so, "sell", iv_s), _leg(lo, "buy", leg_iv(lo, fb_iv))],
                "credit": credit, "width": width, "capital": maxloss * 100.0, "roc": credit / maxloss,
                "pop_iv": p_iv, "pop_rv": p_rv, "pop_cons": p_c, "p_max": pmax,
                "be_lo": be_lo, "be_hi": be_hi, "short_delta": abs(so["delta"]),
                "sigma_dist": sigma_distance(S, so["K"], iv_s, T), "iv_short": iv_s,
            })
            res.append(c)
    return res


def _condors(pcs, ccs, S, T, r, rv, base):
    res = []
    if not pcs or not ccs:
        return res
    p_top = sorted([c for c in pcs if c["short_delta"] <= 0.30], key=lambda c: -c["pop_cons"])[:6]
    c_top = sorted([c for c in ccs if c["short_delta"] <= 0.30], key=lambda c: -c["pop_cons"])[:6]
    for p in p_top:
        for c in c_top:
            kps, kcs = p["legs"][0]["K"], c["legs"][0]["K"]
            if kps >= kcs:
                continue
            credit = p["credit"] + c["credit"]
            w = max(p["width"], c["width"])
            maxloss = w - credit
            if maxloss <= 0:
                continue
            be_lo, be_hi = kps - credit, kcs + credit
            iv_p, iv_c = p["iv_short"], c["iv_short"]
            p_iv = prob_between(S, be_lo, be_hi, iv_p, iv_c, T, r)
            p_rv = prob_between(S, be_lo, be_hi, rv, rv, T, r)
            pmax = min(prob_between(S, kps, kcs, iv_p, iv_c, T, r), prob_between(S, kps, kcs, rv, rv, T, r))
            ic = dict(base)
            ic.update({
                "strategy": "IC", "legs": p["legs"] + c["legs"], "credit": credit, "width": w,
                "capital": maxloss * 100.0, "roc": credit / maxloss,
                "pop_iv": p_iv, "pop_rv": p_rv, "pop_cons": min(p_iv, p_rv), "p_max": pmax,
                "be_lo": be_lo, "be_hi": be_hi, "short_delta": max(p["short_delta"], c["short_delta"]),
                "sigma_dist": min(abs(p["sigma_dist"] or 0), abs(c["sigma_dist"] or 0)),
                "iv_short": 0.5 * (iv_p + iv_c),
            })
            res.append(ic)
    return res


def _csp(puts, S, T, r, rv, fb_iv, lc, slip, sc, cfg, base):
    res = []
    for o in puts:
        if not (o["K"] < S and o["K"] <= cfg["wheel_max_price"]):
            continue
        if not (sc["short_delta_min"] <= abs(o["delta"]) <= sc["short_delta_max"]) or not liquid(o, lc, True):
            continue
        credit = sell_fill(o, slip)
        if credit < sc["min_credit"]:
            continue
        iv = leg_iv(o, fb_iv)
        be = o["K"] - credit
        p_iv, p_rv, p_c = _pops(S, be, iv, rv, T, r, above=True)
        c = dict(base)
        c.update({
            "strategy": "CSP", "legs": [_leg(o, "sell", iv)], "credit": credit, "width": None,
            "capital": be * 100.0, "roc": credit / be,
            "pop_iv": p_iv, "pop_rv": p_rv, "pop_cons": p_c,
            "p_max": min(prob_above(S, o["K"], iv, T, r), prob_above(S, o["K"], rv, T, r)),
            "be_lo": be, "be_hi": None, "short_delta": abs(o["delta"]),
            "sigma_dist": sigma_distance(S, o["K"], iv, T), "iv_short": iv,
        })
        res.append(c)
    return res


def _buy_write(calls, S, T, r, rv, fb_iv, lc, slip, sc, base):
    """Buy 100 shares + sell 1 OTM call. Return figure = premium / net cost if price is unchanged."""
    res = []
    for o in calls:
        if o["K"] < S or not (sc["short_delta_min"] <= abs(o["delta"]) <= sc["short_delta_max"]):
            continue
        if not liquid(o, lc, True):
            continue
        prem = sell_fill(o, slip)
        if prem < sc["min_credit"] or prem >= S:
            continue
        iv = leg_iv(o, fb_iv)
        net = S - prem
        p_iv, p_rv, p_c = _pops(S, net, iv, rv, T, r, above=True)
        c = dict(base)
        c.update({
            "strategy": "CC", "legs": [{"cp": "STK", "K": None, "side": "buy", "bid": None, "ask": None,
                                        "iv": None, "delta": 1.0, "oi": None, "code": base["ticker"]},
                                       _leg(o, "sell", iv)],
            "credit": prem, "width": None, "capital": net * 100.0, "roc": prem / net,
            "ret_called": (o["K"] - S + prem) / net,
            "pop_iv": p_iv, "pop_rv": p_rv, "pop_cons": p_c,
            "p_max": min(prob_above(S, o["K"], iv, T, r), prob_above(S, o["K"], rv, T, r)),
            "be_lo": net, "be_hi": None, "short_delta": abs(o["delta"]),
            "sigma_dist": sigma_distance(S, o["K"], iv, T), "iv_short": iv,
        })
        res.append(c)
    return res


def _leaps_calls(calls, S, T, r, rv, fb_iv, lc, slip, sc, base):
    """Deep-ITM long calls as a stock substitute. Return figure = payoff if the stock ends
    one implied standard deviation higher at expiry (a scenario, not a forecast)."""
    res = []
    for o in calls:
        if o["K"] >= S or o["delta"] < sc["leaps_min_delta"] or not liquid(o, lc, False):
            continue
        debit = buy_fill(o, slip)
        intrinsic = S - o["K"]
        if debit <= 0 or debit < intrinsic * 0.98:
            continue
        extrinsic = debit - intrinsic
        if extrinsic / debit > sc["leaps_max_extrinsic_pct"]:
            continue
        iv = leg_iv(o, fb_iv)
        be = o["K"] + debit
        p_iv, p_rv, p_c = _pops(S, be, iv, rv, T, r, above=True)
        s_up = S * math.exp(iv * math.sqrt(T))
        c = dict(base)
        c.update({
            "strategy": "LCALL", "legs": [_leg(o, "buy", iv)], "credit": -debit, "width": None,
            "capital": debit * 100.0, "roc": (max(s_up - o["K"], 0.0) - debit) / debit,
            "pop_iv": p_iv, "pop_rv": p_rv, "pop_cons": p_c, "p_max": None,
            "be_lo": be, "be_hi": None, "short_delta": o["delta"],
            "sigma_dist": sigma_distance(S, o["K"], iv, T), "iv_short": iv,
            "extrinsic_pct": extrinsic / debit, "leverage": o["delta"] * S / debit,
        })
        res.append(c)
    return res
