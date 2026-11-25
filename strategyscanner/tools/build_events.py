"""Rebuild the week-ahead calendar (output/events_week.json) and re-render today's report with it.

Usage:  python tools/build_events.py
Run by the pre-market catalyst review after it writes output/events_agent.json (BLS releases, White House
and Federal Register items, Fed speeches, Treasury refunding), each with a primary-source link:
{"generated": "2026-10-26 02:40 HST", "events": [{"date": "2026-10-30", "time_et": "8:30 AM",
  "title": "BLS: Employment Cost Index, Q3", "category": "bls", "impact": "high",
  "source": "https://www.bls.gov/schedule/2026/10_sched_list.htm"}]}
"""
import json
import os
import pickle
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from scanner import events as evmod  # noqa: E402
from scanner import report  # noqa: E402
from scanner.data import Fetcher  # noqa: E402
from scanner.util import today_et  # noqa: E402


def main():
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    out_dir = os.path.join(HERE, "output")
    fx = Fetcher(os.path.join(HERE, cfg["cache_dir"]), cfg["sec_user_agent"])
    week = evmod.build_week(fx, out_dir, today_et())
    n = sum(len(d["events"]) for d in week["days"])
    print(f"Week ahead {week['start']} to {week['end']}: {n} events; sources {week['status']}")
    st_path = os.path.join(out_dir, "latest_state.pkl")
    if not os.path.exists(st_path):
        return
    with open(st_path, "rb") as f:
        st = pickle.load(f)
    cat = None
    cp = os.path.join(out_dir, f"catalysts_{st['meta']['run_date']}.json")
    if os.path.exists(cp):
        with open(cp, "r", encoding="utf-8") as f:
            cat = json.load(f)
    st["meta"]["refresh_trigger"] = cfg.get("refresh_trigger_id")
    html = report.render_html(st["meta"], st["lists"], st["feats"], st["stats"], cat, events=week)
    for name in (f"{st['meta']['run_date']}.html", "latest.html"):
        with open(os.path.join(HERE, "reports", name), "w", encoding="utf-8") as f:
            f.write(html)
    print("Report re-rendered: reports\\latest.html")


if __name__ == "__main__":
    main()
