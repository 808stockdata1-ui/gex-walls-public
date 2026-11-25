"""Tests for spread_repair.py (run: .venv\\Scripts\\python.exe -m unittest discover -s tests).

The MRVL fixture is CBOE's delayed chain at the Oct 30 2026 close (MRVL $196.40); "today" is Nov 1 2026,
so the Nov 20 spread has 19 days left. Expected numbers were worked out by hand from the fixture quotes.
"""
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from spread_repair import project_earnings, rank, repair_plan  # noqa: E402

TODAY = date(2026, 11, 1)
SPOT = 196.40
TRADE = {"ticker": "MRVL", "exp": "2026-11-20", "short": 230.0, "long": 220.0, "credit": 2.9865, "qty": 1}


def mrvl_chain() -> pd.DataFrame:
    """Fixture rows in walls_app.load_chain's column layout."""
    raw = pd.read_csv(HERE / "fixtures" / "mrvl_2026-10-30.csv", dtype={"exp": str})
    return pd.DataFrame({
        "expiration": pd.to_datetime(raw["exp"], format="%y%m%d"),
        "type": raw["cp"], "strike": raw["k"].astype(float),
        "bid": raw["bid"], "ask": raw["ask"], "iv": raw["iv"], "delta": raw["delta"],
        "open_interest": raw["oi"].astype(float),
    })


def find(plan, kind, exp, short, long_=None, call_short=None):
    for r in plan["repairs"] + plan.get("below_floor", []):
        if (r["kind"] == kind and r["exp"] == exp and r["short"] == short
                and (long_ is None or r["long"] == long_)
                and (call_short is None or r.get("call_short") == call_short)):
            return r
    return None


class Baselines(unittest.TestCase):
    def setUp(self):
        self.plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)

    def test_close_prices_mid_and_natural(self):
        c = self.plan["close"]
        self.assertAlmostEqual(c["mid"], 8.05, places=4)
        self.assertAlmostEqual(c["nat"], 8.70, places=4)
        self.assertAlmostEqual(c["pl_mid"], -506.35, places=2)
        self.assertAlmostEqual(c["pl_nat"], -571.35, places=2)

    def test_hold_breakeven_max_loss_and_odds(self):
        h = self.plan["hold"]
        self.assertEqual(h["dte"], 19)
        self.assertAlmostEqual(h["be"], 227.0135, places=4)
        self.assertAlmostEqual(h["max_loss"], 701.35, places=2)
        self.assertAlmostEqual(h["pop"], 16.2568, places=2)
        self.assertAlmostEqual(h["p_max_loss"], 78.2247, places=2)

    def test_rejects_spread_not_in_chain(self):
        bad = {**TRADE, "short": 231.0}
        with self.assertRaises(ValueError):
            repair_plan(bad, mrvl_chain(), SPOT, TODAY)


class Rolls(unittest.TestCase):
    def setUp(self):
        self.plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)

    def test_roll_down_and_out_priced_as_one_order(self):
        r = find(self.plan, "roll_down_out", "2026-12-18", 220.0, 210.0)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["net_mid"], -1.525, places=4)
        self.assertAlmostEqual(r["net_nat"], -2.75, places=4)
        self.assertAlmostEqual(r["be"], 218.5385, places=4)
        self.assertAlmostEqual(r["max_loss"], 853.85, places=2)
        self.assertAlmostEqual(r["max_loss_change"], 152.50, places=2)
        self.assertAlmostEqual(r["pop"], 29.4971, places=2)
        self.assertAlmostEqual(r["p_max_loss"], 64.5667, places=2)
        self.assertEqual(r["dte"], 47)

    def test_roll_out_same_strikes_is_a_debit(self):
        r = find(self.plan, "roll_out", "2026-12-18", 230.0, 220.0)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["net_mid"], -0.55, places=4)
        self.assertAlmostEqual(r["be"], 227.5635, places=4)

    def test_roll_order_has_four_legs(self):
        r = find(self.plan, "roll_down_out", "2026-12-18", 220.0, 210.0)
        self.assertEqual([(l["side"], l["exp"], l["type"], l["strike"]) for l in r["legs"]], [
            ("BTC", "2026-11-20", "P", 230.0), ("STC", "2026-11-20", "P", 220.0),
            ("STO", "2026-12-18", "P", 220.0), ("BTO", "2026-12-18", "P", 210.0)])

    def test_every_repair_can_still_end_in_profit(self):
        self.assertTrue(self.plan["repairs"])
        for r in self.plan["repairs"]:
            self.assertGreater(r["total_credit"], 0, r)

    def test_expirations_limited_to_window(self):
        exps = {r["exp"] for r in self.plan["repairs"]}
        self.assertNotIn("2027-02-19", exps)          # 110 days out > 19 + 75
        self.assertIn("2027-01-15", exps)


class Widen(unittest.TestCase):
    def test_widen_only_offered_for_a_credit(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)
        r = find(plan, "roll_widen", "2026-12-18", 230.0, 210.0)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["net_mid"], 5.975, places=4)
        self.assertAlmostEqual(r["be"], 221.0385, places=4)
        self.assertAlmostEqual(r["max_loss"], 1103.85, places=2)
        self.assertAlmostEqual(r["margin"], 2000.0, places=2)
        self.assertAlmostEqual(r["pop"], 27.9582, places=2)
        for x in plan["repairs"]:
            if x["kind"] == "roll_widen":
                self.assertGreaterEqual(x["net_mid"], 0)

    def test_added_risk_cap_excludes_bigger_losses(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)
        self.assertIsNone(find(plan, "roll_widen", "2027-01-15", 230.0, 210.0))   # $1,171 > $701.35 x 1.6
        wide = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY, max_added_risk_pct=100)
        self.assertIsNotNone(find(wide, "roll_widen", "2027-01-15", 230.0, 210.0))


class CallSpread(unittest.TestCase):
    def test_add_call_spread_makes_an_iron_fly(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)
        r = find(plan, "add_call_spread", "2026-11-20", 230.0, 220.0, call_short=230.0)
        self.assertIsNotNone(r)
        self.assertEqual(r["call_long"], 240.0)
        self.assertAlmostEqual(r["net_mid"], 1.345, places=4)
        self.assertAlmostEqual(r["be"], 225.6685, places=4)
        self.assertAlmostEqual(r["be_high"], 234.3315, places=4)
        self.assertAlmostEqual(r["max_loss"], 566.85, places=2)
        self.assertAlmostEqual(r["margin"], 1000.0, places=2)
        self.assertAlmostEqual(r["pop"], 5.2517, places=2)
        self.assertEqual([(l["side"], l["type"], l["strike"]) for l in r["legs"]],
                         [("STO", "C", 230.0), ("BTO", "C", 240.0)])


class Filters(unittest.TestCase):
    def test_earnings_flagged_and_optionally_excluded(self):
        earn = {"date": "2026-12-02", "confirmed": True}
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY, earnings=earn)
        dec = find(plan, "roll_down_out", "2026-12-18", 220.0, 210.0)
        self.assertEqual(dec["earn"], "confirmed")
        fly = find(plan, "add_call_spread", "2026-11-20", 230.0, 220.0, call_short=230.0)
        self.assertIsNone(fly["earn"])
        clean = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY, earnings=earn, exclude_earnings=True)
        self.assertTrue(all(r["exp"] == "2026-11-20" for r in clean["repairs"]))

    def test_projected_earnings_flag_a_week_early(self):
        earn = {"date": "2026-12-22", "confirmed": False}      # projected: flag expirations from Dec 15 on
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY, earnings=earn)
        self.assertEqual(find(plan, "roll_down_out", "2026-12-18", 220.0, 210.0)["earn"], "projected")

    def test_illiquid_new_legs_are_skipped(self):
        df = mrvl_chain()
        self.assertIsNotNone(find(repair_plan(TRADE, df, SPOT, TODAY), "roll_down_out", "2026-12-18", 220.0, 210.0))
        thin = (df["expiration"] == pd.Timestamp("2026-12-18")) & (df["type"] == "P") & (df["strike"] == 210.0)
        df.loc[thin, "open_interest"] = 5
        plan = repair_plan(TRADE, df, SPOT, TODAY)
        self.assertIsNone(find(plan, "roll_down_out", "2026-12-18", 220.0, 210.0))
        self.assertIsNone(find(plan, "roll_widen", "2026-12-18", 230.0, 210.0))

    def test_put_wall_flag(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY, walls={"2026-12-18": 200.0})
        self.assertFalse(find(plan, "roll_down_out", "2026-12-18", 220.0, 210.0)["below_wall"])
        self.assertIsNone(find(plan, "roll_down_out", "2027-01-15", 220.0, 210.0)["below_wall"])

    def test_quantity_scales_dollars(self):
        plan = repair_plan({**TRADE, "qty": 2}, mrvl_chain(), SPOT, TODAY)
        self.assertAlmostEqual(plan["hold"]["max_loss"], 1402.70, places=2)
        self.assertAlmostEqual(plan["close"]["pl_mid"], -1012.70, places=2)
        self.assertAlmostEqual(find(plan, "roll_down_out", "2026-12-18", 220.0, 210.0)["max_loss"], 1707.70, places=2)


class ReturnFloor(unittest.TestCase):
    """Joe's rules: every pick returns at least 15% of max loss at max profit, and nothing under 10% at all."""

    def test_listed_repairs_clear_fifteen_percent(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)
        self.assertTrue(plan["repairs"])
        self.assertTrue(all(r["ret"] >= 0.15 for r in plan["repairs"]))
        self.assertIsNotNone(next((r for r in plan["repairs"]                  # 16.4% clears 15%
                                   if r["exp"] == "2027-01-15" and r["short"] == 220.0 and r["long"] == 210.0), None))

    def test_ten_to_fifteen_percent_shown_below_the_floor(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)
        below = {(r["exp"], r["short"]) for r in plan["below_floor"]}
        self.assertEqual(below, {("2026-12-18", 210.0), ("2027-01-15", 210.0)})   # 10.0% and 10.9%
        self.assertTrue(all(0.10 <= r["ret"] < 0.15 for r in plan["below_floor"]))

    def test_under_ten_percent_dropped_entirely(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)
        self.assertIsNone(find(plan, "roll_down_out", "2026-12-18", 200.0, 190.0))   # risks ~$986 to keep ~$14
        self.assertEqual(plan["dropped_under_min"], 1)

    def test_floors_are_adjustable(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY, min_ret=0.0, drop_ret=0.0)
        self.assertEqual(plan["below_floor"], [])
        self.assertIsNotNone(find(plan, "roll_down_out", "2026-12-18", 200.0, 190.0))


class EarningsProjection(unittest.TestCase):
    """No announced date: project the next report from the company's earnings 8-Ks (Item 2.02)."""
    MRVL_202 = ["2026-08-27", "2026-05-27", "2026-03-05", "2025-12-02", "2025-08-28", "2025-05-29"]

    def test_same_quarter_last_year_plus_52_weeks(self):
        self.assertEqual(project_earnings(self.MRVL_202, date(2026, 11, 1)), "2026-12-01")   # Dec 2 2025 + 364d

    def test_falls_back_to_last_report_plus_91_days(self):
        self.assertEqual(project_earnings(["2026-08-27"], date(2026, 11, 1)), "2026-11-26")

    def test_off_cycle_results_8k_is_ignored(self):
        # Nov 2 2026: preliminary revenue in the Lumentum deal 8-K (Item 2.02) is not the quarterly report
        self.assertEqual(project_earnings(self.MRVL_202 + ["2026-11-02"], date(2026, 11, 3)), "2026-12-01")

    def test_stale_history_projects_nothing(self):
        self.assertIsNone(project_earnings(["2025-01-10"], date(2026, 11, 1)))
        self.assertIsNone(project_earnings([], date(2026, 11, 1)))


class Ranking(unittest.TestCase):
    def test_pop_first_then_capital_then_return(self):
        a = {"id": "a", "pop": 30.2, "max_loss": 900, "ret": 0.20}
        b = {"id": "b", "pop": 30.6, "max_loss": 800, "ret": 0.10}   # same 1-pt POP bucket as a, less capital
        c = {"id": "c", "pop": 30.4, "max_loss": 800, "ret": 0.30}   # same bucket and capital as b, better return
        d = {"id": "d", "pop": 41.0, "max_loss": 2000, "ret": 0.05}  # higher POP wins outright
        self.assertEqual([x["id"] for x in rank([a, b, c, d])], ["d", "c", "b", "a"])

    def test_plan_repairs_come_back_ranked(self):
        plan = repair_plan(TRADE, mrvl_chain(), SPOT, TODAY)
        self.assertGreater(len(plan["repairs"]), 5)
        self.assertEqual([r["pop"] for r in plan["repairs"]], [r["pop"] for r in rank(plan["repairs"])])


if __name__ == "__main__":
    unittest.main()
