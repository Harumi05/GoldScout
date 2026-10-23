"""Synthetic-data tests for research/quant/barriers.py."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.quant.barriers import (  # noqa: E402
    COST_SCENARIOS,
    CostScenario,
    M1Path,
    stop_distance,
    triple_barrier,
    vol_target_multiplier,
)

T0 = 1_700_000_000_000
M = 60_000
ZERO = COST_SCENARIOS["ZERO"]
BASE = COST_SCENARIOS["BASE"]


def make_path(ohlc, start=T0):
    bars = []
    for i, (o, h, l, c) in enumerate(ohlc):
        ts = start + i * M
        bars.append({"timeframe": "M1", "timestamp": ts, "close_timestamp": ts + M,
                     "open": o, "high": h, "low": l, "close": c,
                     "avg_spread": 0.3, "max_spread": 0.3})
    return M1Path(bars)


def flat(n, p=100.0):
    return [(p, p + 0.1, p - 0.1, p)] * n


def run(path, **kw):
    args = dict(signal_time_ms=T0, direction=1, stop_dist=1.0, tp_r=2.0,
                max_hold_ms=10 * M, cost=ZERO)
    args.update(kw)
    return triple_barrier(path, **args)


class TripleBarrierTests(unittest.TestCase):
    def test_tp_hit_long(self):
        path = make_path(flat(3) + [(100.0, 102.5, 99.9, 102.2)] + flat(10, 102.2))
        r = run(path)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["exit_reason"], "TP")
        self.assertAlmostEqual(r["r_multiple"], 2.0)
        self.assertEqual(r["bars_held"], 4)
        self.assertFalse(r["ambiguous_bar"])
        self.assertTrue(r["observer_only"])
        self.assertEqual(r["score_effect"], 0)

    def test_sl_hit_short(self):
        path = make_path(flat(2) + [(100.0, 101.2, 99.95, 101.0)] + flat(10, 101.0))
        r = run(path, direction=-1)
        self.assertEqual(r["exit_reason"], "SL")
        self.assertAlmostEqual(r["r_multiple"], -1.0)
        self.assertAlmostEqual(r["exit_price"], 101.0)

    def test_both_touched_same_bar_is_sl_and_ambiguous(self):
        path = make_path(flat(1) + [(100.0, 102.5, 98.5, 100.0)] + flat(10))
        r = run(path)
        self.assertEqual(r["exit_reason"], "SL")
        self.assertTrue(r["ambiguous_bar"])
        self.assertAlmostEqual(r["r_multiple"], -1.0)

    def test_gap_through_sl_fills_at_open(self):
        path = make_path(flat(2) + [(98.0, 98.2, 97.5, 98.0)] + flat(10, 98.0))
        r = run(path)
        self.assertEqual(r["exit_reason"], "SL")
        self.assertAlmostEqual(r["exit_price"], 98.0)
        self.assertAlmostEqual(r["r_multiple"], -2.0)  # worse than the level
        self.assertFalse(r["ambiguous_bar"])

    def test_gap_through_sl_short_fills_at_open(self):
        path = make_path(flat(2) + [(102.0, 102.5, 101.8, 102.0)] + flat(10, 102.0))
        r = run(path, direction=-1)
        self.assertEqual(r["exit_reason"], "SL")
        self.assertAlmostEqual(r["r_multiple"], -2.0)

    def test_time_exit(self):
        path = make_path(flat(4) + [(100.0, 100.6, 99.6, 100.5)] + flat(20, 100.5))
        r = run(path, max_hold_ms=5 * M)
        self.assertEqual(r["exit_reason"], "TIME")
        self.assertEqual(r["bars_held"], 5)
        self.assertAlmostEqual(r["r_multiple"], 0.5)
        self.assertEqual(r["exit_time_ms"], T0 + 5 * M)
        self.assertAlmostEqual(r["mfe_r"], 0.6)
        self.assertAlmostEqual(r["mae_r"], -0.4)

    def test_insufficient_path_is_censored(self):
        path = make_path(flat(5))
        r = run(path, max_hold_ms=60 * M)
        self.assertEqual(r["status"], "INSUFFICIENT_PATH")
        self.assertIsNone(r["r_multiple"])
        self.assertIsNone(r["exit_reason"])

    def test_path_ending_exactly_at_deadline_is_covered(self):
        path = make_path(flat(5))
        r = run(path, max_hold_ms=5 * M)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["exit_reason"], "TIME")

    def test_barrier_hit_before_path_end_is_ok(self):
        path = make_path(flat(2) + [(100.0, 100.1, 98.9, 99.0)])
        r = run(path, max_hold_ms=600 * M)
        self.assertEqual(r["status"], "OK")
        self.assertEqual(r["exit_reason"], "SL")

    def test_no_entry_bar(self):
        path = make_path(flat(5))
        r = run(path, signal_time_ms=T0 + 100 * M)
        self.assertEqual(r["status"], "NO_ENTRY_BAR")
        self.assertIsNone(r["r_multiple"])

    def test_zero_cost_r_equals_gross_r(self):
        path = make_path(flat(3) + [(100.0, 100.3, 99.7, 100.2)] + flat(10, 100.2))
        for d in (1, -1):
            r = run(path, direction=d, max_hold_ms=8 * M)
            self.assertAlmostEqual(r["r_multiple"], r["gross_r"])

    def test_base_cost_strictly_lower_than_zero(self):
        path = make_path(flat(3) + [(100.0, 100.3, 99.7, 100.2)] + flat(10, 100.2))
        for d in (1, -1):
            z = run(path, direction=d, max_hold_ms=8 * M, cost=ZERO)
            b = run(path, direction=d, max_hold_ms=8 * M, cost=BASE)
            self.assertLess(b["r_multiple"], z["r_multiple"])
            # Time exit at identical mid: cost difference = full round turn.
            self.assertAlmostEqual(z["r_multiple"] - b["r_multiple"], BASE.round_turn)

    def test_short_uses_ask_half_spread(self):
        cost = CostScenario("S", spread=0.4, slippage=0.0, commission_price=0.0)
        # Short fill = bid = 100 - 0.2 = 99.8; SL = 100.8 on ask -> mid 100.6.
        path = make_path(flat(1) + [(100.0, 100.65, 99.95, 100.5)] + flat(10, 100.5))
        r = run(path, direction=-1, cost=cost)
        self.assertAlmostEqual(r["entry_price"], 99.8)
        self.assertEqual(r["exit_reason"], "SL")
        self.assertAlmostEqual(r["exit_price"], 100.8)
        self.assertAlmostEqual(r["r_multiple"], -1.0)
        # High 100.55 -> ask 100.75 < SL 100.8: no trigger.
        path2 = make_path(flat(1) + [(100.0, 100.55, 99.95, 100.5)] + flat(10, 100.3))
        r2 = run(path2, direction=-1, cost=cost, max_hold_ms=5 * M)
        self.assertEqual(r2["exit_reason"], "TIME")
        # Exit at ask = close 100.3 + 0.2 = 100.5 -> -0.7 R net, -0.3 R mid-to-mid.
        self.assertAlmostEqual(r2["exit_price"], 100.5)
        self.assertAlmostEqual(r2["r_multiple"], -0.7)
        self.assertAlmostEqual(r2["gross_r"], -0.3)

    def test_long_uses_bid_half_spread(self):
        cost = CostScenario("S", spread=0.4, slippage=0.0, commission_price=0.0)
        # Long fill = ask 100.2; SL 99.2 on bid -> mid 99.4.
        path = make_path(flat(1) + [(100.0, 100.05, 99.38, 99.5)] + flat(10, 99.5))
        r = run(path, cost=cost)
        self.assertEqual(r["exit_reason"], "SL")
        self.assertAlmostEqual(r["exit_price"], 99.2)

    def test_entry_never_uses_bar_before_signal(self):
        path = make_path([(90.0, 90.1, 89.9, 90.0)] * 3 + flat(10, 100.0))
        r = run(path, signal_time_ms=T0 + 2 * M + 1, max_hold_ms=3 * M)
        self.assertEqual(r["entry_time_ms"], T0 + 3 * M)
        self.assertAlmostEqual(r["entry_price"], 100.0)
        self.assertGreaterEqual(r["entry_time_ms"], T0 + 2 * M + 1)

    def test_entry_bar_range_counts(self):
        path = make_path([(100.0, 100.1, 98.9, 99.0)] + flat(5, 99.0))
        r = run(path)
        self.assertEqual(r["exit_reason"], "SL")
        self.assertEqual(r["bars_held"], 1)

    def test_long_chunk_skip_matches_late_hit(self):
        # Barrier hit deep in a long path exercises chunk skipping.
        path = make_path(flat(200) + [(100.0, 102.1, 99.95, 102.0)] + flat(10, 102.0))
        r = run(path, max_hold_ms=500 * M)
        self.assertEqual(r["exit_reason"], "TP")
        self.assertEqual(r["bars_held"], 201)
        self.assertAlmostEqual(r["mae_r"], -0.1)

    def test_commission_subtracted_once(self):
        cost = CostScenario("C", spread=0.0, slippage=0.0, commission_price=0.1)
        path = make_path(flat(10))
        r = run(path, cost=cost, max_hold_ms=5 * M)
        self.assertAlmostEqual(r["r_multiple"], -0.1)

    def test_value_errors(self):
        path = make_path(flat(5))
        with self.assertRaises(ValueError):
            run(path, direction=0)
        with self.assertRaises(ValueError):
            run(path, stop_dist=0.0)
        with self.assertRaises(ValueError):
            run(path, stop_dist=-1.0)
        with self.assertRaises(ValueError):
            run(path, tp_r=0.0)
        with self.assertRaises(ValueError):
            run(path, max_hold_ms=0)

    def test_unsorted_path_rejected(self):
        with self.assertRaises(ValueError):
            M1Path([{"timestamp": 2, "open": 1, "high": 1, "low": 1, "close": 1},
                    {"timestamp": 1, "open": 1, "high": 1, "low": 1, "close": 1}])


class HelperTests(unittest.TestCase):
    def test_cost_scenarios(self):
        self.assertEqual(set(COST_SCENARIOS), {"ZERO", "BASE", "STRESS"})
        self.assertAlmostEqual(COST_SCENARIOS["STRESS"].spread, 0.60)
        self.assertAlmostEqual(COST_SCENARIOS["STRESS"].round_turn, 0.60 + 0.30 + 0.07)
        self.assertAlmostEqual(BASE.round_turn, 0.40)

    def test_stop_distance(self):
        self.assertAlmostEqual(stop_distance(1.0), 1.2)
        self.assertAlmostEqual(stop_distance(1.0, structure_distance=1.5), 1.75)
        self.assertAlmostEqual(stop_distance(1.0, structure_distance=5.0), 2.0)  # clamp max
        self.assertAlmostEqual(stop_distance(1.0, atr_multiplier=0.5), 0.8)  # clamp min
        self.assertAlmostEqual(stop_distance(1.0, structure_distance=0.1), 1.2)
        self.assertIsNone(stop_distance(None))
        self.assertIsNone(stop_distance(0.0))
        self.assertIsNone(stop_distance(-1.0))

    def test_vol_target_multiplier(self):
        self.assertEqual(vol_target_multiplier(0.5, 1.0), 1.0)  # never > 1
        self.assertAlmostEqual(vol_target_multiplier(2.0, 1.0), 0.5)
        self.assertAlmostEqual(vol_target_multiplier(100.0, 1.0), 0.25)  # floor
        self.assertAlmostEqual(vol_target_multiplier(100.0, 1.0, floor=0.1), 0.1)
        self.assertIsNone(vol_target_multiplier(None, 1.0))
        self.assertIsNone(vol_target_multiplier(0.0, 1.0))
        for fv in (0.01, 0.3, 1.0, 3.0, 1e6):
            m = vol_target_multiplier(fv, 1.0)
            self.assertLessEqual(m, 1.0)
            self.assertGreaterEqual(m, 0.25)
        with self.assertRaises(ValueError):
            vol_target_multiplier(1.0, 0.0)

    def test_from_jsonl_skips_blank_lines(self):
        rows = [{"timeframe": "M1", "timestamp": T0 + i * M, "close_timestamp": T0 + (i + 1) * M,
                 "open": 1.0 + i, "high": 2.0 + i, "low": 0.5 + i, "close": 1.5 + i,
                 "avg_spread": 0.3, "max_spread": 0.3} for i in range(3)]
        fd, name = tempfile.mkstemp(suffix=".jsonl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(rows[0]) + "\n\n")
                fh.write(json.dumps(rows[1]) + "\n   \n")
                fh.write(json.dumps(rows[2]) + "\n")
            p = M1Path.from_jsonl(name)
        finally:
            os.remove(name)
        self.assertEqual(len(p), 3)
        self.assertEqual(p.opens, [1.0, 2.0, 3.0])
        self.assertEqual(p.index_at_or_after(T0 + 1), 1)
        self.assertEqual(p.index_at_or_after(T0), 0)
        self.assertIsNone(p.index_at_or_after(T0 + 10 * M))

    def test_m1path_from_objects(self):
        class Bar:
            def __init__(self, t):
                self.timestamp, self.open, self.high, self.low, self.close = t, 1, 2, 0, 1
        p = M1Path([Bar(1), Bar(2)])
        self.assertEqual(p.timestamps, [1, 2])


if __name__ == "__main__":
    unittest.main()
