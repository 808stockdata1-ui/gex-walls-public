"""Tax-loss bounce: season calendar, per-stock stats and score. No network."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import walls_app as w  # noqa: E402


def test_calendar_2026_season():
    c = w.tlb_calendar(date(2026, 11, 12))
    assert (c["year"], c["phase"], c["opex"]) == (2026, "watch", "2026-12-18")
    assert c["call_exps"] == ["2027-02-19", "2027-03-19"]
    s = {x["key"]: (x["from"], x["to"]) for x in c["steps"]}
    assert s["buy"] == ("2026-12-14", "2026-12-23")
    assert s["exit"] == ("2027-01-04", "2027-01-20")


def test_calendar_phases_have_no_gaps():
    c = w.tlb_calendar(date(2026, 12, 1))
    for a, b in zip(c["steps"], c["steps"][1:]):
        assert date.fromisoformat(b["from"]) - date.fromisoformat(a["to"]) == timedelta(days=1)
    seen = [w.tlb_calendar(date.fromisoformat(d))["phase"] for d in
            ("2026-11-20", "2026-12-14", "2026-12-24", "2027-01-04", "2027-01-21", "2027-03-01", "2027-10-01")]
    assert seen == ["puts", "buy", "hold", "exit", "done", "off", "watch"]
    assert w.tlb_calendar(date(2027, 1, 10))["year"] == 2026


def _bars(closes, start=date(2025, 6, 2)):
    rows, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        rows.append({"date": d.isoformat(), "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1_000_000})
        d += timedelta(days=1)
    return pd.DataFrame(rows)


def test_stats_loser_at_its_low():
    h = _bars([20.0] * 150 + [20 - 0.05 * i for i in range(1, 201)])      # slides from 20 to 10
    st = w._tlb_stats(h, 2026)
    assert st and -51 < st["ytd"] < -45 and st["above_low"] < 2 and st["state"] == "Still falling"
    assert not st["split"] and st["rsi"] is not None and st["rsi"] < 30


def test_stats_flags_split_and_short_history():
    closes = [20.0] * 200 + [5.0] * 150
    assert w._tlb_stats(_bars(closes), 2026)["split"]
    assert w._tlb_stats(_bars([10.0] * 50), 2026) is None


def test_score_penalises_weak_filings():
    base = {"ytd": -50, "above_low": 2, "rsi": 28, "cap": 1e9, "si_pct": 22, "state": "Basing"}
    good, _ = w._tlb_score({**base, "grade": "A", "dilution": False})
    bad, _ = w._tlb_score({**base, "grade": "F", "dilution": True})
    assert good - bad == 35 and 0 <= bad < good <= 100
