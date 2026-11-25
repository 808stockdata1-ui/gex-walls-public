import datetime as dt
import logging
import math
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
SQRT2 = math.sqrt(2.0)

log = logging.getLogger("scanner")


def ncdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / SQRT2))


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def today_et() -> dt.date:
    return now_utc().astimezone(ET).date()


def expiry_close_utc(exp: dt.date) -> dt.datetime:
    """Options stop trading 16:00 New York time on expiration day."""
    return dt.datetime(exp.year, exp.month, exp.day, 16, 0, tzinfo=ET).astimezone(UTC)


def years_to_expiry(exp: dt.date, now: dt.datetime | None = None, floor_hours: float = 2.0) -> float:
    now = now or now_utc()
    secs = (expiry_close_utc(exp) - now).total_seconds()
    secs = max(secs, floor_hours * 3600.0)
    return secs / (365.0 * 86400.0)


def calendar_dte(exp: dt.date, ref: dt.date | None = None) -> int:
    ref = ref or today_et()
    return (exp - ref).days


def prob_above(S: float, B: float, sigma: float, T: float, r: float) -> float:
    """P(S_T > B) under a lognormal model with drift r and volatility sigma."""
    if B <= 0:
        return 1.0
    if S <= 0:
        return 0.0
    if sigma <= 0 or T <= 0:
        return 1.0 if S > B else 0.0
    d2 = (math.log(S / B) + (r - 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return ncdf(d2)


def prob_between(S: float, lo: float, hi: float, sig_lo: float, sig_hi: float, T: float, r: float) -> float:
    """P(lo < S_T < hi); each boundary uses its own vol (skew-aware approximation)."""
    p = prob_above(S, lo, sig_lo, T, r) - prob_above(S, hi, sig_hi, T, r)
    return max(0.0, min(1.0, p))


def bs_gamma(S: float, K: float, sigma: float, T: float, r: float = 0.0) -> float:
    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return npdf(d1) / (S * sigma * math.sqrt(T))


def safe_float(x, default=None):
    try:
        if x is None:
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except (TypeError, ValueError):
        return default


def logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


def sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)
