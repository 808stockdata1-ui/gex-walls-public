#!/usr/bin/env python3
"""Reproduce every table in AUDIT-2026-11-25.md sections 1-4 from the shipped pick database.

    python analysis/audit_picks.py data/picks.csv.gz

No dependencies beyond the standard library. Error bars are clustered on (ticker, expiry) because
picks sharing a ticker and an expiration date share one market move and are not independent.
"""
import csv, gzip, json, math, sys
from collections import defaultdict

def load(path):
    op = gzip.open if str(path).endswith('.gz') else open
    with op(path, 'rt', encoding='utf-8', newline='') as fh:
        rows = list(csv.DictReader(fh))
    num = ("price", "credit", "capital", "roc", "pop_iv", "pop_rv", "pop_cons", "pop_cal", "p_max",
           "settle_price", "pnl", "pnl_pct", "last_mark", "dte", "win", "hit50")
    for r in rows:
        for k in num:
            v = r.get(k)
            r[k] = None if v in (None, "", "None") else float(v)
    return rows

pop = lambda r: r["pop_cal"] if r["pop_cal"] is not None else r["pop_cons"]

def clustered(rows, val):
    """(mean, standard error) with errors clustered on ticker+expiry."""
    g = defaultdict(list)
    for r in rows:
        v = val(r)
        if v is not None:
            g[(r["ticker"], r["expiry"])].append(v)
    n = sum(len(x) for x in g.values())
    if not n:
        return None, None, 0
    m = sum(v for x in g.values() for v in x) / n
    se = math.sqrt(sum((sum(x) - m * len(x)) ** 2 for x in g.values())) / n
    return m, se, len(g)

def nlegs(r):
    try:
        L = json.loads(r["legs"]) if str(r["legs"]).startswith("[") else []
        return max(1, sum(1 for l in L if l.get("cp") in ("C", "P")))
    except Exception:
        return 2

def ev_two_point(r, slip=0.0, comm=0.0):
    """EV at the scanner's own probability: win -> keep credit, lose -> pay max loss."""
    p = pop(r)
    if p is None or r["credit"] is None or not r["capital"]:
        return None
    n = nlegs(r)
    cr = abs(r["credit"]) * 100 - slip * 100 * n - comm * n * 2
    return p * cr - (1 - p) * (r["capital"] - cr)

def main(path):
    rows = load(path)
    S = [r for r in rows if r["status"] == "closed" and r["pnl"] is not None and pop(r) is not None]
    print(f"logged picks {len(rows)}   settled {len(S)}   "
          f"clusters {len({(r['ticker'], r['expiry']) for r in S})}   expiries {len({r['expiry'] for r in S})}")

    print("\n== 1. headline ==")
    w = sum(1 for r in S if r["win"])
    wins = [r["pnl"] for r in S if r["pnl"] > 0]
    loss = [r["pnl"] for r in S if r["pnl"] <= 0]
    m, se, k = clustered(S, lambda r: r["pnl_pct"])
    wr, wse, _ = clustered(S, lambda r: 1.0 if r["win"] else 0.0)
    naive = math.sqrt(wr * (1 - wr) / len(S))
    print(f"  win rate {wr*100:.1f}%  clustered 95% CI +/-{wse*196:.1f}pp   "
          f"(naive would be +/-{naive*196:.1f}pp, {wse/naive:.1f}x too tight)")
    print(f"  avg result {m*100:+.2f}% of capital  95% CI +/-{se*196:.2f}pp   total ${sum(r['pnl'] for r in S):+,.0f}")
    print(f"  avg win ${sum(wins)/len(wins):+,.0f}   avg loss ${sum(loss)/len(loss):+,.0f}   "
          f"break-even win rate {abs(sum(loss)/len(loss))/(abs(sum(loss)/len(loss))+sum(wins)/len(wins))*100:.0f}%")
    print(f"  predicted win rate {sum(pop(r) for r in S)/len(S)*100:.1f}%  ->  "
          f"overconfident by {(sum(pop(r) for r in S)/len(S)-wr)*100:.1f}pp")

    print("\n== 2. expected value of every logged pick, at the scanner's own probability ==")
    L = [r for r in rows if r["credit"] is not None and r["capital"]]
    for lab, sl, co in (("mid fills, no commission", 0.0, 0.0),
                        ("+$0.01/leg slip, $0.65/leg comm", 0.01, 0.65),
                        ("+$0.03/leg slip, $0.65/leg comm", 0.03, 0.65)):
        e = [ev_two_point(r, sl, co) for r in L]
        e = [x for x in e if x is not None]
        print(f"  {lab:<34} mean EV ${sum(e)/len(e):+7.2f}   EV<=0: {sum(1 for x in e if x<=0)/len(e)*100:5.1f}%")

    print("\n  EV quintiles vs what actually happened:")
    Q = sorted([r for r in S if ev_two_point(r) is not None], key=ev_two_point)
    q = len(Q) // 5
    for i in range(5):
        part = Q[i*q:(i+1)*q] if i < 4 else Q[4*q:]
        print(f"    quintile {i+1}: predicted ${sum(ev_two_point(r) for r in part)/len(part):+8.2f}   "
              f"realised ${sum(r['pnl'] for r in part)/len(part):+8.2f}   "
              f"win {sum(1 for r in part if r['win'])/len(part)*100:3.0f}%   n={len(part)}")

    print("\n== 3. per-expiry: the real unit of risk ==")
    g = defaultdict(list)
    for r in S:
        g[r["expiry"]].append(r)
    print(f"  {'expiry':<12}{'trades':>7}{'win%':>6}{'pred%':>7}{'P/L $':>12}{'capital':>13}")
    for e in sorted(g):
        x = g[e]
        print(f"  {e:<12}{len(x):>7}{sum(1 for r in x if r['win'])/len(x)*100:>5.0f}%"
              f"{sum(pop(r) for r in x)/len(x)*100:>6.0f}%{sum(r['pnl'] for r in x):>+12,.0f}"
              f"{sum(r['capital'] or 0 for r in x):>13,.0f}")
    tot = {e: sum(r["pnl"] for r in x) for e, x in g.items()}
    worst = min(tot, key=tot.get)
    print(f"  profitable expiries {sum(1 for v in tot.values() if v>0)}/{len(tot)}")
    print(f"  all but {worst}: ${sum(tot.values())-tot[worst]:+,.0f}   {worst} alone: ${tot[worst]:+,.0f}")
    cap = {e: sum(r["capital"] or 0 for r in x) for e, x in g.items()}
    med = sorted(cap.values())[len(cap)//2]
    put = sum(r["capital"] or 0 for r in g[worst] if r["strategy"] in ("PCS","CSP","IC"))
    call = sum(r["capital"] or 0 for r in g[worst] if r["strategy"] in ("CCS","CC"))
    print(f"  {worst}: ${cap[worst]:,.0f} at risk = {cap[worst]/med:.0f}x the median expiry; "
          f"{put/(put+call)*100:.0f}% of it pointing one way")

    print("\n== 4. was the volatility forecast right? (one move per ticker/expiry) ==")
    seen = {}
    for r in S:
        if not r["settle_price"] or not r["price"] or not r["dte"]:
            continue
        try:
            f = json.loads(r["features"] or "{}")
        except Exception:
            continue
        rv, iv = f.get("rv20"), f.get("iv30")
        if not rv:
            continue
        seen.setdefault((r["ticker"], r["expiry"]),
                        (math.log(r["settle_price"]/r["price"]), r["dte"]/365.0, rv, iv))
    V = list(seen.values())
    def sc(name, fc):
        z = [mv/(s*math.sqrt(T)) for mv, T, rv, iv in V if (s := fc(rv, iv)) and s > 0]
        if len(z) < 20:
            return
        print(f"  {name:<30} n={len(z):>4}  RMS {math.sqrt(sum(x*x for x in z)/len(z)):.3f}  "
              f"mean {sum(z)/len(z):+.3f}  |z|>2 {sum(1 for x in z if abs(x)>2)/len(z)*100:4.1f}%")
    print("  (RMS 1.00 = forecast matched the moves; above 1.00 = forecast too low)")
    sc("rv20 (what was used)", lambda rv, iv: rv)
    sc("iv30", lambda rv, iv: iv)
    sc("max(rv20, iv30)", lambda rv, iv: max(rv, iv or 0))
    sc("max(rv20, iv30) x 1.25", lambda rv, iv: max(rv, iv or 0) * 1.25)
    ir = sorted(iv/rv for mv, T, rv, iv in V if iv and rv)
    print(f"  iv30/rv20 median {ir[len(ir)//2]:.2f}  share above 1.0 {sum(1 for x in ir if x>1)/len(ir)*100:.0f}%")
    print("\n  regime scoreboard: realised move / what implied was paying for (>1.00 = selling lost)")
    be = defaultdict(list)
    for (tk, e), (mv, T, rv, iv) in seen.items():
        if iv:
            be[e].append(abs(mv)/(iv*math.sqrt(T)))
    for e in sorted(be):
        x = sorted(be[e])
        if len(x) >= 5:
            print(f"    {e}  n={len(x):>3}  median {x[len(x)//2]:.2f}")
    allz = sorted(v for x in be.values() for v in x)
    print(f"    WHOLE PERIOD median {allz[len(allz)//2]:.2f}")

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/picks.csv.gz")
