"""Mandatory filters (never changed by learning) and ranking.

2026-11-25: ranking used to be probability of profit first, then lowest capital, then return. An
audit of 3,478 settled picks found that selects high-win-rate trades that lose money on average
(-10.1% of capital per trade; 56% of logged picks had negative expected value at the scanner's own
probabilities before commission). Probability of profit says how OFTEN a trade wins, never how much
it wins against how much it loses. So there is now a hard expected-value gate (ev.py) and ranking is
by expected return per dollar at risk, with POP still a floor. Set ev.enabled false in config.json to
go back to the old behaviour."""
import math

from .strategies import SHORT_PREMIUM


def profit_pct(c):
    """Max profit as a fraction of capital: credit / max loss for spreads and condors, credit / cash
    secured for puts, gain if called away for buy-writes, the one-standard-deviation-up payoff for
    LEAPS calls."""
    if c["strategy"] == "CC":
        return c.get("ret_called") if c.get("ret_called") is not None else c["roc"]
    return c["roc"]


def ev_on_risk(c):
    """Expected profit per dollar of capital at risk (the ranking objective)."""
    ev, cap = c.get("ev"), c.get("capital")
    if ev is None or not cap:
        return None
    return ev / cap


def mandatory_ok(c, cfg, profit_floor=None):
    st, tf = c["strategy"], c["timeframe"]
    if c["capital"] <= 0 or c["capital"] > cfg["max_capital"].get(st, 1e12):
        return False, "capital"
    floor = cfg["min_roc"].get(st, {}).get(tf)
    if floor is not None and c["roc"] < floor:
        return False, "min_roc"
    pf = cfg.get("min_profit") if profit_floor is None else profit_floor
    if pf is not None and (profit_pct(c) or 0.0) < pf:
        return False, "min_profit"
    mp = cfg.get("min_premium_roc")  # credit / premium itself vs capital (a covered call can't qualify on stock gain alone)
    if mp is not None and (c.get("roc") or 0.0) < mp:
        return False, "min_premium_roc"
    ann = cfg.get("min_roc_annual")  # same profit, annualized (0-DTE counts as one day)
    if ann is not None and (profit_pct(c) or 0.0) * 365.0 / max(c.get("dte") or 0, 1) < ann:
        return False, "min_roc_annual"
    if st in SHORT_PREMIUM and c["earn_in_window"] and tf in cfg["exclude_earnings_timeframes"]:
        return False, "earnings"
    # 2026-11-18: pop_cal is only set when a calibration beat raw POP walk-forward, so it is the most accurate POP
    p = c["pop_cons"] if c.get("pop_cal") is None else c["pop_cal"]
    if p < cfg["min_pop"].get(tf, 0.0):
        return False, "min_pop"
    if (cfg.get("ev") or {}).get("gate"):               # off by default: see ev_pass
        ok, why = ev_pass(c, cfg)
        if not ok:
            return False, why
    return True, ""


def ev_pass(c, cfg):
    """(ok, reason): does this trade clear the expected-value tests?

    Kept separate from mandatory_ok so the two questions stay separate. Candidates that fail are
    still shown -- in a market with no volatility premium almost nothing passes, and an empty list
    tells you less than a list whose best entry is marked "expected value -$12". Only the picks that
    pass are logged to the track record, so the record measures a disciplined strategy rather than
    every idea printed.
    """
    ev_cfg = cfg.get("ev") or {}
    if not ev_cfg.get("enabled", True):
        return True, ""
    ev = c.get("ev")
    if ev is None:
        return False, "no_ev"
    if ev <= float(ev_cfg.get("min_ev_per_contract", 0.0)):
        return False, "min_ev"
    floor = ev_cfg.get("min_ev_on_risk")
    if floor is not None and (ev_on_risk(c) or -1) < float(floor):
        return False, "min_ev_on_risk"
    iv, rv = c.get("iv_short"), c.get("rv")
    mir = ev_cfg.get("min_iv_rv")       # short premium only pays when implied is above the stock's movement
    if mir is not None and c["strategy"] in SHORT_PREMIUM:
        if iv and rv and rv > 0 and iv / rv < float(mir):
            return False, "min_iv_rv"
    # Data-quality guards. Expected value faithfully amplifies a bad quote: the first run after the
    # 2026-11-25 change put a LEAPS call on INSM at the top with +$6,822 a contract, implied vol 0.10
    # against realized 0.97 -- a stale quote on an illiquid long-dated option, not an opportunity.
    # An implied vol far from the stock's own movement, or an expected return on risk larger than any
    # real edge, means the inputs are wrong.
    band = ev_cfg.get("iv_rv_band")
    if band and iv and rv and rv > 0 and not (1.0 / float(band) <= iv / rv <= float(band)):
        return False, "iv_implausible"
    ceil = ev_cfg.get("max_ev_on_risk")
    if ceil is not None and (ev_on_risk(c) or 0) > float(ceil):
        return False, "ev_implausible"
    return True, ""


def rank_pop(c):
    p = c.get("pop_cal")
    return p if p is not None else c["pop_cons"]


def sort_key(c, bucket):
    """Best expected return per dollar at risk first (bucketed so capital breaks near-ties), then
    smallest capital, then highest POP. Falls back to the old POP-first order when EV is off."""
    eor = ev_on_risk(c)
    if eor is None:
        return (2, 0, -math.floor(rank_pop(c) / bucket + 1e-9), c["capital"], -c["roc"])
    # picks that clear the expected-value tests first, then best expected return per dollar at risk;
    # the ones that fail are still listed, below, so you can see how close they came
    passed = 0 if c.get("ev_pass", True) else 1
    return (passed, -math.floor(eor / 0.01 + 1e-9), 0, c["capital"], -rank_pop(c))


def best_per_ticker(cands, bucket):
    best = {}
    for c in sorted(cands, key=lambda c: sort_key(c, bucket)):
        best.setdefault(c["ticker"], c)
    return sorted(best.values(), key=lambda c: sort_key(c, bucket))


def select(cands, cfg):
    """Returns (logged_picks, report_lists).
    logged_picks: top N per (timeframe, strategy), one structure per ticker.
    report_lists: {timeframe: {"top": [...], "by_strategy": {strategy: [...]}}}"""
    bucket = cfg["pop_bucket"]
    n_log = cfg["log_top_n"]
    n_show = cfg["report_top_n"]
    for c in cands:                                      # mark every candidate before anything sorts
        ok, why = ev_pass(c, cfg)
        c["ev_pass"], c["ev_fail"] = ok, ("" if ok else why)
    groups = {}
    for c in cands:
        groups.setdefault((c["timeframe"], c["strategy"]), []).append(c)
    gate_log = (cfg.get("ev") or {}).get("gate_log", True)
    logged, report = [], {}
    for (tf, st), lst in groups.items():
        ranked = best_per_ticker(lst, bucket)
        keep = [c for c in ranked if c.get("ev_pass")] if gate_log else ranked
        for i, c in enumerate(keep[:n_log]):
            c["rank"] = i + 1
            logged.append(c)
        report.setdefault(tf, {"top": [], "by_strategy": {}})["by_strategy"][st] = ranked[:n_show]
    for tf, rep in report.items():
        pool = [c for lst in rep["by_strategy"].values() for c in lst]
        pool.sort(key=lambda c: sort_key(c, bucket))
        per_ticker, top = {}, []
        for c in pool:
            if per_ticker.get(c["ticker"], 0) >= cfg["max_per_ticker_top"]:
                continue
            per_ticker[c["ticker"]] = per_ticker.get(c["ticker"], 0) + 1
            top.append(c)
            if len(top) >= n_show:
                break
        rep["top"] = top
    return logged, report
