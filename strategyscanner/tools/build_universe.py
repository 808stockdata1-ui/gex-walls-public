"""Weekly universe refresh: every optionable US stock/ETF with a liquid option chain.

1. CBOE symbol directory  -> every symbol with listed options at Cboe
2. Nasdaq stock + ETF screener -> last price (and volume for stocks); drops indexes and non-US listings
3. Pre-filter: price >= min_price; stocks need day volume >= universe.min_day_volume (Nasdaq screener);
   ETFs need FINRA reported volume >= universe.min_etf_finra_volume on the latest daily file
4. CBOE delayed chain for each survivor -> keep it only if, within universe.max_dte days and
   +/-25% of spot, enough strikes have real open interest and a tight market
Writes universe_auto.txt (used by the daily scan together with universe.txt) and
output/universe_stats.csv (why each symbol was kept or dropped).

Usage:  python tools/build_universe.py
"""
import csv
import datetime as dt
import io
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from scanner.data import Fetcher  # noqa: E402
from scanner.options import liquid  # noqa: E402
from scanner.util import safe_float, today_et  # noqa: E402

CBOE_DIR = "https://www.cboe.com/us/options/symboldir/equity_index_options/?download=csv"
NASDAQ_STOCKS = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true"
NASDAQ_ETFS = "https://api.nasdaq.com/api/screener/etf?tableonly=true&limit=10000&offset=0&download=true"
NASDAQ_HDR = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
              "Referer": "https://www.nasdaq.com/"}


def money(s):
    return safe_float(str(s or "").replace("$", "").replace(",", ""))


def load_cfg():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def optionable_symbols(fx):
    txt = fx._get(CBOE_DIR, as_json=False)
    if not txt:
        raise SystemExit("CBOE symbol directory unavailable")
    out = set()
    for row in csv.reader(io.StringIO(txt)):
        if len(row) >= 2 and row[1].strip() and row[1].strip() != "Stock Symbol":
            out.add(row[1].strip().upper())
    return out


def screener(fx):
    """symbol -> (kind, price, day_volume or None)"""
    out = {}
    d = fx._get(NASDAQ_STOCKS, headers=NASDAQ_HDR)
    for r in ((d or {}).get("data") or {}).get("rows") or []:
        s = (r.get("symbol") or "").strip().upper().replace("/", ".")
        if s:
            out[s] = ("stock", money(r.get("lastsale")), safe_float(r.get("volume")))
    d = fx._get(NASDAQ_ETFS, headers=NASDAQ_HDR)
    data = (d or {}).get("data") or {}
    rows = data.get("rows") or (data.get("data") or {}).get("rows") or []
    for r in rows:
        s = (r.get("symbol") or "").strip().upper()
        if s and s not in out:
            out[s] = ("etf", money(r.get("lastSalePrice")), None)
    return out


def finra_total_volume(fx, ref):
    """Latest FINRA daily file: symbol -> total reported share volume (used to pre-screen ETFs)."""
    for i in range(7):
        day = ref - dt.timedelta(days=i)
        if day.weekday() >= 5:
            continue
        txt = fx._get(f"https://cdn.finra.org/equity/regsho/daily/CNMSshvol{day.strftime('%Y%m%d')}.txt",
                      as_json=False, retries=2)
        if txt and "|" in txt:
            out = {}
            for line in txt.splitlines()[1:]:
                p = line.split("|")
                if len(p) >= 5:
                    out[p[1].upper()] = safe_float(p[4], 0.0)
            return day, out
    return None, {}


def chain_liquidity(fx, sym, ucfg, lcfg, today):
    ch = fx.option_chain(sym)
    if not ch:
        return {"reason": "no chain"}
    S = ch["price"]
    n_liquid, oi_total, exps = 0, 0.0, set()
    for o in ch["options"]:
        dte = (o["exp"] - today).days
        if dte < 0 or dte > ucfg["max_dte"]:
            continue
        oi_total += o["oi"]
        if abs(o["K"] / S - 1.0) <= 0.25 and liquid(o, lcfg, True):
            n_liquid += 1
            exps.add(o["exp"])
    ok = n_liquid >= ucfg["min_liquid_strikes"] and oi_total >= ucfg["min_total_oi"]
    return {"price": S, "liquid_strikes": n_liquid, "oi_total": int(oi_total), "expiries": len(exps),
            "reason": "" if ok else "thin chain"}


def main():
    t0 = time.time()
    cfg = load_cfg()
    ucfg = cfg["universe"]
    fx = Fetcher(os.path.join(HERE, cfg["cache_dir"]), cfg["sec_user_agent"], cboe_rate=cfg.get("cboe_rate", 2.0),
                 cboe_hosts=cfg.get("cboe_hosts"))
    today = today_et()
    opt = optionable_symbols(fx)
    scr = screener(fx)
    if len(scr) < 3000:
        raise SystemExit(f"Nasdaq screener returned only {len(scr)} rows; keeping the previous universe")
    print(f"Optionable at Cboe: {len(opt)}; Nasdaq screener rows: {len(scr)}", flush=True)
    finra_day, finra_vol = finra_total_volume(fx, today)

    stats, cands = {}, []
    for s in sorted(opt):
        if s not in scr:
            stats[s] = {"reason": "not a US-listed stock/ETF (index or other)"}
            continue
        kind, px, vol = scr[s]
        if not px or px < cfg["min_price"]:
            stats[s] = {"kind": kind, "price": px, "reason": "price below minimum"}
        elif kind == "stock" and (vol or 0) < ucfg["min_day_volume"]:
            stats[s] = {"kind": kind, "price": px, "day_volume": vol, "reason": "low share volume"}
        elif kind == "etf" and finra_vol and finra_vol.get(s, 0) < ucfg["min_etf_finra_volume"]:
            stats[s] = {"kind": kind, "price": px, "day_volume": finra_vol.get(s, 0), "reason": "low share volume"}
        else:
            cands.append(s)
            stats[s] = {"kind": kind, "price": px, "day_volume": vol}
    est = len(cands) / (cfg.get("cboe_rate", 2.0) * len(fx.cboe_hosts)) / 60
    print(f"Chain checks to run: {len(cands)} (about {est:.0f} min at the Cboe rate limit)", flush=True)

    with ThreadPoolExecutor(max_workers=ucfg.get("workers", 8)) as ex:
        results = ex.map(lambda x: chain_liquidity(fx, x, ucfg, cfg["liquidity"], today), cands)
        for i, (s, res) in enumerate(zip(cands, results), 1):
            stats[s].update(res)
            if i % 250 == 0:
                print(f"  {i}/{len(cands)} checked, {time.time() - t0:.0f}s, rate-limit replies {fx.n429}, "
                      f"Cboe pace {fx.cboe_rate_total:.2f} req/s", flush=True)

    fx.save_pace()
    keep = sorted(s for s in cands if not stats[s].get("reason"))
    if len(keep) < 200:
        raise SystemExit(f"Only {len(keep)} symbols passed; something is wrong, keeping the previous universe")
    with open(os.path.join(HERE, "universe_auto.txt"), "w", encoding="utf-8") as f:
        f.write(f"# Auto-generated {dt.datetime.now().isoformat(timespec='minutes')} by tools/build_universe.py\n")
        f.write(f"# {len(keep)} optionable stocks/ETFs with liquid chains. Edit universe.txt instead; this file is overwritten weekly.\n")
        f.write("\n".join(keep) + "\n")
    os.makedirs(os.path.join(HERE, "output"), exist_ok=True)
    cols = ["symbol", "kept", "kind", "price", "day_volume", "liquid_strikes", "oi_total", "expiries", "reason"]
    with open(os.path.join(HERE, "output", "universe_stats.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for s in sorted(stats):
            r = stats[s]
            w.writerow([s, int(s in keep)] + [r.get(c) for c in cols[2:]])
    reasons = {}
    for r in stats.values():
        reasons[r.get("reason") or "kept"] = reasons.get(r.get("reason") or "kept", 0) + 1
    print(f"Kept {len(keep)} symbols in {time.time() - t0:.0f}s. Breakdown: {reasons}")


if __name__ == "__main__":
    main()
