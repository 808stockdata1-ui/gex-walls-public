"""The learning loop.

1. mark_open_picks: price every open pick from today's chains; record whether the 50%-profit
   target was reached (checked once per run, so intraday touches between runs are missed).
2. resolve_expired: after expiry, settle each pick at the official close on expiration day.
3. fit_models: once enough picks have resolved, fit a regularized logistic regression that maps
   (conservative POP + signals) -> realized win probability. The model is blended with the raw POP
   in proportion to sample size, and switched off if it does not beat raw POP on a time-ordered
   holdout. Mandatory filters are never touched; only how candidates are scored changes.
"""
import datetime as dt
import json
import math
import warnings

import numpy as np

from .util import log, logit, sigmoid

MODEL_FEATURES = ["logit_pop", "iv_rv", "rsi14", "above_sma50", "above_sma200", "spot_vs_flip",
                  "short_vs_putwall", "short_vol_ratio", "earn_in_window", "vix", "log_dte",
                  "short_delta", "has_8k", "ret20", "spy_above_sma50", "catalyst_risk"]
SHORT_FAMILY = {"PCS", "CCS", "IC", "CSP", "CC"}
# Joe's rule (2026-11-03): picks paying under 10% of their capital (credit or premium / capital) don't count —
# they are left in the database but excluded from the track record, calibration and model training.
MIN_COUNTED_ROC = 0.10


def counted(r):
    return (r["roc"] or 0.0) >= MIN_COUNTED_ROC - 1e-12


def family_of(strategy):
    return "short_premium" if strategy in SHORT_FAMILY else "long_call"


# ---------------------------------------------------------------------------- marking
def _intrinsic(cp, K, S):
    if cp == "C":
        return max(S - K, 0.0)
    if cp == "P":
        return max(K - S, 0.0)
    return 0.0


def position_pnl_per_share(pick, legs, S_now, leg_value):
    """P/L per share now. leg_value(leg) returns the option's current value (mid or intrinsic)."""
    credit = pick["credit"]
    if pick["strategy"] == "CC":
        call = [l for l in legs if l["cp"] == "C"][0]
        return (S_now - pick["price"]) + credit - leg_value(call)
    if pick["strategy"] == "LCALL":
        return leg_value(legs[0]) - (-credit)
    cost_to_close = 0.0
    for l in legs:
        v = leg_value(l)
        cost_to_close += v if l["side"] == "sell" else -v
    return credit - cost_to_close


def mark_open_picks(con, chains, today):
    """chains: {ticker: raw chain dict with 'price' and 'options'}"""
    rows = con.execute("SELECT * FROM picks WHERE status='open'").fetchall()
    n = 0
    for p in rows:
        if dt.date.fromisoformat(p["expiry"]) < today:
            continue
        ch = chains.get(p["ticker"])
        if not ch:
            continue
        by_code = {o["code"]: o for o in ch["options"]}
        legs = json.loads(p["legs"])
        opt_legs = [l for l in legs if l["cp"] in ("C", "P")]
        if any(l["code"] not in by_code for l in opt_legs):
            continue

        def val(l):
            o = by_code[l["code"]]
            if o["bid"] <= 0 and o["ask"] <= 0:
                return 0.0
            return 0.5 * (o["bid"] + o["ask"])

        pnl = position_pnl_per_share(p, legs, ch["price"], val)
        base = abs(p["credit"]) or 1e-9
        pct = pnl / base
        hit = p["hit50"]
        hit_date, days50 = p["hit50_date"], p["days_to_50"]
        if not hit and pct >= 0.5:
            hit, hit_date = 1, today.isoformat()
            days50 = (today - dt.date.fromisoformat(p["date"])).days
        mx = pct if p["max_profit_pct"] is None else max(p["max_profit_pct"], pct)
        mn = pct if p["min_profit_pct"] is None else min(p["min_profit_pct"], pct)
        con.execute("""UPDATE picks SET last_mark=?, last_mark_date=?, hit50=?, hit50_date=?, days_to_50=?,
                       max_profit_pct=?, min_profit_pct=? WHERE pick_id=?""",
                    (pnl, today.isoformat(), hit, hit_date, days50, mx, mn, p["pick_id"]))
        n += 1
    return n


def resolve_expired(con, histories_fn, today):
    """histories_fn(ticker) -> list of daily bars. Settles picks whose expiry has passed."""
    rows = con.execute("SELECT * FROM picks WHERE status='open' AND expiry < ?", (today.isoformat(),)).fetchall()
    cache, n = {}, 0
    for p in rows:
        t = p["ticker"]
        if t not in cache:
            cache[t] = histories_fn(t) or []
        exp = dt.date.fromisoformat(p["expiry"])
        bars = [b for b in cache[t] if b["date"] <= exp]
        if not bars or (exp - bars[-1]["date"]).days > 4:
            continue  # settlement bar not available yet
        S_T = bars[-1]["close"]
        legs = json.loads(p["legs"])
        pnl_ps = position_pnl_per_share(p, legs, S_T, lambda l: _intrinsic(l["cp"], l["K"], S_T))
        pnl = pnl_ps * 100.0
        pnl_pct = pnl / p["capital"] if p["capital"] else None
        con.execute("""UPDATE picks SET status='closed', settle_price=?, pnl=?, pnl_pct=?, win=?, resolved_date=?
                       WHERE pick_id=?""", (S_T, pnl, pnl_pct, int(pnl > 0), today.isoformat(), p["pick_id"]))
        n += 1
    return n


# ---------------------------------------------------------------------------- features
def feature_row(pop_cons, f):
    def g(k, default=None):
        v = f.get(k)
        return default if v is None else float(v)
    dte = g("dte", 30.0)
    return {
        "logit_pop": logit(pop_cons), "iv_rv": g("iv_rv"),
        "rsi14": None if g("rsi14") is None else g("rsi14") / 100.0,
        "above_sma50": g("above_sma50"), "above_sma200": g("above_sma200"),
        "spot_vs_flip": g("spot_vs_flip"), "short_vs_putwall": g("short_vs_putwall"),
        "short_vol_ratio": g("short_vol_ratio"), "earn_in_window": g("earn_in_window", 0.0),
        "vix": None if g("vix") is None else g("vix") / 100.0, "log_dte": math.log1p(max(dte, 0.0)),
        "short_delta": g("short_delta"), "has_8k": 1.0 if (g("n8k_7d", 0.0) or 0) > 0 else 0.0,
        "ret20": g("ret20"), "spy_above_sma50": g("spy_above_sma50"),
        "catalyst_risk": g("catalyst_risk"),  # set by the daily catalyst review (tools/add_catalysts.py)
    }


def _matrix(rows, means=None, stds=None):
    X = np.array([[np.nan if r[k] is None else r[k] for k in MODEL_FEATURES] for r in rows], dtype=float)
    if means is None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=RuntimeWarning)  # all-missing columns
            means = np.nanmean(X, axis=0)
            stds = np.nanstd(X, axis=0)
        means = np.where(np.isnan(means), 0.0, means)
        stds = np.where((stds < 1e-9) | np.isnan(stds), 1.0, stds)
    X = np.where(np.isnan(X), means, X)
    Z = (X - means) / stds
    Z = np.hstack([np.ones((Z.shape[0], 1)), Z])
    return Z, means, stds


def fit_logistic(Z, y, lam=1.0, iters=50):
    w = np.zeros(Z.shape[1])
    reg = np.full(Z.shape[1], lam)
    reg[0] = 0.0
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - y) + reg * w
        H = (Z.T * (p * (1 - p))) @ Z + np.diag(reg + 1e-9)
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-7:
            break
    return w


def fit_models(con, min_train=50, prior_n=200):
    """Fit win and hit50 models per family. Returns number of models stored."""
    rows = [r for r in con.execute("SELECT * FROM picks WHERE status='closed' ORDER BY date, pick_id").fetchall()
            if counted(r)]
    stored = 0
    for fam in ("short_premium", "long_call"):
        fr = [r for r in rows if family_of(r["strategy"]) == fam]
        for target in ("win", "hit50"):
            if len(fr) < min_train:
                continue
            ys = np.array([float(r[target] or 0) for r in fr])
            n_pos = int(ys.sum())
            if n_pos < 5 or len(ys) - n_pos < 5:
                continue
            feats = [feature_row(r["pop_cons"], json.loads(r["features"])) for r in fr]
            base = np.array([r["pop_cons"] for r in fr]) if target == "win" else np.full(len(fr), ys.mean())
            cut = int(len(fr) * 0.8)
            Ztr, m, s = _matrix(feats[:cut])
            wtr = fit_logistic(Ztr, ys[:cut])
            Zte, _, _ = _matrix(feats[cut:], m, s)
            pte = 1.0 / (1.0 + np.exp(-np.clip(Zte @ wtr, -30, 30)))
            brier_base = float(np.mean((base[cut:] - ys[cut:]) ** 2))
            brier_model = float(np.mean((pte - ys[cut:]) ** 2))
            Z, m, s = _matrix(feats)
            w = fit_logistic(Z, ys)
            weight = len(fr) / (len(fr) + prior_n) if brier_model < brier_base else 0.0
            con.execute("""INSERT INTO models (ts, target, family, n, n_pos, feature_names, coefs, means, stds,
                           brier_base, brier_model, weight) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (dt.datetime.now().isoformat(timespec="seconds"), target, fam, len(fr), n_pos,
                         json.dumps(MODEL_FEATURES), json.dumps(w.tolist()), json.dumps(m.tolist()),
                         json.dumps(s.tolist()), brier_base, brier_model, weight))
            stored += 1
            log.info("Model %s/%s n=%d brier base=%.4f model=%.4f weight=%.2f",
                     fam, target, len(fr), brier_base, brier_model, weight)
    return stored


def latest_models(con):
    out = {}
    for r in con.execute("SELECT * FROM models ORDER BY model_id").fetchall():
        out[(r["family"], r["target"])] = r
    return out


def _group_shift(rows, prior_n):
    """Logit shift that moves a group's average POP to its (prior-shrunk) graded win rate. Two-sided."""
    if not rows:
        return 0.0
    n = len(rows)
    wins = sum(1 for r in rows if r["win"])
    avg_pop = sum(r["pop_cons"] for r in rows) / n
    rate = (wins + prior_n * avg_pop) / (n + prior_n)
    return logit(rate) - logit(avg_pop)


def _cal_predict(method, train, r, prior_n=100, min_n=20):
    if method == "raw":
        return r["pop_cons"]
    if method == "group_shift":
        g = [t for t in train if t["timeframe"] == r["timeframe"] and t["strategy"] == r["strategy"]]
        if len(g) < min_n:
            return r["pop_cons"]
        return sigmoid(logit(r["pop_cons"]) + _group_shift(g, prior_n))
    raise ValueError(method)


def choose_calibration(con, min_days=20, min_gain=0.001, prior_n=100):
    """2026-11-18 (GEX Walls merge): pick how the shown POP is calibrated by a walk-forward test, never by fit.

    For every pick date D, a method may only learn from picks that had already expired before D; it is then scored
    (Brier) on D's picks. A calibration is switched on only if it beats the raw model POP by `min_gain` Brier over at
    least `min_days` scored dates. On the 2026-10-19..11-16 record (1,509 graded picks, 16 dates) raw POP won:
    Brier 0.1714 vs 0.1731 for a two-sided group shift and 0.1758 for the old one-sided shrink, because the group
    gaps followed the market's direction that month (weekly CCS +8 pts, weekly PCS -9 pts while SPY fell ~5%)
    and flipped between weeks. So the old shrink is gone and raw POP is shown until a calibration proves itself."""
    rows = sorted((dict(r) for r in con.execute(
        "SELECT pick_id, date, expiry, timeframe, strategy, pop_cons, win, roc FROM picks WHERE status='closed'"
    ).fetchall() if counted(r)), key=lambda r: (r["date"], r["pick_id"]))
    dates = sorted({r["date"] for r in rows})
    scores = {"raw": [], "group_shift": []}
    scored_dates = 0
    for d in dates:
        train = [t for t in rows if t["expiry"] < d]
        test = [t for t in rows if t["date"] == d]
        if len(train) < 20 or not test:
            continue
        scored_dates += 1
        for m in scores:
            scores[m].extend((_cal_predict(m, train, t, prior_n) - t["win"]) ** 2 for t in test)
    brier = {m: (sum(v) / len(v) if v else None) for m, v in scores.items()}
    out = {"method": "raw", "dates_scored": scored_dates, "n_scored": len(scores["raw"]), "brier": brier,
           "groups": {}}
    if scored_dates >= min_days and brier["raw"] is not None and brier["group_shift"] is not None \
            and brier["group_shift"] <= brier["raw"] - min_gain:
        out["method"] = "group_shift"
        groups = {}
        for r in rows:
            groups.setdefault((r["timeframe"], r["strategy"]), []).append(r)
        out["groups"] = {k: _group_shift(v, prior_n) for k, v in groups.items() if len(v) >= 20}
    return out


def group_shrink(con, *args, **kwargs):
    """Kept for old callers: the per-group logit shifts in use (empty while raw POP is the most accurate)."""
    return choose_calibration(con)["groups"]


def apply_models(cands, models):
    """Sets pop_cal (calibrated POP) and p50 (P(hit 50% target)) where a method has PROVEN better than raw POP on a
    walk-forward test (models["_cal"] from choose_calibration; the feature model only when its holdout weight > 0).
    Two-sided: a group that has won more than predicted is shown higher, one that won less is shown lower."""
    cal = models.get("_cal") or {}
    shifts = cal.get("groups") or models.get("_shrink") or {}
    for c in cands:
        fam = family_of(c["strategy"])
        f = feature_row(c["pop_cons"], c)
        for target, key in (("win", "pop_cal"), ("hit50", "p50")):
            mr = models.get((fam, target))
            if not mr or (target == "win" and mr["weight"] <= 0):
                continue
            Z, _, _ = _matrix([f], np.array(json.loads(mr["means"])), np.array(json.loads(mr["stds"])))
            p = float(sigmoid(float(Z[0] @ np.array(json.loads(mr["coefs"])))))
            if target == "win":
                c[key] = mr["weight"] * p + (1 - mr["weight"]) * c["pop_cons"]
            else:
                c[key] = p
        sh = shifts.get((c.get("timeframe"), c.get("strategy")))
        if sh:
            base = c["pop_cal"] if c.get("pop_cal") is not None else c["pop_cons"]
            c["pop_cal"] = sigmoid(logit(base) + sh)
            c["pop_shift"] = sh


# ---------------------------------------------------------------------------- reporting
def summary(con):
    allrows = con.execute("SELECT * FROM picks").fetchall()
    rows = [r for r in allrows if counted(r)]
    closed = [r for r in rows if r["status"] == "closed"]
    out = {"n_total": len(rows), "n_open": len(rows) - len(closed), "n_closed": len(closed),
           "n_excluded": len(allrows) - len(rows), "min_counted_roc": MIN_COUNTED_ROC}

    def rate(lst, k="win"):
        return (sum(1 for r in lst if r[k]) / len(lst)) if lst else None

    groups = {}
    for r in closed:
        groups.setdefault((r["timeframe"], r["strategy"]), []).append(r)
    cal_now = choose_calibration(con)
    shrink_now = cal_now["groups"]
    out["by_group"] = [{
        "timeframe": tf, "strategy": st, "n": len(lst), "win_rate": rate(lst),
        "avg_pop": sum(r["pop_cons"] for r in lst) / len(lst), "hit50_rate": rate(lst, "hit50"),
        "avg_pnl_pct": sum((r["pnl_pct"] or 0) for r in lst) / len(lst),
        "managed_win_rate": sum(1 for r in lst if r["hit50"] or r["win"]) / len(lst),
        # out-of-sample check of the POP actually shown (pop_cal, set at pick time) on the picks that had one
        "n_shown": sum(1 for r in lst if r["pop_cal"] is not None),
        "win_rate_shown": rate([r for r in lst if r["pop_cal"] is not None]),
        "avg_pop_shown": (sum(r["pop_cal"] for r in lst if r["pop_cal"] is not None)
                          / max(1, sum(1 for r in lst if r["pop_cal"] is not None))) or None,
        "shrink_now": shrink_now.get((tf, st)),
    } for (tf, st), lst in sorted(groups.items())]

    buckets = [(0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.85), (0.85, 0.9), (0.9, 0.95), (0.95, 1.01)]
    out["calibration"] = []
    for lo, hi in buckets:
        lst = [r for r in closed if r["strategy"] in SHORT_FAMILY and lo <= r["pop_cons"] < hi]
        if lst:
            out["calibration"].append({"bucket": f"{lo:.0%}–{min(hi, 1):.0%}", "n": len(lst),
                                       "predicted": sum(r["pop_cons"] for r in lst) / len(lst),
                                       "realized": rate(lst)})

    splits = []
    feats = [(r, json.loads(r["features"])) for r in closed if r["strategy"] in SHORT_FAMILY]
    tests = [("Above 50-day average", lambda f: f.get("above_sma50") == 1),
             ("IV above realized vol (IV/RV>1.1)", lambda f: (f.get("iv_rv") or 0) > 1.1),
             ("Price above gamma flip", lambda f: (f.get("spot_vs_flip") or 0) > 0),
             ("Short strike below put wall", lambda f: (f.get("short_vs_putwall") or 0) < 0),
             ("RSI under 40", lambda f: (f.get("rsi14") or 50) < 40),
             ("RSI over 70", lambda f: (f.get("rsi14") or 50) > 70),
             ("8-K filed in last 7 days", lambda f: (f.get("n8k_7d") or 0) > 0),
             ("Catalyst review flagged a risk", lambda f: (f.get("catalyst_risk") or 0) > 0),
             ("VIX above 20", lambda f: (f.get("vix") or 0) > 20)]
    for name, fn in tests:
        yes = [r for r, f in feats if fn(f)]
        no = [r for r, f in feats if not fn(f)]
        if yes and no:
            splits.append({"signal": name, "n_yes": len(yes), "win_yes": rate(yes),
                           "n_no": len(no), "win_no": rate(no)})
    out["signal_splits"] = splits
    out["calibration_choice"] = {"method": cal_now["method"], "dates_scored": cal_now["dates_scored"],
                                 "n_scored": cal_now["n_scored"], "brier": cal_now["brier"]}
    out["models"] = [{"family": m["family"], "target": m["target"], "n": m["n"], "weight": m["weight"],
                      "brier_base": m["brier_base"], "brier_model": m["brier_model"],
                      "coefs": dict(zip(["intercept"] + json.loads(m["feature_names"]), json.loads(m["coefs"])))}
                     for m in latest_models(con).values()]
    return out
