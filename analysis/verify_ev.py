#!/usr/bin/env python3
"""Checks on scanner/ev.py that it cannot pass by accident: put-call parity, Monte Carlo, and the
identity that a fairly-priced position valued at its own volatility must have EV exactly zero.

    python analysis/verify_ev.py          # run from the repo root
"""
import math, os, random, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategyscanner"))
try:
    from scanner.ev import exp_call, exp_put, position_ev
except ImportError:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from strategyscanner.scanner.ev import exp_call, exp_put, position_ev

fails = []
def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}  {detail}")
    if not ok:
        fails.append(name)

S, T = 100.0, 0.25
print("== put-call parity on expected intrinsics: E[call] - E[put] = F - K ==")
for K in (70, 100, 130):
    for sig in (0.15, 0.5, 1.2):
        lhs = exp_call(S, K, sig, T) - exp_put(S, K, sig, T)
        check(f"K={K} sig={sig}", abs(lhs - (S - K)) < 1e-9, f"{lhs:+.9f} vs {S-K:+.9f}")

print("\n== Monte Carlo (200k paths, zero drift) ==")
random.seed(11)
N = 200_000
for K, sig in ((88, 0.35), (105, 0.5), (130, 0.9)):
    v = sig * math.sqrt(T)
    ap = ac = 0.0
    for _ in range(N):
        ST = S * math.exp(-0.5 * v * v + v * random.gauss(0, 1))
        ap += max(K - ST, 0.0)
        ac += max(ST - K, 0.0)
    mp, mc = ap / N, ac / N
    fp, fc = exp_put(S, K, sig, T), exp_call(S, K, sig, T)
    check(f"put  K={K} sig={sig}", abs(mp - fp) / max(fp, 1e-9) < 0.02, f"MC {mp:.4f} vs {fp:.4f}")
    check(f"call K={K} sig={sig}", abs(mc - fc) / max(fc, 1e-9) < 0.02, f"MC {mc:.4f} vs {fc:.4f}")

print("\n== a fairly-priced position valued at its own volatility has EV exactly 0 ==")
sig = 0.35
pcs = [{"cp": "P", "K": 90.0, "side": "sell"}, {"cp": "P", "K": 85.0, "side": "buy"}]
fair = exp_put(S, 90, sig, T) - exp_put(S, 85, sig, T)
ev = position_ev(pcs, fair, S, T, lambda l: sig)
check("put credit spread", abs(ev) < 1e-9, f"EV {ev:+.10f} at fair credit {fair:.4f}")

ic = [{"cp": "P", "K": 90.0, "side": "sell"}, {"cp": "P", "K": 85.0, "side": "buy"},
      {"cp": "C", "K": 110.0, "side": "sell"}, {"cp": "C", "K": 115.0, "side": "buy"}]
fair_ic = (exp_put(S, 90, sig, T) - exp_put(S, 85, sig, T)
           + exp_call(S, 110, sig, T) - exp_call(S, 115, sig, T))
ev = position_ev(ic, fair_ic, S, T, lambda l: sig)
check("iron condor", abs(ev) < 1e-9, f"EV {ev:+.10f} at fair credit {fair_ic:.4f}")

lc = [{"cp": "C", "K": 60.0, "side": "buy"}]
fair_lc = exp_call(S, 60, 0.30, 2.0)
ev = position_ev(lc, -fair_lc, S, 2.0, lambda l: 0.30)
check("2y LEAPS call (no drift edge)", abs(ev) < 1e-9, f"EV {ev:+.10f} at fair debit {fair_lc:.3f}")

print("\n== EV moves the right way with the volatility forecast ==")
credit = exp_put(S, 90, 0.35, T) - exp_put(S, 85, 0.35, T)
prev = None
for rv in (0.20, 0.28, 0.35, 0.45, 0.60):
    ev = position_ev(pcs, credit, S, T, lambda l: rv)
    print(f"    sold at iv 0.35, realised vol {rv:.2f} -> EV {ev*100:+8.2f} $/contract")
    if prev is not None:
        check(f"EV falls as vol rises ({rv})", ev < prev, "")
    prev = ev
check("positive when realised below implied", position_ev(pcs, credit, S, T, lambda l: 0.25) > 0)
check("negative when realised above implied", position_ev(pcs, credit, S, T, lambda l: 0.45) < 0)

print(f"\n{'ALL CHECKS PASSED' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
