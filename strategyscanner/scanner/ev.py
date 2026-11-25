"""Expected value of an options position, under the same lognormal the POP figures use.

Why this exists (2026-11-25 audit): the scanner ranked and filtered on probability of profit
(POP) only. A trade can have a 78% chance of profit and still lose money on average, because the
22% loses several times what the 78% wins. Measured over 3,478 settled picks the average result was
-10.1% of capital per trade, and 56% of all logged picks had negative expected value at the
scanner's own probabilities before any commission. These functions compute that expected value so
it can be filtered and ranked on.

The maths: E[max(K - S_T, 0)] and E[max(S_T - K, 0)] in closed form for a lognormal S_T with
forward F = S*exp(rT) and volatility sigma -- the undiscounted Black-76 intrinsics. The expected
value of a position is then the net credit received minus the net intrinsic it is expected to owe
at expiry, which is exact for anything built from European legs held to expiry.

Drift: zero (forward = spot). The POP figures use drift r, but EV must not manufacture edge out of
an assumed market rise -- with drift r a two-year LEAPS call scores +8% of spot before it does
anything, and in the audit every single long-LEAPS pick passed the gate for that reason alone.

Volatility used for the gate, chosen by measurement rather than taste: over 499 independent settled
moves, the realized move was 1.71x what the scanner's rv20 forecast implied; iv30 alone 1.56x;
max(rv20, iv30) 1.49x; and max(rv20, iv30) x 1.25 came closest at 1.19x. Separately, across 110,318
forward/trailing vol pairs over two years of cached history, forward 20-day vol was 1.00x trailing
at the median but 1.12x on average and 1.36x in RMS -- the distribution is skewed, and expected
value is an average, so a median-unbiased forecast understates what short premium expects to pay.
Hence the gate prices the expected cost above the realized-vol forecast. It deliberately does NOT use
implied vol there: expected value at implied vol is zero by no-arbitrage, so stressing implied would
reject every short-premium trade ever written instead of discriminating between them. Stressing the
realized-vol forecast is a correction to that forecast's measured bias, and it leaves the real
question intact -- is implied enough above realized to pay for the movement that actually turns up.

The stress is applied as the WORST of several volatility scenarios, not a single higher number,
because expected cost is not monotone in volatility. A short call spread's expected payout rises
with volatility, peaks, then falls back toward zero: at very high volatility a zero-drift lognormal
has its median far below spot, so an out-of-the-money call is unlikely to be reached. Stressing
volatility in one direction therefore made call spreads look better the more the stock was actually
moving -- PANW scored +$43 a contract with realized volatility above implied. Taking the minimum
expected value over {rv/stress, rv, rv*stress} finds the worst case in that neighbourhood and
removes the perverse incentive on both sides of the market.

Three measures are reported:
  ev_iv  -- each leg's implied volatility, no stress. ~0 minus costs if the position is fairly
            priced, so it is a fill-and-commission sanity check, not an edge estimate.
  ev_rv  -- the underlying's realized volatility, no stress. Positive when implied exceeds realized.
  ev     -- the gate: max(realized, implied) x rv_stress. A trade passes only if it still pays after
            the stock moves more than either forecast says, which is what actually kept happening.
"""
import math

from .util import ncdf

__all__ = ["exp_put", "exp_call", "position_ev", "ev_fields"]


def _d12(F, K, sigma, T):
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * v * v) / v
    return d1, d1 - v


def exp_put(F, K, sigma, T):
    """E[max(K - S_T, 0)], undiscounted, lognormal with forward F and vol sigma."""
    if K <= 0:
        return 0.0
    if F <= 0:
        return K
    if sigma <= 0 or T <= 0:
        return max(K - F, 0.0)
    d1, d2 = _d12(F, K, sigma, T)
    return K * ncdf(-d2) - F * ncdf(-d1)


def exp_call(F, K, sigma, T):
    """E[max(S_T - K, 0)], undiscounted."""
    if K <= 0:
        return F
    if F <= 0:
        return 0.0
    if sigma <= 0 or T <= 0:
        return max(F - K, 0.0)
    d1, d2 = _d12(F, K, sigma, T)
    return F * ncdf(d1) - K * ncdf(d2)


def position_ev(legs, credit, S, T, vol_of_leg, stock_shares=0):
    """Expected profit per share at expiry, zero drift (forward = spot).

    legs: [{"cp": "C"|"P"|"STK", "K": float|None, "side": "sell"|"buy"}]
    credit: net premium received per share (negative for a net debit).
    vol_of_leg(leg) -> the volatility to use for that leg.
    stock_shares: +1 for a long 100-share lot (buy-write), 0 otherwise.

    A short leg owes its intrinsic at expiry and a long leg receives it. Exact for European legs
    held to expiry. With zero drift long stock contributes nothing in expectation.
    """
    F = float(S)
    ev = float(credit)
    for l in legs:
        cp = l.get("cp")
        if cp not in ("C", "P"):
            continue
        K = l.get("K")
        if K is None:
            continue
        sig = vol_of_leg(l)
        if sig is None or sig <= 0:
            return None
        intr = exp_call(F, K, sig, T) if cp == "C" else exp_put(F, K, sig, T)
        ev += intr if l.get("side") == "buy" else -intr
    return ev


def ev_fields(c, S, T, r, rv, cfg_ev):
    """{'ev_iv','ev_rv','ev'} in dollars per contract, after commissions. `r` is accepted and
    ignored, so callers do not have to change when the drift choice does."""
    legs = c.get("legs") or []
    opt_legs = [l for l in legs if l.get("cp") in ("C", "P")]
    if not opt_legs or T <= 0 or S <= 0:
        return {}
    shares = 1 if c.get("strategy") in ("CC", "BW") else 0
    comm = float(cfg_ev.get("commission_per_contract", 0.65))
    legs_cost = comm * len(opt_legs) * (2 if cfg_ev.get("charge_close", True) else 1)
    stress = float(cfg_ev.get("rv_stress", 1.0) or 1.0)
    stress = stress if stress > 0 else 1.0
    fb = c.get("iv_short") or rv

    def iv_leg(l):
        v = l.get("iv")
        return v if v and 0.02 <= v <= 5.0 else fb

    out = {}
    ev_iv = position_ev(opt_legs, c["credit"], S, T, iv_leg, shares)
    ev_rv = position_ev(opt_legs, c["credit"], S, T, lambda l: rv, shares)
    # the gate: the worst of the volatility scenarios around the realized-vol forecast
    ev_st = None
    if rv and rv > 0:
        for mult in (1.0 / stress, 1.0, stress):
            v = position_ev(opt_legs, c["credit"], S, T, lambda l, m=mult: rv * m, shares)
            if v is not None and (ev_st is None or v < ev_st):
                ev_st = v
    for k, v in (("ev_iv", ev_iv), ("ev_rv", ev_rv), ("ev", ev_st)):
        out[k] = None if v is None else round(v * 100.0 - legs_cost, 2)
    return out
