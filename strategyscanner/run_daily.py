"""Daily run: settle expired picks -> learn -> scan all timeframes -> mark open picks -> rank -> log -> report.

Usage:  python run_daily.py                                 (full universe: universe.txt + universe_auto.txt)
        python run_daily.py --tickers SPY,AAPL --no-log     (preview: grades nothing, logs nothing,
                                                             writes reports/preview.html only)
"""
import argparse
import csv
import datetime as dt
import json
import logging
import os
import pickle
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scanner import db as dbm  # noqa: E402
from scanner import events as evmod  # noqa: E402
from scanner import learn, ranker, report, strategies  # noqa: E402
from scanner.data import Fetcher  # noqa: E402
from scanner.indicators import ticker_features  # noqa: E402
from scanner.options import gex_profile, group_chain  # noqa: E402
from scanner.util import log, now_utc, today_et  # noqa: E402

SIGNAL_KEYS = ("rsi14", "above_sma50", "above_sma200", "iv_rv", "iv_rank", "iv30", "rv20", "spot_vs_flip",
               "net_gex_sign", "short_vol_ratio", "n8k_7d", "nform4_30d", "n13dg_90d", "ret5", "ret20",
               "dist_hi52", "vix", "spy_above_sma50")


def load_config():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("db_path", "cache_dir", "reports_dir", "output_dir", "logs_dir", "universe_file", "universe_auto_file"):
        if k in cfg:
            cfg[k] = os.path.join(HERE, cfg[k])
    return cfg


def load_universe(*paths):
    out, seen = [], set()
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                t = line.split("#")[0].strip().upper()
                if t and t not in seen:
                    seen.add(t)
                    out.append(t)
    return out


def setup_logging(logs_dir, today, preview):
    os.makedirs(logs_dir, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    name = f"preview_{today.isoformat()}.log" if preview else f"run_{today.isoformat()}.log"
    fh = logging.FileHandler(os.path.join(logs_dir, name), encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout is not None:  # pythonw (scheduled, windowless) has no console
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)


def market_context(fx):
    ctx = {"r": None, "vix": None, "spy": None}
    irx = fx.history("^IRX", "1mo", cache=False)
    if irx:
        ctx["r"] = irx[-1]["close"] / 100.0
    vix = fx.history("^VIX", "1mo", cache=False)
    if vix:
        ctx["vix"] = vix[-1]["close"]
    spy = fx.history("SPY", "1y", cache=False)
    if spy:
        ctx["spy"] = ticker_features(spy)
    return ctx


def chain_meta(chain):
    if not chain:
        return None
    return {"asof": chain.get("asof"), "last_trade_time": chain.get("last_trade_time"), "price": chain.get("price")}


def process_ticker(sym, fx, cfg, ctx, today, now, earnings, shortvol, ciks, iv_hist, models, keep_chain):
    """Fetch, compute signals, generate every candidate, filter, and keep only the best candidate per
    (timeframe, strategy) for this ticker. Full chains are kept only for tickers with open picks."""
    res = {"sym": sym, "f": None, "meta": None, "chain": None, "status": None, "best": [],
           "n_raw": 0, "n_passed": 0, "reject": {}, "loose": {}}
    bars = fx.history(sym, "1y")
    chain = fx.option_chain(sym)
    res["meta"] = chain_meta(chain)
    if keep_chain:
        res["chain"] = chain
    if not bars or len(bars) < 30 or not chain:
        res["status"] = "no price history" if not bars or len(bars) < 30 else "no option chain"
        return res
    live_date = None
    ltt = chain.get("last_trade_time")
    if ltt:
        try:
            live_date = dt.date.fromisoformat(ltt[:10])
        except ValueError:
            pass
    f = ticker_features(bars, chain["price"], live_date)
    S = chain["price"]
    f["price"] = S
    res["f"] = f
    if S < cfg["min_price"] or (f["avg_vol20"] or 0) < cfg["min_avg_volume"]:
        res["status"] = "fails price/volume filter"
        return res
    grouped = group_chain(chain["options"])
    f["iv30"] = chain["iv30"] or None
    f["iv_rv"] = (f["iv30"] / f["rv20"]) if f["iv30"] and f["rv20"] else None
    f["iv_rank"] = dbm.iv_rank_from_history(iv_hist.get(sym, []), f["iv30"])
    g = gex_profile(grouped, S, ctx["r"], now)
    f.update(g)
    f["spot_vs_flip"] = (S - g["gamma_flip"]) / S if g["gamma_flip"] else None
    f["net_gex_sign"] = (1 if g["net_gex"] > 0 else -1) if g["net_gex"] else 0
    f["short_vol_ratio"] = shortvol.get(sym)
    f["earnings_date"] = earnings.get(sym)
    cik = ciks.get(sym) or ciks.get(sym.replace(".", "-"))
    sec = fx.sec_filing_counts(cik, today) if cik else None
    f.update(sec or {"n8k_7d": None, "nform4_30d": None, "n13dg_90d": None, "last_8k": None,
                     "last_results_8k": None})
    f["earnings_est"] = False
    if f["earnings_date"] is None and f.get("last_results_8k"):
        # No confirmed date: project one quarter after the last earnings 8-K (Item 2.02) on EDGAR.
        est = dt.date.fromisoformat(f["last_results_8k"]) + dt.timedelta(days=91)
        if est < today:
            est = today + dt.timedelta(days=3)  # overdue: treat as imminent
        f["earnings_date"], f["earnings_est"] = est, True
    f["vix"] = ctx["vix"]
    f["spy_above_sma50"] = ctx["spy"]["above_sma50"] if ctx["spy"] else None

    rv_vals = [v for v in (f["rv20"], f["rv60"]) if v]
    tc = {"sym": sym, "S": S, "chain": grouped, "iv30": f["iv30"], "rv": max(rv_vals) if rv_vals else None,
          "r": ctx["r"], "now": now, "today": today, "earnings_date": f["earnings_date"]}
    cands = strategies.generate(tc, cfg)
    res["n_raw"] = len(cands)
    for c in cands:
        for k in SIGNAL_KEYS:
            c[k] = f.get(k)
        puts = [l for l in c["legs"] if l["cp"] == "P" and l["side"] == "sell"]
        calls = [l for l in c["legs"] if l["cp"] == "C" and l["side"] == "sell"]
        c["short_vs_putwall"] = (puts[0]["K"] - g["put_wall"]) / S if puts and g["put_wall"] else None
        c["short_vs_callwall"] = (calls[0]["K"] - g["call_wall"]) / S if calls and g["call_wall"] else None
    learn.apply_models(cands, models)
    best = {}
    loose_floor = (cfg.get("min_profit") or 0.0) * cfg.get("fast", {}).get("shortlist_profit_factor", 0.75)
    for c in cands:
        key = (c["timeframe"], c["strategy"])
        ok, why = ranker.mandatory_ok(c, cfg)
        # Shortlist score: also count near-misses on the profit floor, since live prices can push them over.
        if ok or (why == "min_profit" and ranker.mandatory_ok(c, cfg, profit_floor=loose_floor)[0]):
            sk = ranker.sort_key(c, cfg["pop_bucket"])
            if key not in res["loose"] or sk < res["loose"][key]:
                res["loose"][key] = sk
        if not ok:
            res["reject"][why] = res["reject"].get(why, 0) + 1
            continue
        res["n_passed"] += 1
        if key not in best or ranker.sort_key(c, cfg["pop_bucket"]) < ranker.sort_key(best[key], cfg["pop_bucket"]):
            best[key] = c
    res["best"] = list(best.values())
    return res


def build_shortlist(loose_all, n):
    """Round-robin over every (timeframe, strategy) ranking so each one keeps its best tickers."""
    groups = {}
    for key, sk, sym in loose_all:
        groups.setdefault(key, []).append((sk, sym))
    for g in groups.values():
        g.sort()
    out, seen, r = [], set(), 0
    while len(out) < n and any(r < len(g) for g in groups.values()):
        for key in sorted(groups):
            g = groups[key]
            if r < len(g) and g[r][1] not in seen:
                seen.add(g[r][1])
                out.append(g[r][1])
                if len(out) >= n:
                    break
        r += 1
    return out


def load_shortlist(path, today, max_age_days):
    try:
        with open(path, "r", encoding="utf-8") as f:
            sl = json.load(f)
        age = (today - dt.date.fromisoformat(sl["run_date"])).days
        if 0 <= age <= max_age_days and sl.get("tickers"):
            return sl
    except (OSError, ValueError, KeyError):
        pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", help="comma-separated override of the universe")
    ap.add_argument("--no-log", action="store_true",
                    help="preview: no grading, no learning, no logging; writes reports/preview.html")
    ap.add_argument("--fast", action="store_true",
                    help="live rescan of the shortlist written by the last full scan (a few minutes)")
    args = ap.parse_args()
    preview = bool(args.no_log)
    fast = bool(args.fast) and not args.tickers

    cfg = load_config()
    today = today_et()
    now = now_utc()
    setup_logging(cfg["logs_dir"], today, preview)
    for d in (os.path.dirname(cfg["db_path"]), cfg["cache_dir"], cfg["reports_dir"], cfg["output_dir"]):
        os.makedirs(d, exist_ok=True)
    t0 = time.time()
    shortlist_path = os.path.join(cfg["output_dir"], "shortlist.json")
    fcfg = cfg.get("fast", {})
    sl = None
    if args.tickers:
        universe = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    elif fast:
        sl = load_shortlist(shortlist_path, today, fcfg.get("max_age_days", 4))
        if sl:
            universe = list(sl["tickers"])
        else:
            log.warning("No recent shortlist (output/shortlist.json); running a full scan instead")
            fast = False
    if not args.tickers and not fast:
        universe = load_universe(cfg["universe_file"], cfg.get("universe_auto_file"))
    uset = set(universe)
    log.info("Run start %s, %d tickers%s%s", today, len(universe), " (fast rescan)" if fast else "",
             " (preview)" if preview else "")

    fx = Fetcher(cfg["cache_dir"], cfg["sec_user_agent"], cboe_rate=cfg.get("cboe_rate", 2.0),
                 cboe_hosts=cfg.get("cboe_hosts"))
    ctx = market_context(fx)
    if ctx["r"] is None:
        ctx["r"] = cfg["risk_free_fallback"]
    week = evmod.load_week(cfg["output_dir"])
    if not week or week.get("start") != today.isoformat() or not (fast or args.tickers):
        try:  # the full scan refreshes the week-ahead calendar; the live rescan reuses it
            week = evmod.build_week(fx, cfg["output_dir"], today)
        except Exception:
            log.warning("Week-ahead calendar failed:\n%s", traceback.format_exc())
    earnings = fx.earnings_calendar(today, 75)
    sv_date, shortvol = fx.short_volume(today)
    ciks = fx.sec_cik_map()

    con = dbm.connect(cfg["db_path"])
    # 1. Grade: settle expired picks, then refit the calibration models before scoring today's trades.
    n_resolved = 0
    if not preview and not fast:  # the fast rescan leaves grading and refitting to the full scan
        n_resolved = learn.resolve_expired(con, lambda t: fx.history(t, "1y"), today)
        con.commit()
        learn.fit_models(con, cfg["learning"]["min_train"], cfg["learning"]["prior_n"])
        con.commit()
    models = learn.latest_models(con)
    models["_cal"] = learn.choose_calibration(con)  # walk-forward test: calibrate only if it beats raw POP
    log.info("Calibration: %s (walk-forward Brier %s over %d dates)", models["_cal"]["method"],
             {k: (round(v, 4) if v is not None else None) for k, v in models["_cal"]["brier"].items()},
             models["_cal"]["dates_scored"])
    open_tickers = {r["ticker"] for r in con.execute("SELECT DISTINCT ticker FROM picks WHERE status='open'")}
    fetch_list = list(universe) if fast else universe + sorted(t for t in open_tickers if t not in uset)
    loose_all = []
    iv_hist = dbm.iv_history(con, today.isoformat())

    # 2. Scan.
    feats, chains, metas, best_all, skipped = {}, {}, {}, [], {}
    totals = {"raw": 0, "passed": 0, "reject": {}}

    def handle(r):
        sym = r["sym"]
        if r["meta"]:
            metas[sym] = r["meta"]
        if r["chain"]:
            chains[sym] = r["chain"]
        if r["f"]:
            feats[sym] = r["f"]
        if r["status"]:
            skipped[sym] = r["status"]
            return
        skipped.pop(sym, None)
        if sym in uset:
            best_all.extend(r["best"])
            loose_all.extend((k, sk, sym) for k, sk in r["loose"].items())
            totals["raw"] += r["n_raw"]
            totals["passed"] += r["n_passed"]
            for k, v in r["reject"].items():
                totals["reject"][k] = totals["reject"].get(k, 0) + v

    def run_one(s):
        return process_ticker(s, fx, cfg, ctx, today, now, earnings, shortvol, ciks, iv_hist, models,
                              s in open_tickers)

    with ThreadPoolExecutor(max_workers=cfg["workers"]) as ex:
        futs = {ex.submit(run_one, s): s for s in fetch_list}
        for fut in as_completed(futs):
            s = futs[fut]
            try:
                handle(fut.result())
            except Exception:
                log.error("Ticker %s failed:\n%s", s, traceback.format_exc())
                skipped[s] = "error"
    # Second pass, one at a time, for anything a data source throttled during the burst.
    retry = [s for s, why in skipped.items() if why in ("no price history", "no option chain", "error")]
    if retry and not fast:  # the live rescan skips the slow pass; a missing ticker just drops out
        log.info("Retrying %d tickers slowly", len(retry))
        for s in retry:
            time.sleep(0.5)
            try:
                handle(run_one(s))
            except Exception:
                log.error("Ticker %s failed on retry:\n%s", s, traceback.format_exc())
    log.info("Scanned %d tickers (%d skipped) in %.0fs, %d candidates built; rate-limit replies %s; "
             "Cboe pace settled at %.2f req/s", len(feats), len(skipped), time.time() - t0, totals["raw"],
             fx.n429, fx.cboe_rate_total)
    fx.save_pace()
    if not fast and not args.tickers:
        tick = build_shortlist(loose_all, fcfg.get("shortlist_size", 60))
        with open(shortlist_path, "w", encoding="utf-8") as f:
            json.dump({"run_date": today.isoformat(), "generated": now.isoformat(timespec="seconds"),
                       "n_scanned": len(feats), "tickers": tick}, f, indent=1)
        log.info("Shortlist for the live rescan: %d tickers", len(tick))
    if skipped:
        counts = {}
        for why in skipped.values():
            counts[why] = counts.get(why, 0) + 1
        log.info("Skipped by reason: %s", counts)
        hard = sorted(s for s, why in skipped.items() if why != "fails price/volume filter")
        if hard:
            log.info("No data for: %s", ", ".join(hard[:200]))

    quotes_last_trade = max((m.get("last_trade_time") or "" for m in metas.values()), default="").replace("T", " ")
    data_asof = max((m.get("asof") or "" for m in metas.values()), default="")
    market_open_today = quotes_last_trade[:10] == today.isoformat()
    no_log = preview or not market_open_today
    if no_log and not preview:
        log.info("Quotes are from %s, not today: picks not logged", quotes_last_trade[:10])

    # 3. Mark open picks against today's chains.
    n_marked = 0
    if not preview:
        n_marked = learn.mark_open_picks(con, chains, today)
        con.commit()

    # 4. Rank and log.
    logged, lists = ranker.select(best_all, cfg)
    log.info("Trades passing filters: %d (rejected %s); picks to log: %d", totals["passed"], totals["reject"], len(logged))
    n_new = 0
    if not preview:
        cur = con.execute("""INSERT INTO runs (ts, run_date, data_asof, n_tickers, n_candidates, n_passed,
                             n_logged, n_marked, n_resolved, notes) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                          (now.isoformat(timespec="seconds"), today.isoformat(), data_asof, len(feats),
                           totals["raw"], totals["passed"], 0 if no_log else len(logged), n_marked, n_resolved,
                           json.dumps({"skipped": len(skipped), "rejected": totals["reject"]})))
        run_id = cur.lastrowid
        if not no_log:
            for sym, f in feats.items():
                if sym in uset:
                    dbm.upsert_ticker_daily(con, today.isoformat(), sym, f)
            for c in logged:
                n_new += int(dbm.insert_pick(con, run_id, today.isoformat(), c))
        con.commit()
        log.info("New picks logged: %d", n_new)

    # 5. Report.
    stats = learn.summary(con)
    meta = {"run_date": today.isoformat(), "generated_utc": now.isoformat(timespec="seconds"),
            "data_asof": data_asof, "quotes_last_trade": quotes_last_trade, "risk_free": ctx["r"], "vix": ctx["vix"],
            "spy": ctx["spy"], "n_universe": len(universe), "n_tickers": len(feats), "n_candidates": totals["raw"],
            "n_passed": totals["passed"], "n_new_picks": n_new, "n_marked": n_marked, "n_resolved": n_resolved,
            "short_volume_date": sv_date.isoformat() if sv_date else None,
            "skipped": skipped, "seconds": round(time.time() - t0, 1), "test_run": preview,
            "market_closed": not market_open_today, "refresh_trigger": cfg.get("refresh_trigger_id"),
            "min_profit": cfg.get("min_profit"), "mode": "fast" if fast else "full",
            "full_n": sl.get("n_scanned") if sl else None, "full_generated": sl.get("generated") if sl else None}
    if preview:
        with open(os.path.join(cfg["reports_dir"], "preview.html"), "w", encoding="utf-8") as f:
            f.write(report.render_html(meta, lists, feats, stats, events=week))
        with open(os.path.join(cfg["output_dir"], "preview.json"), "w", encoding="utf-8") as f:
            json.dump(report.to_json(meta, lists, feats, stats), f, indent=1, default=str)
        log.info("Preview done in %.1fs. Report: reports/preview.html", time.time() - t0)
        return

    cat_path = os.path.join(cfg["output_dir"], f"catalysts_{today.isoformat()}.json")
    catalysts = None
    if os.path.exists(cat_path):
        with open(cat_path, "r", encoding="utf-8") as f:
            catalysts = json.load(f)
    with open(os.path.join(cfg["output_dir"], "latest_state.pkl"), "wb") as f:
        pickle.dump({"meta": meta, "lists": lists, "feats": {t: feats[t] for t in report.pick_tickers(lists)
                                                             if t in feats}, "stats": stats}, f)
    html = report.render_html(meta, lists, feats, stats, catalysts, events=week)
    with open(os.path.join(cfg["reports_dir"], f"{today.isoformat()}.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(cfg["reports_dir"], "latest.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with open(os.path.join(cfg["output_dir"], "latest.json"), "w", encoding="utf-8") as f:
        json.dump(report.to_json(meta, lists, feats, stats), f, indent=1, default=str)
    write_csv(os.path.join(cfg["output_dir"], f"picks_{today.isoformat()}.csv"), lists)
    report.notify_telegram(cfg["telegram"], meta, lists)
    log.info("Done in %.1fs. Report: reports/latest.html", time.time() - t0)


def write_csv(path, lists):
    cols = ["timeframe", "strategy", "ticker", "price", "rsi14", "expiry", "dte", "legs", "credit", "capital",
            "roc", "pop_cons", "pop_iv", "pop_rv", "pop_cal", "p50", "p_max", "be_lo", "be_hi", "iv_rv",
            "earn_in_window"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for tf, rep in lists.items():
            for c in rep["top"]:
                w.writerow([tf, c["strategy"], c["ticker"], round(c["S"], 2),
                            None if c.get("rsi14") is None else round(c["rsi14"], 1), c["expiry"], c["dte"],
                            report.legs_text(c), round(c["credit"], 2), round(c["capital"], 0), round(c["roc"], 4),
                            round(c["pop_cons"], 4), round(c["pop_iv"], 4), round(c["pop_rv"], 4),
                            c.get("pop_cal"), c.get("p50"), c.get("p_max"), c.get("be_lo"), c.get("be_hi"),
                            c.get("iv_rv"), c["earn_in_window"]])


if __name__ == "__main__":
    main()
