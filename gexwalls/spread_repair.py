"""Spread repair for GEX Walls: rank every way to fix a losing put credit spread.

Pure functions, no network. Give it the spread (a My trades entry), CBOE's chain in walls_app.load_chain's
layout and the stock price. Each repair is priced as one order at mid and at natural (worst fill), and the
repaired position is described at its expiration:

    hold             keep the spread as is (baseline)
    close            buy it back now (baseline)
    roll_out         same strikes, later expiration
    roll_down_out    lower strikes, same width, later expiration (debits allowed)
    roll_widen       later expiration and a wider spread, only when the roll pays a credit
    add_call_spread  sell a same-width call spread at or above the short put (iron condor / iron fly)

Odds use the same lognormal model as the rest of GEX Walls (each leg's implied vol, 4% rate, at expiration).
Repairs are ranked like the Strategy Scanner: highest POP (1-point buckets), then lowest capital (max loss),
then highest return (max profit / max loss).
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from statistics import NormalDist

import pandas as pd

RATE = 0.04
_N = NormalDist()

DEFAULTS = {
    "max_added_risk_pct": 60.0,  # a repair's max loss may exceed holding's by at most this much
    "min_extra_days": 5,         # new expirations: at least this many days past the current one...
    "max_extra_days": 75,        # ...and at most this many
    "min_oi": 20,                # open interest needed on every new leg
    "max_rel_spread": 0.35,      # widest (ask - bid) / mid allowed on every new leg
    "widen_mults": (1.5, 2.0),   # widened spreads: these multiples of today's width
    "min_call_credit": 0.05,     # smallest call-spread credit worth adding
    "exclude_earnings": False,   # drop repairs whose expiration falls after earnings
    "pop_bucket": 1.0,           # POP points treated as a tie before capital and return decide
    "min_ret": 0.15,             # Joe's floor: max profit at least 15% of max loss to be listed...
    "drop_ret": 0.10,            # ...and anything under 10% is dropped entirely (10-15% shown below the floor)
}

LABELS = {"roll_out": "Roll out", "roll_down_out": "Roll down & out", "roll_widen": "Roll & widen",
          "add_call_spread": "Add call spread"}


def prob_above(spot: float, level: float, iv: float, days: float, rate: float = RATE) -> float:
    """Lognormal odds (percent) that the stock finishes at or above `level` at expiration."""
    if level <= 0:
        return 100.0
    t = max(days, 0.5) / 365
    iv = iv if iv and iv > 0 else 0.5
    d2 = (math.log(spot / level) + (rate - iv * iv / 2) * t) / (iv * math.sqrt(t))
    return _N.cdf(d2) * 100


def project_earnings(results_dates: list, today: date) -> str | None:
    """Next earnings date projected from past results 8-Ks (Item 2.02 filing dates, any order).

    The next report is the same quarter a year earlier + 52 weeks (keeps the weekday): the earliest 2.02 filed
    between 371 and 264 days ago, i.e. one whose anniversary falls from a week ago to ~100 days ahead. Anchoring on
    last year ignores off-cycle 2.02s (pre-announcements, preliminary numbers in a deal 8-K). Without that history:
    the latest report + 91 days. None when the projection is more than a week in the past.
    """
    ds = sorted({date.fromisoformat(str(x)[:10]) for x in results_dates or []})
    if not ds:
        return None
    year_ago = [d for d in ds if today - timedelta(days=371) <= d <= today - timedelta(days=264)]
    proj = year_ago[0] + timedelta(days=364) if year_ago else ds[-1] + timedelta(days=91)
    return proj.isoformat() if proj >= today - timedelta(days=7) else None


def rank(cands: list, bucket: float = 1.0) -> list:
    """Highest POP first (POPs within the same `bucket` points tie), then lowest max loss, then best return."""
    return sorted(cands, key=lambda c: (-math.floor(c["pop"] / bucket), c["max_loss"], -c["ret"]))


def _key(k: float) -> float:
    return round(float(k), 3)


def _book(df: pd.DataFrame) -> dict:
    """{(exp 'YYYY-MM-DD', 'P'|'C'): {strike: quote}} with quote fields bid, ask, iv, open_interest."""
    d = df[["expiration", "type", "strike", "bid", "ask", "iv", "open_interest"]].copy()
    d["exp"] = pd.to_datetime(d["expiration"]).dt.strftime("%Y-%m-%d")
    for c in ("bid", "ask", "iv", "open_interest"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0.0)
    book: dict = {}
    for r in d.itertuples(index=False):
        book.setdefault((r.exp, r.type), {}).setdefault(_key(r.strike), r)
    return book


def _mid(q) -> float:
    return (q.bid + q.ask) / 2


def _iv(q, fallback: float) -> float:
    return float(q.iv) if q is not None and q.iv > 0 else fallback


def _liquid(q, p: dict) -> bool:
    if q is None or q.bid <= 0 or q.ask <= 0 or q.open_interest < p["min_oi"]:
        return False
    m = _mid(q)
    return m > 0 and (q.ask - q.bid) / m <= p["max_rel_spread"]


def _days(exp: str, today: date) -> int:
    return (date.fromisoformat(exp) - today).days


def _leg(side: str, exp: str, typ: str, strike: float) -> dict:
    return {"side": side, "exp": exp, "type": typ, "strike": float(strike)}


def _earn_flag(exp: str, earnings: dict | None, today: date) -> str | None:
    """'confirmed' / 'projected' when earnings land on or before this expiration (projected dates: a week early)."""
    if not earnings or not earnings.get("date"):
        return None
    ed = date.fromisoformat(str(earnings["date"])[:10])
    if ed < today:
        return None
    confirmed = bool(earnings.get("confirmed"))
    lead = timedelta(days=0 if confirmed else 7)
    if date.fromisoformat(exp) >= ed - lead:
        return "confirmed" if confirmed else "projected"
    return None


def _below_wall(walls: dict | None, exp: str, short: float) -> bool | None:
    w = (walls or {}).get(exp)
    return None if w is None else bool(short <= w)


def repair_plan(spread: dict, df: pd.DataFrame, spot: float, today: date,
                earnings: dict | None = None, walls: dict | None = None, **params) -> dict:
    """Baselines (hold, close) plus every repair that passes the filters, ranked best first."""
    p = {**DEFAULTS, **params}
    book = _book(df)
    exp0 = str(spread["exp"])[:10]
    K, L = float(spread["short"]), float(spread["long"])
    C, n = float(spread["credit"]), int(spread.get("qty") or 1)
    W = K - L
    puts0 = book.get((exp0, "P"), {})
    s0, l0 = puts0.get(_key(K)), puts0.get(_key(L))
    if s0 is None or l0 is None:
        raise ValueError(f"{spread.get('ticker', '')} {K:g}/{L:g}P {exp0} is not in CBOE's chain "
                         "(expired or mistyped?)")
    mid0 = max(0.0, _mid(s0) - _mid(l0))
    nat0 = max(0.0, s0.ask - l0.bid)
    dte0 = _days(exp0, today)
    iv_s0 = _iv(s0, 0.5)
    iv_l0 = _iv(l0, iv_s0)
    hold = {"exp": exp0, "dte": dte0, "short": K, "long": L, "width": W, "credit": C, "qty": n,
            "mid": mid0, "nat": nat0, "be": K - C, "max_loss": (W - C) * 100 * n, "max_profit": C * 100 * n,
            "margin": W * 100 * n, "pl_now": (C - mid0) * 100 * n,
            "pop": prob_above(spot, K - C, iv_s0, dte0), "p_max_loss": 100 - prob_above(spot, L, iv_l0, dte0),
            "earn": _earn_flag(exp0, earnings, today), "below_wall": _below_wall(walls, exp0, K)}
    close = {"mid": mid0, "nat": nat0, "pl_mid": (C - mid0) * 100 * n, "pl_nat": (C - nat0) * 100 * n,
             "legs": [_leg("BTC", exp0, "P", K), _leg("STC", exp0, "P", L)]}
    cap = hold["max_loss"] * (1 + p["max_added_risk_pct"] / 100) + 1e-9
    cands = []

    def add(kind, exp, d, short, long_, width, net_mid, net_nat, pop, p_max, legs, **extra):
        tot = C + net_mid
        ml = (width - tot) * 100 * n
        cands.append({"kind": kind, "label": LABELS[kind], "exp": exp, "dte": d, "short": short, "long": long_,
                      "width": width, "net_mid": net_mid, "net_nat": net_nat, "total_credit": tot,
                      "be": short - tot, "max_loss": ml, "max_loss_change": ml - hold["max_loss"],
                      "margin": width * 100 * n, "max_profit": tot * 100 * n, "ret": tot / (width - tot),
                      "pop": pop, "pop_change": pop - hold["pop"], "p_max_loss": p_max,
                      "earn": _earn_flag(exp, earnings, today), "below_wall": _below_wall(walls, exp, short),
                      "legs": legs, **extra})

    # rolls to later expirations: same width (out / down and out), or wider for a credit
    close_legs = close["legs"]
    for (e, typ), puts in sorted(book.items()):
        if typ != "P" or e <= exp0:
            continue
        d = _days(e, today)
        if not dte0 + p["min_extra_days"] <= d <= dte0 + p["max_extra_days"]:
            continue
        for k, sq in puts.items():
            if k > K or not _liquid(sq, p):
                continue
            plans = [(W, "roll_out" if k == K else "roll_down_out")] + [(W * m, "roll_widen") for m in p["widen_mults"]]
            for w, kind in plans:
                lq = puts.get(_key(k - w))
                if lq is None or not _liquid(lq, p):
                    continue
                net_mid = (_mid(sq) - _mid(lq)) - mid0
                net_nat = (sq.bid - lq.ask) - nat0
                tot = C + net_mid
                if tot <= 0 or (kind == "roll_widen" and net_mid < 0) or (w - tot) * 100 * n > cap:
                    continue
                iv_s = _iv(sq, iv_s0)
                add(kind, e, d, k, k - w, w, net_mid, net_nat,
                    prob_above(spot, k - tot, iv_s, d), 100 - prob_above(spot, k - w, _iv(lq, iv_s), d),
                    close_legs + [_leg("STO", e, "P", k), _leg("BTO", e, "P", k - w)])

    # call credit spread of the same width at or above the short put, same expiration (condor / fly)
    calls = book.get((exp0, "C"), {})
    for kc, cq in calls.items():
        if kc < K or not _liquid(cq, p):
            continue
        lc = calls.get(_key(kc + W))
        if lc is None or not _liquid(lc, p):
            continue
        cm = _mid(cq) - _mid(lc)
        if cm < p["min_call_credit"]:
            continue
        tot = C + cm
        iv_c = _iv(cq, iv_s0)
        pop = max(0.0, prob_above(spot, K - tot, iv_s0, dte0) - prob_above(spot, kc + tot, iv_c, dte0))
        p_max = (100 - prob_above(spot, L, iv_l0, dte0)) + prob_above(spot, kc + W, _iv(lc, iv_c), dte0)
        add("add_call_spread", exp0, dte0, K, L, W, cm, cq.bid - lc.ask, pop, p_max,
            [_leg("STO", exp0, "C", kc), _leg("BTO", exp0, "C", kc + W)],
            call_short=kc, call_long=kc + W, be_high=kc + tot, shape="iron fly" if kc == K else "iron condor")

    if p["exclude_earnings"]:
        cands = [c for c in cands if not c["earn"]]
    ok = [c for c in cands if c["ret"] >= p["min_ret"]]
    thin = [c for c in cands if p["drop_ret"] <= c["ret"] < p["min_ret"]]
    return {"hold": hold, "close": close, "repairs": rank(ok, p["pop_bucket"]),
            "below_floor": rank(thin, p["pop_bucket"]),
            "dropped_under_min": sum(c["ret"] < min(p["drop_ret"], p["min_ret"]) for c in cands),
            "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()}}
