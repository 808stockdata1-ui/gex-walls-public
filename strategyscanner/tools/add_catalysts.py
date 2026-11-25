"""Attach the daily catalyst review to that day's logged picks and re-render the report.

Usage:  python tools/add_catalysts.py output/catalysts_2026-10-19.json
File:   {"date": "2026-10-19", "reviewed_at": "2026-10-19 06:55 HST",
         "tickers": {"ONDS": {"risk": 1, "note": "S-3 shelf filed 10/17", "source": "https://www.sec.gov/..."}}}
risk 1 = a dated event before the pick's expiry, or a material filing in the last 7 days.
risk 0 = reviewed, nothing material.
"""
import json
import os
import pickle
import sqlite3
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from scanner import events as evmod  # noqa: E402
from scanner import report  # noqa: E402


def main(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    day = data["date"]
    data["tickers"] = {k.upper(): v for k, v in (data.get("tickers") or {}).items()}
    flags = data["tickers"]

    con = sqlite3.connect(os.path.join(HERE, "data", "scanner.db"))
    n = 0
    rows = con.execute("SELECT pick_id, ticker, features FROM picks WHERE date=?", (day,)).fetchall()
    for pick_id, ticker, feats in rows:
        info = flags.get(ticker.upper())
        if info is None:
            continue
        f = json.loads(feats)
        f["catalyst_risk"] = int(bool(info.get("risk")))
        f["catalyst_note"] = (info.get("note") or "")[:300]
        con.execute("UPDATE picks SET features=? WHERE pick_id=?", (json.dumps(f), pick_id))
        n += 1
    con.commit()
    con.close()
    print(f"Flags recorded on {n} logged picks for {day}")

    state_path = os.path.join(HERE, "output", "latest_state.pkl")
    if not os.path.exists(state_path):
        print("No saved report state; report not re-rendered")
        return
    with open(state_path, "rb") as f:
        st = pickle.load(f)
    if st["meta"]["run_date"] != day:
        print(f"Latest scan is {st['meta']['run_date']}, not {day}; report not re-rendered")
        return
    try:
        with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
            st["meta"]["refresh_trigger"] = json.load(f).get("refresh_trigger_id")
    except (OSError, ValueError):
        pass
    html = report.render_html(st["meta"], st["lists"], st["feats"], st["stats"], data,
                              events=evmod.load_week(os.path.join(HERE, "output")))
    for name in (f"{day}.html", "latest.html"):
        with open(os.path.join(HERE, "reports", name), "w", encoding="utf-8") as f:
            f.write(html)
    jp = os.path.join(HERE, "output", "latest.json")
    if os.path.exists(jp):
        with open(jp, "r", encoding="utf-8") as f:
            j = json.load(f)
        j["catalysts"] = data
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(j, f, indent=1, default=str)
    print("Report re-rendered: reports\\latest.html")


if __name__ == "__main__":
    main(sys.argv[1])
