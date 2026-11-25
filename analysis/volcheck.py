"""Is the rv20 volatility forecast systematically too low, or was Oct-Nov 2026 just a bad patch?
For every cached daily history: trailing 20-day realized vol vs the NEXT 20 trading days' realized
vol, over as much history as the cache holds. Prints ratios only."""
import json, math, glob, os, statistics as st
from collections import defaultdict
import sys
D = sys.argv[1] if len(sys.argv) > 1 else 'hist_cache'
files = glob.glob(os.path.join(D, '*.json'))
if not files:
    sys.exit(f"no *.json daily-history files in {D!r}; pass the directory as an argument")
print(f"history files: {len(files)}")
def rv(closes):
    if len(closes) < 3: return None
    lr = [math.log(closes[i+1]/closes[i]) for i in range(len(closes)-1) if closes[i] > 0 and closes[i+1] > 0]
    if len(lr) < 3: return None
    m = sum(lr)/len(lr)
    return math.sqrt(sum((x-m)**2 for x in lr)/(len(lr)-1)) * math.sqrt(252)
rows = []
by_month = defaultdict(list)
for fp in files:
    try:
        d = json.load(open(fp, encoding='utf-8'))
    except Exception:
        continue
    bars = d if isinstance(d, list) else (d.get('data') or [])
    if not isinstance(bars, list) or len(bars) < 120: continue
    try:
        bars = [b for b in bars if b.get('close')]
        bars.sort(key=lambda b: str(b['date']))
        cl = [float(b['close']) for b in bars]; dt = [str(b['date'])[:10] for b in bars]
    except Exception:
        continue
    for i in range(21, len(cl)-21, 5):           # every 5th day, non-overlapping enough
        past, fut = rv(cl[i-21:i+1]), rv(cl[i:i+21])
        if not past or not fut or past < 0.05: continue
        rows.append(fut/past); by_month[dt[i][:7]].append(fut/past)
n = len(rows)
rows.sort()
print(f"\nobservations: {n:,} across {len(files)} tickers")
print(f"forward 20d vol / trailing 20d vol:")
print(f"  median {rows[n//2]:.3f}   mean {sum(rows)/n:.3f}")
print(f"  RMS-style (sqrt of mean of squares) {math.sqrt(sum(x*x for x in rows)/n):.3f}")
print(f"  25th {rows[n//4]:.3f}  75th {rows[3*n//4]:.3f}  90th {rows[int(n*0.9)]:.3f}  99th {rows[int(n*0.99)]:.3f}")
print(f"  share above 1.0: {sum(1 for x in rows if x>1)/n*100:.0f}%   above 1.5: {sum(1 for x in rows if x>1.5)/n*100:.0f}%")
print("\nby month (median ratio, n):")
for m in sorted(by_month):
    L = sorted(by_month[m])
    if len(L) < 30: continue
    print(f"  {m}  median {L[len(L)//2]:.2f}  mean {sum(L)/len(L):.2f}  n={len(L)}")
