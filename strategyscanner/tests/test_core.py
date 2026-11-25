"""Self-tests for the math and the learning loop. Run:  python tests/test_core.py"""
import datetime as dt
import json
import math
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from scanner import db as dbm  # noqa: E402
from scanner import learn  # noqa: E402
from scanner.indicators import rsi_wilder, realized_vol  # noqa: E402
from scanner.ranker import mandatory_ok, select, sort_key  # noqa: E402
from scanner.util import prob_above, prob_between  # noqa: E402


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


def test_prob_math():
    # At-the-money with zero drift and tiny T is ~50%
    assert approx(prob_above(100, 100, 0.2, 1e-6, 0.0), 0.5, 1e-3)
    # Monotonic in barrier
    assert prob_above(100, 90, 0.3, 0.1, 0.04) > prob_above(100, 95, 0.3, 0.1, 0.04)
    # Between = above(lo) - above(hi)
    p = prob_between(100, 90, 110, 0.3, 0.3, 0.1, 0.04)
    assert approx(p, prob_above(100, 90, 0.3, 0.1, 0.04) - prob_above(100, 110, 0.3, 0.1, 0.04))
    # Known value: S=100, B=100*exp(0.2*sqrt(1)), sigma=0.2, T=1, r=0 -> d2 = (-0.2 - 0.02)/0.2 = -1.1
    B = 100 * math.exp(0.2)
    assert approx(prob_above(100, B, 0.2, 1.0, 0.0), 0.13566606, 1e-6)


def test_rsi_and_vol():
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    # Wilder's classic example data: avg gain 0.23857, avg loss 0.10 -> RSI 70.46
    # (published tables show ~70.5 because their spreadsheet rounds the averages)
    assert abs(rsi_wilder(closes, 14) - 70.464) < 0.01
    flat = [100.0] * 30
    assert rsi_wilder(flat, 14) == 100.0  # no losses
    assert realized_vol([100 * (1.01 ** i) for i in range(30)], 20) < 1e-9  # constant growth, zero vol


def _pick(strategy, legs, credit, price, capital):
    return {"strategy": strategy, "credit": credit, "price": price, "capital": capital}, legs


def test_payoffs():
    I = learn._intrinsic
    # PCS -100P/+95P credit 1.00: at 110 keep 1.00, at 90 lose 4.00, at 99 lose 0.00 (breakeven)
    p, legs = _pick("PCS", [{"cp": "P", "K": 100, "side": "sell"}, {"cp": "P", "K": 95, "side": "buy"}], 1.0, 105, 400)
    for S, want in ((110, 1.0), (90, -4.0), (99, 0.0)):
        got = learn.position_pnl_per_share(p, legs, S, lambda l: I(l["cp"], l["K"], S))
        assert approx(got, want), (S, got, want)
    # CCS -110C/+115C credit 1.50
    p, legs = _pick("CCS", [{"cp": "C", "K": 110, "side": "sell"}, {"cp": "C", "K": 115, "side": "buy"}], 1.5, 105, 350)
    for S, want in ((100, 1.5), (120, -3.5), (111.5, 0.0)):
        got = learn.position_pnl_per_share(p, legs, S, lambda l: I(l["cp"], l["K"], S))
        assert approx(got, want), (S, got, want)
    # CSP -50P credit 1.00
    p, legs = _pick("CSP", [{"cp": "P", "K": 50, "side": "sell"}], 1.0, 55, 4900)
    assert approx(learn.position_pnl_per_share(p, legs, 40, lambda l: I(l["cp"], l["K"], 40)), -9.0)
    # Buy-write at 20, sell 22C for 0.50: called away at 25 -> 2.50; at 18 -> -1.50
    p, legs = _pick("CC", [{"cp": "STK", "K": None, "side": "buy"}, {"cp": "C", "K": 22, "side": "sell"}], 0.5, 20, 1950)
    assert approx(learn.position_pnl_per_share(p, legs, 25, lambda l: I(l["cp"], l["K"], 25)), 2.5)
    assert approx(learn.position_pnl_per_share(p, legs, 18, lambda l: I(l["cp"], l["K"], 18)), -1.5)
    # LEAPS long 80C paid 25 (credit = -25): at 120 -> +15
    p, legs = _pick("LCALL", [{"cp": "C", "K": 80, "side": "buy"}], -25.0, 100, 2500)
    assert approx(learn.position_pnl_per_share(p, legs, 120, lambda l: I(l["cp"], l["K"], 120)), 15.0)
    # Iron condor -90P/+85P -110C/+115C credit 2.00
    p, legs = _pick("IC", [{"cp": "P", "K": 90, "side": "sell"}, {"cp": "P", "K": 85, "side": "buy"},
                           {"cp": "C", "K": 110, "side": "sell"}, {"cp": "C", "K": 115, "side": "buy"}], 2.0, 100, 300)
    for S, want in ((100, 2.0), (80, -3.0), (120, -3.0), (112, 0.0)):
        got = learn.position_pnl_per_share(p, legs, S, lambda l: I(l["cp"], l["K"], S))
        assert approx(got, want), (S, got, want)


def _cand(ticker, pop, capital, roc, tf="monthly", st="PCS"):
    return {"ticker": ticker, "pop_cons": pop, "capital": capital, "roc": roc, "timeframe": tf, "strategy": st}


def test_ranking_order():
    cfg = {"pop_bucket": 0.01, "log_top_n": 5, "report_top_n": 10, "max_per_ticker_top": 2}
    a = _cand("A", 0.905, 500, 0.15)
    b = _cand("B", 0.909, 200, 0.12)   # same POP bucket as A, less capital -> ahead of A
    c = _cand("C", 0.95, 900, 0.10)    # higher POP bucket -> first
    d = _cand("D", 0.909, 200, 0.20)   # same bucket and capital as B, higher return -> ahead of B
    order = [x["ticker"] for x in sorted([a, b, c, d], key=lambda x: sort_key(x, 0.01))]
    assert order == ["C", "D", "B", "A"], order
    logged, lists = select([a, b, c, d], cfg)
    assert [x["ticker"] for x in lists["monthly"]["top"]] == ["C", "D", "B", "A"]


def test_min_roc_annual():
    cfg = {"max_capital": {}, "min_roc": {}, "min_roc_annual": 0.20, "min_pop": {},
           "exclude_earnings_timeframes": []}
    base = {"capital": 500, "earn_in_window": 0, "pop_cons": 0.7, "timeframe": "weekly", "strategy": "CSP"}
    assert mandatory_ok(dict(base, roc=0.004, dte=7), cfg)[0]            # 0.4% / 7d ~ 20.9% ann
    assert mandatory_ok(dict(base, roc=0.003, dte=7), cfg) == (False, "min_roc_annual")  # ~15.6%
    assert mandatory_ok(dict(base, roc=0.0006, dte=0), cfg)[0]           # 0-DTE treated as 1 day
    assert mandatory_ok(dict(base, roc=0.10, dte=365, timeframe="leaps"), cfg) == (False, "min_roc_annual")


def test_min_profit_per_trade():
    cfg = {"max_capital": {}, "min_roc": {}, "min_profit": 0.20, "min_pop": {}, "exclude_earnings_timeframes": []}
    base = {"capital": 500, "earn_in_window": 0, "pop_cons": 0.8, "timeframe": "weekly", "dte": 7}
    assert mandatory_ok(dict(base, strategy="PCS", roc=0.20), cfg)[0]
    assert mandatory_ok(dict(base, strategy="PCS", roc=0.19), cfg) == (False, "min_profit")
    assert mandatory_ok(dict(base, strategy="CSP", roc=0.005), cfg) == (False, "min_profit")
    # buy-write: judged on the gain if called away, not the premium alone
    assert mandatory_ok(dict(base, strategy="CC", roc=0.02, ret_called=0.22), cfg)[0]
    assert mandatory_ok(dict(base, strategy="CC", roc=0.02, ret_called=0.10), cfg) == (False, "min_profit")


def test_learning_loop():
    """Simulate 300 resolved picks where a hidden signal (iv_rv) matters; the model should find it."""
    tmp = tempfile.mkdtemp()
    con = dbm.connect(os.path.join(tmp, "t.db"))
    rnd = random.Random(7)
    base = dt.date(2026, 1, 5)
    for i in range(300):
        pop = rnd.uniform(0.6, 0.95)
        ivrv = rnd.uniform(0.7, 1.8)
        true_p = min(0.99, max(0.01, pop + 0.25 * (ivrv - 1.2)))
        win = int(rnd.random() < true_p)
        d = base + dt.timedelta(days=i // 5)
        feats = {k: None for k in dbm.FEATURE_KEYS}
        feats.update({"iv_rv": ivrv, "rsi14": rnd.uniform(25, 75), "dte": 30, "short_delta": 0.2})
        con.execute("""INSERT INTO picks (key, run_id, date, timeframe, strategy, ticker, expiry, dte, legs, price,
                       credit, capital, roc, pop_iv, pop_rv, pop_cons, features, status, win, hit50, pnl)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"k{i}", 1, d.isoformat(), "monthly", "PCS", "TST", (d + dt.timedelta(30)).isoformat(), 30,
                     "[]", 100, 1.0, 400, 0.25, pop, pop, pop, json.dumps(feats), "closed", win, win, 100 if win else -400))
    con.commit()
    n = learn.fit_models(con, min_train=50, prior_n=200)
    assert n >= 1
    models = learn.latest_models(con)
    m = models[("short_premium", "win")]
    coefs = dict(zip(["intercept"] + json.loads(m["feature_names"]), json.loads(m["coefs"])))
    assert coefs["iv_rv"] > 0, coefs  # learned that rich IV helps
    assert coefs["logit_pop"] > 0, coefs
    # Calibrated POP should rise with iv_rv for the same raw POP
    lo = {"strategy": "PCS", "pop_cons": 0.8, "iv_rv": 0.8, "dte": 30, "short_delta": 0.2}
    hi = dict(lo, iv_rv=1.7)
    learn.apply_models([lo, hi], models)
    if m["weight"] > 0:
        assert hi["pop_cal"] > lo["pop_cal"], (lo, hi)
    s = learn.summary(con)
    assert s["n_closed"] == 300 and s["calibration"]


def test_walk_forward_calibration():
    """A calibration is used only when it beats raw POP walk-forward; then it is two-sided."""
    rnd = random.Random(7)
    for bias, want in ((0.15, "group_shift"), (0.0, "raw")):
        tmp = tempfile.mkdtemp()
        con = dbm.connect(os.path.join(tmp, "t.db"))
        base = dt.date(2026, 1, 5)
        i = 0
        for day in range(60):
            d = base + dt.timedelta(days=day)
            for st, sign in (("PCS", -1), ("CCS", 1)):
                for _ in range(8):
                    pop = rnd.uniform(0.6, 0.8)
                    win = int(rnd.random() < min(0.99, max(0.01, pop + sign * bias)))
                    feats = {k: None for k in dbm.FEATURE_KEYS}
                    con.execute("""INSERT INTO picks (key, run_id, date, timeframe, strategy, ticker, expiry, dte, legs,
                                   price, credit, capital, roc, pop_iv, pop_rv, pop_cons, features, status, win, hit50, pnl)
                                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (f"k{i}", 1, d.isoformat(), "weekly", st, "TST", (d + dt.timedelta(3)).isoformat(), 3,
                                 "[]", 100, 1.0, 400, 0.25, pop, pop, pop, json.dumps(feats), "closed", win, win, 0))
                    i += 1
        con.commit()
        cal = learn.choose_calibration(con)
        assert cal["method"] == want, (bias, cal["method"], cal["brier"])
        if want == "group_shift":
            c1 = {"strategy": "CCS", "timeframe": "weekly", "pop_cons": 0.7}
            c2 = {"strategy": "PCS", "timeframe": "weekly", "pop_cons": 0.7}
            learn.apply_models([c1, c2], {"_cal": cal})
            assert c1["pop_cal"] > 0.75 and c2["pop_cal"] < 0.65, (c1, c2)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
    print("all tests passed")
