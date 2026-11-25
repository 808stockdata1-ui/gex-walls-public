"""GEX Walls daily audit: accuracy, speed, freshness, coverage, calibration (self-graded picks) and usage.

Read-only against the running app (default http://127.0.0.1:8050). Writes, next to this file:
  latest.json        scorecard + every check and finding
  YYYY-MM-DD.md      human-readable report
  history.jsonl      one scorecard line per run (progress over time)
  picks_log.jsonl    trades the app recommended, logged daily and graded after expiry (or after the swing window)

Run:  .venv\\Scripts\\python.exe audit\\audit.py      (from the GEXWalls folder)
No accounts, no paid data: CBOE delayed quotes/history and Nasdaq.com only.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
APP = HERE.parent
BASE = os.environ.get("GEXWALLS_URL", "http://127.0.0.1:8050")
UA = {"User-Agent": "Mozilla/5.0 (GEX Walls audit, local)", "Accept": "application/json"}
CBOE_Q = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/{s}.json"
CBOE_HIST = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/{s}.json"
NASDAQ_Q = "https://api.nasdaq.com/api/quote/{s}/info?assetclass={c}"
NASDAQ_X = "https://api.nasdaq.com/api/quote/{s}/extended-trading?markettype={m}&assetclass=etf"


def et_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        return datetime.now(timezone(timedelta(hours=-5)))


def rj(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def get(path: str, timeout: float = 120):
    t0 = time.perf_counter()
    r = requests.get(BASE + path, timeout=timeout)
    ms = (time.perf_counter() - t0) * 1000
    return r, ms


class Audit:
    def __init__(self):
        self.checks: list[dict] = []          # {cat, name, ok, value, target, note}
        self.findings: list[str] = []
        self.metrics: dict = {}

    def check(self, cat, name, ok, value=None, target=None, note=""):
        self.checks.append({"cat": cat, "name": name, "ok": bool(ok), "value": value, "target": target, "note": note})
        if not ok:
            self.findings.append(f"[{cat}] {name}: {value}{' (target ' + str(target) + ')' if target is not None else ''}"
                                 f"{' — ' + note if note else ''}")


# ------------------------------------------------------------------ speed
def speed(a: Audit) -> None:
    eps = [("/", 1500), ("/api/watch", 1500), ("/api/scan", 2500), ("/api/picks", 1500), ("/api/capital", 4000),
           ("/api/calls", 2500), ("/api/csp", 2500), ("/api/mining_earnings?days=21", 4000), ("/api?t=SPY", 9000)]
    res = {}
    for p, tgt in eps:
        try:
            get(p)                                  # warm caches the way a user would hit them
            r, ms = get(p)
            size = len(r.content)
            res[p] = {"ms": round(ms), "kb": round(size / 1024), "status": r.status_code}
            a.check("speed", f"GET {p}", r.status_code == 200 and ms <= tgt, f"{ms:.0f} ms, {size / 1024:.0f} KB", f"≤{tgt} ms")
        except Exception as e:
            res[p] = {"error": str(e)}
            a.check("speed", f"GET {p}", False, f"error {e}")
    a.metrics["speed"] = res
    page = res.get("/", {}).get("kb")
    if page:
        a.check("speed", "page size", page <= 450, f"{page} KB", "≤450 KB", "large page = slower phone loads")


# ------------------------------------------------------------------ freshness
def _age_h(ts: str | None) -> float | None:
    if not ts:
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", str(ts))
    if not m:
        m2 = re.match(r"(\d{4}-\d{2}-\d{2})$", str(ts))
        if not m2:
            return None
        d = datetime.fromisoformat(m2.group(1)).replace(hour=16)
    else:
        d = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}:{m.group(3)}")
    now = et_now().replace(tzinfo=None)
    h = (now - d).total_seconds() / 3600
    # weekends don't count against freshness
    wk = sum(1 for i in range(int(h // 24) + 1) if (now - timedelta(days=i)).weekday() >= 5)
    return max(0.0, h - 24 * wk)


# NYSE full-day holidays (nyse.com/markets/hours-calendars, read 2026-11-17) - extend each year
NYSE_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
                 "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}


def freshness(a: Audit) -> None:
    out = {}
    def one(name, ts, max_h):
        age = _age_h(ts)
        out[name] = {"ts": ts, "age_h": None if age is None else round(age, 1)}
        a.check("freshness", name, age is not None and age <= max_h, f"{ts or 'never run'}" + (f" ({age:.0f} h old)" if age is not None else ""), f"≤{max_h} h")
    try:
        sc = get("/api/scan")[0].json()
        one("momentum + spread scan", (sc.get("results") or {}).get("ts"), 26)
        one("sector ETF GEX snapshot", (sc.get("sector_gex") or {}).get("last_run"), 26)
    except Exception as e:
        a.check("freshness", "scan", False, str(e))
    for nm, path, mx in (("sell-puts scan", "/api/csp", 26), ("calls scan", "/api/calls", 72)):
        try:
            one(nm, (get(path)[0].json().get("results") or {}).get("ts"), mx)
        except Exception as e:
            a.check("freshness", nm, False, str(e))
    try:                                   # early moves / swing / algos read daily bars: asof = last bar date (16:00 ET)
        ea = get("/api/edge")[0].json().get("asof")
        hol = sum(1 for h_ in NYSE_HOLIDAYS if ea and str(ea)[:10] < h_ <= et_now().date().isoformat())
        one("early-moves bars (edge asof)", ea, 26 + 24 * hol)
    except Exception as e:
        a.check("freshness", "early-moves bars", False, str(e))
    try:
        pk = get("/api/picks")[0].json()
        one("Strategy Scanner picks", pk.get("file_time"), 26)
    except Exception as e:
        a.check("freshness", "picks", False, str(e))
    a.metrics["freshness"] = out


# ------------------------------------------------------------------ accuracy
def _wilder_rsi(closes: list[float], n: int = 14) -> float | None:
    if len(closes) <= n:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for g, l_ in zip(gains[n:], losses[n:]):
        ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + l_) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def _ndist_pdf(x):
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def accuracy(a: Audit) -> None:
    acc = {}
    for tk in ("SPY", "QQQ", "IWM"):
        try:
            r = get(f"/api?t={tk}", 180)[0].json()
        except Exception as e:
            a.check("accuracy", f"{tk} lookup", False, str(e))
            continue
        if r.get("error"):
            a.check("accuracy", f"{tk} lookup", False, r["error"])
            continue
        live = r.get("live") or {}
        spot, px = r.get("spot"), live.get("price")
        # 1. price: CBOE vs Nasdaq (independent feeds)
        ap = px or spot
        try:
            try:
                q = requests.get(NASDAQ_Q.format(s=tk, c="etf"), headers=UA, timeout=15).json()["data"]["primaryData"]
                nq = float(str(q["lastSalePrice"]).replace("$", "").replace(",", ""))
                diff = abs(nq / (px or spot) - 1) * 100
                lbl = "Nasdaq"
            except Exception:   # Nasdaq down ("Error while calling vendor", 2026-11-15): CBOE quote file instead
                cq = requests.get(CBOE_Q.format(s=tk), headers=UA, timeout=15).json()["data"]
                best = None
                for nm, ref in (("last", cq.get("current_price")), ("close", cq.get("close"))):
                    for mine, who in ((px, "live"), (live.get("close"), "close"), (spot, "spot")):
                        if ref and mine:
                            d_ = abs(float(ref) / float(mine) - 1) * 100
                            if best is None or d_ < best[0]:
                                best = (d_, float(ref), float(mine), f"CBOE {nm} (Nasdaq down) vs app {who}")
                if best is None:
                    raise ValueError("Nasdaq and CBOE quote both unavailable")
                diff, nq, ap, lbl = best
            if diff > 0.5:   # outside 9:30-16:00 the app may hold an extended-hours trade; compare with Nasdaq's too
                from zoneinfo import ZoneInfo
                _h = datetime.now(ZoneInfo("America/New_York"))
                _m = _h.hour * 60 + _h.minute
                for mt in (("pre",) if 240 <= _m < 570 else ("post",) if (_m >= 960 or _m < 240) else ()):
                    try:
                        x = requests.get(NASDAQ_X.format(s=tk, m=mt), headers=UA, timeout=15).json()["data"]["infoTable"]["rows"][0]
                        xq = float(str(x["consolidated"]).split()[0].replace("$", "").replace(",", ""))
                    except Exception:
                        continue
                    xd = abs(xq / ap - 1) * 100
                    if xd < diff:
                        nq, diff, lbl = xq, xd, f"Nasdaq {mt}-market"
            a.check("accuracy", f"{tk} price vs Nasdaq", diff <= 0.5, f"app {ap:.2f} vs {lbl} {nq:.2f} ({diff:.2f}%)", "≤0.5%")
        except Exception as e:
            a.check("accuracy", f"{tk} price vs Nasdaq", False, f"Nasdaq unavailable: {e}")
        # 2. RSI recomputed independently from the candles the app returned
        closes = [c[4] for c in (r.get("candles") or []) if c and c[4] is not None]
        mine = _wilder_rsi(closes)
        if mine is not None and r.get("rsi") is not None:
            a.check("accuracy", f"{tk} RSI(14) recompute", abs(mine - r["rsi"]) <= 1.0, f"app {r['rsi']:.1f} vs audit {mine:.1f}", "±1.0")
        # 3. walls on the right side of spot
        bad = [e["exp"] for e in r.get("expirations") or []
               if (e.get("put_wall") and e["put_wall"] > spot * 1.001) or (e.get("call_wall") and e["call_wall"] < spot * 0.999)]
        a.check("accuracy", f"{tk} walls vs spot", not bad, "ok" if not bad else f"wrong side: {bad}")
        # 4. spread arithmetic in the strategy card
        errs, n = 0, 0
        for w in ((r.get("strategy") or {}).get("windows") or {}).values():
            for P in list((w.get("profiles") or {}).values()) + list((w.get("nearest") or {}).values()) + [w.get("best") or []]:
                for c in P or []:
                    n += 1
                    if not (0 < c["credit"] < c["width"]) or abs(c["maxloss"] - (c["width"] - c["credit"]) * 100) > 1 \
                            or not (0 <= c["pop"] <= 100) or abs(c["be"] - (c["short"] - c["credit"])) > 0.01:
                        errs += 1
        a.check("accuracy", f"{tk} spread math ({n} spreads)", errs == 0, f"{errs} inconsistent")
        # 5. GEX inputs: Cboe's greeks vs Black-Scholes gamma at the displayed spot (matrix-gex PR #67, fix 1)
        acc[tk] = {"spot": spot, "rsi": r.get("rsi"), "rsi_audit": mine, "gamma_source": r.get("gamma_source")}
        if tk == "SPY":
            a.metrics["gamma_source"] = r.get("gamma_source")
            a.check("accuracy", "GEX gamma at displayed spot (Black-Scholes)", r.get("gamma_source") == "bs_spot",
                    r.get("gamma_source") or "cboe greeks", "bs_spot",
                    "Cboe greeks can use a stale underlying; recomputing at spot avoids GEX sign errors")
    # 6. capital table sanity
    try:
        cap = get("/api/capital")[0].json()
        rows = cap.get("rows") or []
        bad = [r_ for r_ in rows if r_["dte"] < 0 or not (0 <= r_["p"] <= 1) or r_["capital"] <= 0]
        a.check("accuracy", f"capital table ({len(rows)} rows)", not bad, f"{len(bad)} invalid rows")
        a.metrics["capital_rows"] = {t: sum(1 for r_ in rows if r_["type"] == t) for t in sorted({r_["type"] for r_ in rows})}
        a.metrics["swing_rows"] = len(cap.get("swing") or [])
        try:                                   # early-move engine: fresh tables + each setup's back-test vs arbitrary entries
            eg = get("/api/edge")[0].json()
            a.metrics["edge"] = {"asof": eg.get("asof"), "universe": eg.get("universe"), "rows": len(eg.get("rows") or []),
                                 "baseline_r": (eg.get("baseline") or {}).get("avg_r"),
                                 "baseline_recent_r": (eg.get("baseline") or {}).get("rec_r"),
                                 "setups": {x["key"]: {"n": x["bt"].get("n"), "avg_r": x["bt"].get("avg_r"), "recent_r": x["bt"].get("rec_r"),
                                                       "beats": x.get("beats"), "paper_n": (x.get("paper") or {}).get("n"),
                                                       "paper_r": (x.get("paper") or {}).get("avg_r"),
                                                       "paused": x.get("paused")} for x in eg.get("algos") or []}}
        except Exception as e:
            a.metrics["edge"] = {"error": str(e)}
    except Exception as e:
        a.check("accuracy", "capital table", False, str(e))
    # 7. clock: Eastern time with daylight saving handled
    try:
        from zoneinfo import ZoneInfo
        off = datetime.now(ZoneInfo("America/New_York")).utcoffset().total_seconds() / 3600
        a.check("accuracy", "Eastern time zone data", off in (-4, -5), f"UTC{off:+.0f}")
    except Exception as e:
        a.check("accuracy", "Eastern time zone data", False, f"tzdata missing ({e}) — app falls back to a fixed offset")
    a.metrics["accuracy"] = acc


# ------------------------------------------------------------------ coverage
def coverage(a: Audit) -> None:
    try:
        r = get("/api?t=SPY", 180)[0].json()
        wins = (r.get("strategy") or {}).get("windows") or {}
        empty = [w for w, v in wins.items() if w != "0dte" and not (v.get("listed") if "listed" in v else v.get("exps"))]
        fresh = [w for w, v in wins.items() if v.get("listed") and not v.get("quoted")]
        # the furthest week chip (~9 weeks out) is often past the exchange's weekly listing horizon (e.g. 2026-11-09:
        # SPY listed Dec 31 then Jan 15, no Jan 8 yet) - not an app gap, so only a note when it is the ONLY empty one
        # 2026-11-13: also Jan 8 while Jan 15 (monthly) was listed - CBOE's SPY file had Dec 24, Dec 31, Jan 15, Jan 29.
        # A week whose Friday is > 6 weeks out is past the weekly listing horizon: note, not a failure.
        weeks = [w for w in wins if w.startswith("w")]
        _t0 = et_now().date()

        def _far(w: str) -> bool:
            try:
                return (datetime.fromisoformat(w[1:]).date() - _t0).days > 42
            except Exception:
                return False
        horizon = [w for w in empty if weeks and (w == weeks[-1] or _far(w))]
        empty = [w for w in empty if w not in horizon]
        a.check("coverage", f"SPY spread windows ({len(wins)})", not empty,
                ("all have expirations" if not empty else f"no expiration listed: {empty}")
                + (f"; past the exchange weekly listing horizon (not listed yet): {horizon}" if horizon else "")
                + (f"; newly listed, no quotes yet: {fresh}" if fresh else ""))
        a.check("coverage", "SPY week-by-week GEX", len(r.get("weeks") or []) >= 6, f"{len(r.get('weeks') or [])} weeks", "≥6")
        ds = [e["dte"] for e in (r.get("expirations") or [])]
        gap = max((b - a_ for a_, b in zip(ds, ds[1:])), default=0)
        a.check("coverage", "SPY wall expirations: largest time gap", gap <= 35, f"{gap} days between listed expirations", "≤35 days")
        a.check("coverage", "SPY next-4-weeks GEX", sum(1 for w in (r.get("gex4") or []) if w.get("net") is not None) >= 4,
                f"{sum(1 for w in (r.get('gex4') or []) if w.get('net') is not None)} of 4 weeks", "4")
    except Exception as e:
        a.check("coverage", "SPY lookup", False, str(e))
    try:
        sc = get("/api/scan")[0].json().get("results") or {}
        a.check("coverage", "momentum universe", (sc.get("universe") or 0) >= 200, f"{sc.get('universe')} tickers", "≥200")
        a.check("coverage", "spread scan chains", len(sc.get("items") or {}) >= 25, f"{len(sc.get('items') or {})} tickers", "≥25")
    except Exception as e:
        a.check("coverage", "scan", False, str(e))
    cr = a.metrics.get("capital_rows") or {}
    for t in ("credit", "put", "cc", "call", "bcs", "leaps"):
        a.check("coverage", f"capital table: {t}", cr.get(t, 0) >= 5, f"{cr.get(t, 0)} rows", "≥5")
    eg_ = a.metrics.get("edge") or {}       # swing setups now come only from signals that fired: 0 on a quiet day is valid
    a.check("coverage", "early-move engine", (eg_.get("universe") or 0) >= 150 and (eg_.get("rows") or 0) >= 50,
            f"{eg_.get('universe')} tickers, {eg_.get('rows')} rows, {a.metrics.get('swing_rows')} swing setups, bars to {eg_.get('asof')}",
            "≥150 tickers")
    bl_ = eg_.get("baseline_r")              # 12a review idea: forward (paper) log vs arbitrary entries, judged once 20+ closed
    for k_, s_ in sorted((eg_.get("setups") or {}).items()):
        if (s_.get("paper_n") or 0) >= 20 and s_.get("paper_r") is not None and bl_ is not None:
            a.check("calibration", f"early-move {k_} forward log", s_["paper_r"] >= bl_ or bool(s_.get("paused")),
                    f"{s_['paper_r']:+.2f}R over {s_['paper_n']} closed vs {bl_:+.2f}R arbitrary entries"
                    + (" - paused for swing picks (app 21a)" if s_.get("paused") else ""), "≥ baseline, or paused",
                    "below the back-test baseline out of sample - consider marking the setup not used")


# ------------------------------------------------------------------ calibration: log picks, grade them later
LEG_RE = re.compile(r"([−+-])\s*([\d.]+)\s*([CP])")


def _hist(sym: str, cache: dict) -> dict:
    if sym in cache:
        return cache[sym]
    out = {}
    for s in (sym, "_" + sym):
        try:
            r = requests.get(CBOE_HIST.format(s=s), headers=UA, timeout=30)
            if r.status_code == 200:
                for row in r.json().get("data") or []:
                    out[str(row["date"])[:10]] = (float(row["high"]), float(row["low"]), float(row["close"]))
                break
        except Exception:
            continue
    cache[sym] = out
    return out


MIN_COUNTED = 0.10      # Joe's rule: a pick paying under 10% of its capital doesn't count (kept in the log, not scored)


def counted(x: dict) -> bool:
    if x.get("type") == "swing":
        if x.get("setup"):                 # rebuilt 2026-11-12: measured against the stop (2:1 target = 200% of the risk)
            return bool(x.get("price")) and bool(x.get("stop")) and x["price"] > x["stop"]
        return bool(x.get("price")) and (x.get("target") or 0) / x["price"] - 1 >= MIN_COUNTED - 1e-9
    return (x.get("g") or 0) >= MIN_COUNTED - 1e-9


def log_picks(a: Audit, today: str) -> int:
    """Log today's top recommendations (top 3 per strategy, ≤30 days + LEAPS-free) and the top 10 swing setups."""
    log = HERE / "picks_log.jsonl"
    have = set()
    if log.exists():
        for ln in log.read_text(encoding="utf-8").splitlines():
            try:
                have.add(json.loads(ln)["id"])
            except Exception:
                pass
    try:
        cap = get("/api/capital")[0].json()
    except Exception:
        return 0
    new = []
    rows = sorted([r for r in cap.get("rows") or [] if r["type"] != "leaps" and r["dte"] <= 30 and counted(r)],
                  key=lambda r: -r["evd"])
    per: dict = {}
    for r in rows:
        if per.get(r["type"], 0) >= 3:
            continue
        per[r["type"]] = per.get(r["type"], 0) + 1
        legs = [{"s": 1 if m[0] == "+" else -1, "k": float(m[1]), "t": m[2]} for m in LEG_RE.findall(r.get("legs") or "")]
        rid = f"{today}|{r['type']}|{r['ticker']}|{r['exp']}|{r.get('legs')}"
        if rid in have:
            continue
        new.append({"id": rid, "logged": today, "type": r["type"], "strat": r.get("strat"), "ticker": r["ticker"],
                    "exp": r["exp"], "legs": legs, "price": r.get("price"), "capital": r["capital"], "collect": r["collect"],
                    "p": r["p"], "g": r["g"], "src": r.get("src"), "status": "open"})
    for x in [s_ for s_ in (cap.get("swing") or []) if counted({"type": "swing", **s_})][:10]:
        rid = f"{today}|swing|{x['ticker']}"
        if rid in have:
            continue
        new.append({"id": rid, "logged": today, "type": "swing", "ticker": x["ticker"], "price": x["price"], "stop": x["stop"],
                    "target": x["target"], "p": x["p"], "days": x["days"], "setup": x.get("setup"), "status": "open"})
    with log.open("a", encoding="utf-8") as f:
        for n_ in new:
            f.write(json.dumps(n_) + "\n")
    return len(new)


def _pnl(x: dict, S: float) -> float:
    """P/L per position at expiration given the underlying close S (cash-settled view)."""
    v = 0.0
    for l_ in x["legs"]:
        intr = max(S - l_["k"], 0) if l_["t"] == "C" else max(l_["k"] - S, 0)
        v += l_["s"] * intr * 100
    if x["type"] in ("credit", "put"):
        return x["collect"] + v
    if x["type"] == "cc":
        return x["collect"] + v + (S - (x["price"] or S)) * 100
    return v - x["capital"]                      # long call / bull call spread: paid capital


def scanner_calibration(a: Audit) -> None:
    """The Picks scanner's own graded record (built into GEX Walls since 2026-11-18: APP/strategy).
    Raw model POP vs what happened, per (timeframe, strategy) with 20+ graded picks."""
    lj = rj(Path(os.environ.get("STRATEGY_SCANNER_DIR", str(APP / "strategy"))) / "output" / "latest.json", {}) or {}
    cc = (lj.get("learning") or {}).get("calibration_choice")
    if cc and (cc.get("brier") or {}).get("raw") is not None:
        b = cc["brier"]
        used = b.get(cc["method"], b["raw"])
        a.check("calibration", "Picks: shown chance of profit is the most accurate (walk-forward)",
                used is not None and used <= min(v for v in b.values() if v is not None) + 0.001,
                f"{cc['method']} — Brier " + ", ".join(f"{k} {v:.4f}" for k, v in b.items() if v is not None)
                + f" over {cc.get('dates_scored')} dates", "lowest Brier")
    for g in ((lj.get("learning") or {}).get("by_group") or []):
        if g.get("n", 0) >= 20:
            a.check("calibration", f"Picks scanner {g['timeframe']} {g['strategy']}",
                    abs(g["win_rate"] - g["avg_pop"]) <= 0.08,
                    f"won {g['win_rate'] * 100:.0f}% vs predicted {g['avg_pop'] * 100:.0f}% (n={g['n']})"
                    + (f"; shown subset won {g['win_rate_shown'] * 100:.0f}% vs {g['avg_pop_shown'] * 100:.0f}%"
                       f" (n={g['n_shown']})" if g.get("n_shown") and g.get("win_rate_shown") is not None
                       and g.get("avg_pop_shown") is not None else ""), "within 8 pts")


def grade(a: Audit) -> dict:
    log = HERE / "picks_log.jsonl"
    if not log.exists():
        return {}
    rows = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    today = et_now().date().isoformat()
    cache: dict = {}
    for x in rows:
        if x.get("status") != "open":
            continue
        h = _hist(x["ticker"], cache)
        if not h:
            continue
        if x["type"] == "swing":
            days = sorted(d for d in h if d > x["logged"])
            limit = days[: max(5, int(x.get("days") or 20) * 2)]
            for d in limit:
                hi, lo, cl = h[d]
                if lo <= x["stop"]:
                    x.update(status="closed", win=False, closed=d, pnl_pct=(x["stop"] / x["price"] - 1) * 100); break
                if hi >= x["target"]:
                    x.update(status="closed", win=True, closed=d, pnl_pct=(x["target"] / x["price"] - 1) * 100); break
            else:
                if len(limit) >= max(5, int(x.get("days") or 20) * 2):
                    cl = h[limit[-1]][2]
                    x.update(status="closed", win=cl > x["price"], closed=limit[-1], pnl_pct=(cl / x["price"] - 1) * 100)
            continue
        if x["exp"] >= today or x["exp"] not in h:
            if x["exp"] < today and x["exp"] not in h:
                prior = [d for d in h if d <= x["exp"]]
                if not prior:
                    continue
                S = h[max(prior)][2]
            else:
                continue
        else:
            S = h[x["exp"]][2]
        pnl = _pnl(x, S)
        x.update(status="closed", win=pnl > 0, closed=x["exp"], settle=S, pnl=pnl, pnl_pct=pnl / x["capital"] * 100)
    log.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")
    closed = [x for x in rows if x.get("status") == "closed" and counted(x)]
    by: dict = {}
    for x in closed:                 # swing rows logged before the 2026-11-12 rebuild (no `setup`) are graded apart
        by.setdefault("swing_old" if x["type"] == "swing" and not x.get("setup") else x["type"], []).append(x)
    stats = {}
    for t, L in by.items():
        brier = statistics.fmean((x["p"] - (1 if x["win"] else 0)) ** 2 for x in L)
        stats[t] = {"n": len(L), "won": sum(1 for x in L if x["win"]), "pred": statistics.fmean(x["p"] for x in L),
                    "brier": brier, "clusters": len({(x["ticker"], x.get("exp") or x.get("closed")) for x in L}),
                    "avg_pnl_pct": statistics.fmean(x.get("pnl_pct") or 0 for x in L)}
    a.metrics["calibration"] = {"open": sum(1 for x in rows if x.get("status") == "open" and counted(x)), "closed": len(closed),
                                "not_counted": sum(1 for x in rows if not counted(x)), "by_type": stats}
    for t, s in stats.items():
        if t == "swing_old":                            # retired engine: shown in the report, not scored
            continue
        if s["clusters"] >= 20:                         # independent (ticker, expiry) clusters before trusting a hit rate
            a.check("calibration", f"{t}: predicted vs actual", abs(s["won"] / s["n"] - s["pred"]) <= 0.08,
                    f"won {s['won']}/{s['n']} = {s['won'] / s['n'] * 100:.0f}% vs predicted {s['pred'] * 100:.0f}% · Brier {s['brier']:.3f}",
                    "within 8 pts")
    return stats


# ------------------------------------------------------------------ usage (what the user actually opens)
def usage(a: Audit) -> None:
    u = rj(APP / "usage.json", {}) or {}
    days = sorted((u.get("days") or {}).items())[-14:]
    tot: dict = {}
    for _, d in days:
        for k, v in d.items():
            tot[k] = tot.get(k, 0) + v
    a.metrics["usage_14d"] = dict(sorted(tot.items(), key=lambda kv: -kv[1]))
    tabs = {k: v for k, v in tot.items() if k.startswith("tab:")}
    if tabs:
        unused = [k[4:] for k in ("tab:picks", "tab:main", "tab:puts", "tab:calls", "tab:calc", "tab:flow") if not tot.get(k)]
        a.check("ux", "tabs used in last 14 days", not unused, "all used" if not unused else f"unused: {unused}",
                note="candidates to merge or move")


# ------------------------------------------------------------------ phone reachability (LAN listen address)
def lan_reach(a: Audit) -> None:
    """2026-11-18: the app was found listening on 127.0.0.1 only (started by double-click, no --lan) on 11-16 and
    11-17, so Joe's phone could not open it. Connect to the PC's own Wi-Fi address on the app's port."""
    import socket
    port = int(BASE.rsplit(":", 1)[-1].split("/")[0]) if BASE.rsplit(":", 1)[-1].split("/")[0].isdigit() else 8050
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
    except OSError as e:
        a.check("ux", "phone (Wi-Fi) can reach the app", False, f"no network address: {e}")
        return
    try:
        with socket.create_connection((ip, port), timeout=5):
            pass
        a.check("ux", "phone (Wi-Fi) can reach the app", True, f"http://{ip}:{port}", "listening on the home network")
    except OSError as e:
        a.check("ux", "phone (Wi-Fi) can reach the app", False, f"http://{ip}:{port} refused ({e.__class__.__name__})",
                "listening on the home network", "restart with GEX Walls (phone on Wi-Fi).bat")


# ------------------------------------------------------------------ score + report
WEIGHTS = {"accuracy": 0.3, "calibration": 0.2, "freshness": 0.15, "speed": 0.15, "coverage": 0.15, "ux": 0.05}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    a = Audit()
    t0 = time.time()
    today = et_now().date().isoformat()
    for fn in (speed, freshness, accuracy, coverage, usage, lan_reach):
        try:
            fn(a)
        except Exception as e:
            a.check("audit", fn.__name__, False, f"crashed: {e}")
    try:
        scanner_calibration(a)
        grade(a)
        a.metrics["logged_today"] = log_picks(a, today)
    except Exception as e:
        a.check("audit", "pick logging/grading", False, f"crashed: {e}")
    scores = {}
    for cat in WEIGHTS:
        cs = [c for c in a.checks if c["cat"] == cat]
        if cs:
            scores[cat] = round(100 * sum(c["ok"] for c in cs) / len(cs))
    wsum = sum(WEIGHTS[c] for c in scores)
    overall = round(sum(scores[c] * WEIGHTS[c] for c in scores) / wsum) if wsum else None
    out = {"date": today, "time": et_now().strftime("%H:%M ET"), "overall": overall, "scores": scores,
           "checks": a.checks, "findings": a.findings, "metrics": a.metrics, "seconds": round(time.time() - t0)}
    (HERE / "latest.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    with (HERE / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"date": today, "time": out["time"], "overall": overall, "scores": scores,
                            "n_checks": len(a.checks), "n_failed": len(a.findings)}) + "\n")
    md = [f"# GEX Walls audit — {today} {out['time']}", "", f"**Overall {overall}/100** · " +
          " · ".join(f"{k} {v}" for k, v in scores.items()), "", "## Failed checks"]
    md += [f"- {x}" for x in a.findings] or ["- none"]
    md += ["", "## All checks"] + [f"- {'✓' if c['ok'] else '✗'} [{c['cat']}] {c['name']}: {c['value']}" for c in a.checks]
    cal = a.metrics.get("calibration") or {}
    md += ["", f"## Self-graded picks: {cal.get('closed', 0)} closed, {cal.get('open', 0)} open, {a.metrics.get('logged_today', 0)} logged today"]
    for t, s in (cal.get("by_type") or {}).items():
        md.append(f"- {t}: won {s['won']}/{s['n']} vs predicted {s['pred'] * 100:.0f}% · Brier {s['brier']:.3f} · {s['clusters']} clusters · avg P/L {s['avg_pnl_pct']:+.1f}%")
    md += ["", "## Usage (14 days)"] + [f"- {k}: {v}" for k, v in list((a.metrics.get('usage_14d') or {}).items())[:20]]
    (HERE / f"{today}.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"overall {overall} {scores} failed {len(a.findings)} in {out['seconds']}s")
    for x in a.findings:
        print(" -", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
