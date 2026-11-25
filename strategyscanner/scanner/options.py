"""Chain helpers: liquidity checks, fill estimates, per-strike IV, dealer gamma exposure (GEX)."""
import math
from collections import defaultdict

import numpy as np

from .util import bs_gamma, years_to_expiry


def group_chain(options):
    """{expiry: {"C": [opts sorted by K], "P": [...]}}"""
    g = defaultdict(lambda: {"C": [], "P": []})
    for o in options:
        if o["cp"] in ("C", "P"):
            g[o["exp"]][o["cp"]].append(o)
    for exp in g:
        g[exp]["C"].sort(key=lambda o: o["K"])
        g[exp]["P"].sort(key=lambda o: o["K"])
    return dict(g)


def mid(o):
    return 0.5 * (o["bid"] + o["ask"])


def sell_fill(o, slip):
    """Estimated price received when selling: between mid and bid."""
    return mid(o) - slip * 0.5 * (o["ask"] - o["bid"])


def buy_fill(o, slip):
    """Estimated price paid when buying: between mid and ask."""
    return mid(o) + slip * 0.5 * (o["ask"] - o["bid"])


def liquid(o, cfg, short: bool):
    """Mandatory liquidity filter for one leg."""
    bid, ask = o["bid"], o["ask"]
    if ask <= 0 or bid < 0 or ask < bid:
        return False
    if short and bid <= 0:
        return False
    oi_min = cfg["min_oi_short"] if short else cfg["min_oi_long"]
    if o["oi"] < oi_min:
        return False
    m = mid(o)
    if m <= 0:
        return False
    spread = ask - bid
    return spread <= cfg["max_abs_spread"] or spread / m <= cfg["max_rel_spread"]


def leg_iv(o, fallback):
    iv = o.get("iv") or 0.0
    if 0.02 <= iv <= 5.0:
        return iv
    return fallback


def gex_profile(chain, spot, r, now, max_dte_days=90):
    """Dealer gamma exposure using the common convention (dealers long calls, short puts).
    Returns net GEX ($ per 1% move), call wall, put wall, and zero-gamma (flip) level."""
    rows = []  # (K, sign, oi, iv, T)
    per_strike_call = defaultdict(float)
    per_strike_put = defaultdict(float)
    net = 0.0
    for exp, sides in chain.items():
        T = years_to_expiry(exp, now)
        if T * 365.0 > max_dte_days:
            continue
        for cp, sign in (("C", 1.0), ("P", -1.0)):
            for o in sides[cp]:
                if o["oi"] <= 0:
                    continue
                g = o["gamma"] if o["gamma"] > 0 else 0.0
                val = sign * g * o["oi"] * 100.0 * spot * spot * 0.01
                net += val
                if cp == "C":
                    per_strike_call[o["K"]] += val
                else:
                    per_strike_put[o["K"]] += val
                iv = o["iv"] if 0.02 <= o["iv"] <= 5.0 else None
                if iv:
                    rows.append((o["K"], sign, o["oi"], iv, T))
    call_wall = max(per_strike_call.items(), key=lambda kv: kv[1])[0] if per_strike_call else None
    put_wall = min(per_strike_put.items(), key=lambda kv: kv[1])[0] if per_strike_put else None

    flip = None
    if rows:
        arr = np.array(rows, dtype=float)
        K, sign, oi, iv, T = (arr[:, i][None, :] for i in range(5))
        g = spot * (0.75 + 0.0125 * np.arange(41))  # 75% .. 125% of spot
        s = g[:, None]
        sq = iv * np.sqrt(T)
        d1 = (np.log(s / K) + (r + 0.5 * iv * iv) * T) / sq
        gam = np.exp(-0.5 * d1 * d1) / (np.sqrt(2.0 * np.pi) * s * sq)
        vals = ((sign * gam * oi).sum(axis=1) * 100.0 * g * g * 0.01).tolist()
        grid = g.tolist()
        best = None
        for i in range(1, len(grid)):
            if (vals[i - 1] <= 0 < vals[i]) or (vals[i - 1] >= 0 > vals[i]):
                a, b = vals[i - 1], vals[i]
                x = grid[i - 1] + (grid[i] - grid[i - 1]) * (-a / (b - a)) if b != a else grid[i]
                if best is None or abs(x - spot) < abs(best - spot):
                    best = x
        flip = best
    return {"net_gex": net, "call_wall": call_wall, "put_wall": put_wall, "gamma_flip": flip}


def strike_step(options):
    ks = sorted({o["K"] for o in options})
    diffs = [b - a for a, b in zip(ks, ks[1:]) if b - a > 0]
    return min(diffs) if diffs else None


def sigma_distance(S, K, sigma, T):
    if sigma <= 0 or T <= 0 or S <= 0 or K <= 0:
        return None
    return math.log(K / S) / (sigma * math.sqrt(T))
