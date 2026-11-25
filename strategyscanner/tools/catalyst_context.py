"""Primary-source context for the daily catalyst review.

For every ticker in today's top picks: price, RSI(14), its picks and latest expiry, the earnings date the
scanner used, and recent SEC EDGAR filings (forms that can move a stock) with direct links.
Usage:  python tools/catalyst_context.py      -> prints JSON, writes output/catalyst_context_<date>.json
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from scanner.data import SEC_SUBMISSIONS_URL, Fetcher  # noqa: E402

ITEM_NAMES = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy",
    "2.01": "acquisition/disposition completed", "2.02": "results of operations", "2.03": "new debt",
    "2.05": "restructuring", "2.06": "impairment", "3.01": "delisting notice", "3.02": "unregistered share sale",
    "3.03": "shareholder rights change", "4.01": "auditor change", "4.02": "financials non-reliance",
    "5.01": "change in control", "5.02": "officer/director change", "5.03": "bylaws change",
    "5.07": "shareholder vote", "7.01": "Reg FD disclosure", "8.01": "other events", "9.01": "exhibits",
}
MOVING_FORMS = ("8-K", "6-K", "S-1", "S-3", "F-3", "424B", "SC 13D", "SC TO", "SC 14D9", "425", "DEF 14A",
                "PRE 14A", "DEFM14A", "10-Q", "10-K", "20-F", "25-NSE", "NT 10")
COUNT_FORMS = ("4", "4/A", "144", "SC 13G", "SC 13G/A")


def recent_filings(fx, cik, ref, days=14):
    d = fx._get(SEC_SUBMISSIONS_URL.format(cik=cik), headers={"User-Agent": fx.sec_ua}, retries=2, limiter="sec")
    if not d:
        return {"error": "EDGAR unavailable"}
    rec = d["filings"]["recent"]
    out, counts = [], {}
    for i, form in enumerate(rec["form"]):
        try:
            fd = dt.date.fromisoformat(rec["filingDate"][i])
        except ValueError:
            continue
        if (ref - fd).days > days:
            break
        fu = form.upper()
        if fu in COUNT_FORMS:
            counts[fu] = counts.get(fu, 0) + 1
            continue
        if not fu.startswith(MOVING_FORMS):
            continue
        acc = rec["accessionNumber"][i].replace("-", "")
        items = [x.strip() for x in (rec.get("items", [""] * len(rec["form"]))[i] or "").split(",") if x.strip()]
        out.append({
            "form": form, "filed": fd.isoformat(),
            "items": [f"{x} {ITEM_NAMES.get(x, '')}".strip() for x in items if x != "9.01"],
            "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{rec['primaryDocument'][i]}",
        })
    return {"filings": out[:12], "counts_14d": counts, "name": d.get("name")}


def main():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    with open(os.path.join(HERE, "output", "latest.json"), "r", encoding="utf-8") as f:
        j = json.load(f)
    run_date = dt.date.fromisoformat(j["meta"]["run_date"])
    tick = {}
    for tf, lst in j["top"].items():
        for c in lst:
            t = tick.setdefault(c["ticker"], {"price": c["price"], "rsi14": c.get("rsi14"), "picks": [],
                                              "latest_expiry": c["expiry"]})
            t["picks"].append(f'{tf}: {c["strategy_name"]} {c["legs"]} exp {c["expiry"]}')
            t["latest_expiry"] = max(t["latest_expiry"], c["expiry"])
    fx = Fetcher(os.path.join(HERE, cfg["cache_dir"]), cfg["sec_user_agent"])
    ciks = fx.sec_cik_map()
    for t, info in tick.items():
        info["rsi14"] = None if info["rsi14"] is None else round(info["rsi14"], 1)
        info["earnings_date_used"] = (j.get("tickers", {}).get(t) or {}).get("earnings_date")
        cik = ciks.get(t)
        if cik:
            info["sec"] = recent_filings(fx, cik, run_date)
        else:
            info["sec"] = {"note": "No SEC registrant for this symbol (ETF/fund or foreign issuer)"}
    out = {"run_date": run_date.isoformat(), "market_closed": j["meta"].get("market_closed"),
           "quotes_last_trade": j["meta"].get("quotes_last_trade"), "tickers": tick}
    path = os.path.join(HERE, "output", f"catalyst_context_{run_date.isoformat()}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
