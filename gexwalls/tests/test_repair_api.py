"""Integration tests for the Repair tab's backend (walls_app.repair_data). Needs internet: CBOE, Nasdaq, SEC.

Run: .venv\\Scripts\\python.exe -m unittest tests.test_repair_api
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import walls_app as w  # noqa: E402


def live_spread():
    """A real put spread from today's MRVL chain: first expiration 14-60 days out, the strike at/above spot
    and the next one below it."""
    spot, df, _ = w.chain_cached("MRVL")
    puts = df[(df["type"] == "P") & (df["dte"].between(14, 60))]
    exp = sorted(puts["expiration"].unique())[0]
    ks = sorted(puts[puts["expiration"] == exp]["strike"].unique())
    short = min(k for k in ks if k >= spot)
    long_ = max(k for k in ks if k < short)
    sub = puts[puts["expiration"] == exp].set_index("strike")
    mid = (sub.loc[short, "bid"] + sub.loc[short, "ask"]) / 2 - (sub.loc[long_, "bid"] + sub.loc[long_, "ask"]) / 2
    return {"t": ["MRVL"], "exp": [str(exp)[:10]], "short": [str(short)], "long": [str(long_)],
            "credit": [f"{min(1.0, (short - long_) / 2):.2f}"], "qty": ["1"]}, mid


class RepairApi(unittest.TestCase):
    def test_lists_my_trades_without_a_spread(self):
        out = w.repair_data({})
        self.assertIsInstance(out["trades"], list)

    def test_unknown_trade_id_is_an_error(self):
        with self.assertRaises(ValueError):
            w.repair_data({"id": ["not-a-trade"]})

    def test_bad_strikes_are_an_error(self):
        q, _ = live_spread()
        with self.assertRaises(ValueError):
            w.repair_data({**q, "long": q["short"]})

    def test_typed_spread_is_priced_from_the_live_chain(self):
        q, mid = live_spread()
        out = w.repair_data(q)
        self.assertEqual(out["ticker"], "MRVL")
        self.assertAlmostEqual(out["plan"]["close"]["mid"], max(0.0, mid), places=6)
        self.assertTrue(out["rsi"] is None or 0 <= out["rsi"] <= 100)
        self.assertTrue(out["earnings"] is None or len(out["earnings"]["date"]) == 10)
        for r in out["plan"]["repairs"] + out["plan"]["below_floor"]:
            self.assertTrue(r["legs"])

    def test_query_settings_reach_the_engine(self):
        q, _ = live_spread()
        out = w.repair_data({**q, "risk": ["100"], "days": ["40"], "floor": ["0"], "noearn": ["1"]})
        p = out["plan"]["params"]
        self.assertEqual((p["max_added_risk_pct"], p["max_extra_days"], p["min_ret"], p["exclude_earnings"]),
                         (100.0, 40, 0.0, True))

    def test_nonsense_settings_fall_back_to_defaults(self):
        q, _ = live_spread()
        p = w.repair_data({**q, "risk": ["nan"], "floor": ["abc"]})["plan"]["params"]
        self.assertEqual((p["max_added_risk_pct"], p["min_ret"], p["drop_ret"]), (60.0, 0.15, 0.10))


if __name__ == "__main__":
    unittest.main()
