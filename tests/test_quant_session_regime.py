"""Synthetic tests for the session / volatility-regime quant study."""

from __future__ import annotations

import argparse
import unittest
from datetime import datetime, timezone

from research.quant import edge_study as es
from research.quant import session_regime as sr
from research.quant import session_regime_study as study

HOUR = sr.HOUR_MS
M1 = sr.M1_MS


def wall_ms(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def ok(entry: int, exit_ms: int, r: float, reason: str = "SL") -> dict:
    return {"status": "OK", "entry_time_ms": entry, "exit_time_ms": exit_ms,
            "exit_reason": reason, "r_multiple": r}


class BucketTests(unittest.TestCase):
    def test_server_hour_buckets(self) -> None:
        self.assertEqual(sr.server_hour_bucket(wall_ms(2026, 3, 4, 0)), "HOUR_00_05")
        self.assertEqual(sr.server_hour_bucket(wall_ms(2026, 3, 4, 5, 59)), "HOUR_00_05")
        self.assertEqual(sr.server_hour_bucket(wall_ms(2026, 3, 4, 6)), "HOUR_06_11")
        self.assertEqual(sr.server_hour_bucket(wall_ms(2026, 3, 4, 13)), "HOUR_12_17")
        self.assertEqual(sr.server_hour_bucket(wall_ms(2026, 3, 4, 23)), "HOUR_18_23")

    def test_day_of_week_sunday_merges_monday_saturday_none(self) -> None:
        self.assertEqual(sr.dow_bucket(wall_ms(2026, 3, 2, 10)), "DOW_MON")  # Monday
        self.assertEqual(sr.dow_bucket(wall_ms(2026, 3, 6, 10)), "DOW_FRI")
        self.assertEqual(sr.dow_bucket(wall_ms(2026, 3, 1, 23)), "DOW_MON")  # Sunday reopen
        self.assertIsNone(sr.dow_bucket(wall_ms(2026, 3, 7, 10)))  # Saturday

    def test_tercile_cuts_and_labels(self) -> None:
        cuts = sr.tercile_cuts([None, 1, 2, 3, 4, 5, 6, 7, 8, 9])
        self.assertEqual(cuts, (4, 7))
        self.assertEqual(sr.tercile_label(3.9, cuts), "LOW")
        self.assertEqual(sr.tercile_label(4, cuts), "MID")
        self.assertEqual(sr.tercile_label(7, cuts), "HIGH")
        self.assertIsNone(sr.tercile_label(None, cuts))
        self.assertIsNone(sr.tercile_cuts([1, None, 2]))
        self.assertIsNone(sr.tercile_label(1.0, None))

    def test_vol_ratio_and_h4_strength(self) -> None:
        self.assertEqual(sr.vol_ratio_bucket(1.01), "VR_EXPANSION")
        self.assertEqual(sr.vol_ratio_bucket(1.0), "VR_CONTRACTION")
        self.assertIsNone(sr.vol_ratio_bucket(None))
        self.assertEqual(sr.h4_strength({"h4_dist_ema50_atr": -2.5}), 2.5)
        self.assertIsNone(sr.h4_strength({}))

    def test_cuts_come_from_train_only(self) -> None:
        train = [{"h1_rv24": v, "h4_dist_ema50_atr": -v} for v in range(1, 10)]
        cuts = sr.TrainCuts.from_train(train)
        # A huge test value does not move cuts: it is HIGH relative to train.
        b = sr.candidate_buckets(wall_ms(2026, 3, 3, 8), {"h1_rv24": 100.0,
                                 "h4_dist_ema50_atr": 0.5, "h1_vol_ratio": 0.8}, cuts)
        self.assertEqual(b, {"HOUR_06_11", "DOW_TUE", "RV_HIGH", "H4S_LOW", "VR_CONTRACTION"})

    def test_missing_features_skip_family(self) -> None:
        cuts = sr.TrainCuts(None, None)
        b = sr.candidate_buckets(wall_ms(2026, 3, 3, 8), {}, cuts)
        self.assertEqual(b, {"HOUR_06_11", "DOW_TUE"})

    def test_family_size_is_pre_registered(self) -> None:
        self.assertEqual(len(sr.ALL_BUCKETS), 17)
        names = sr.hypothesis_names((1.25, 2.0))
        self.assertEqual(len(names), 102)
        self.assertEqual(len(set(names)), 102)


class SlotTests(unittest.TestCase):
    def test_intrabar_exit_releases_at_bar_close(self) -> None:
        t = sr.slot_trade("a", 1, ok(0, 10 * M1, 1.0, "TP"), max_hold_ms=24 * HOUR)
        self.assertEqual(t.release_time_ms, 11 * M1)
        t2 = sr.slot_trade("b", 1, ok(0, 10 * M1, 0.2, "TIME"), max_hold_ms=24 * HOUR)
        self.assertEqual(t2.release_time_ms, 10 * M1)

    def test_entry_in_exit_bar_is_blocked(self) -> None:
        a = sr.slot_trade("a", 1, ok(0, 10 * M1, -1.0), max_hold_ms=HOUR)
        same_bar = sr.slot_trade("b", 1, ok(10 * M1, 20 * M1, 1.0), max_hold_ms=HOUR)
        next_bar = sr.slot_trade("c", 1, ok(11 * M1, 20 * M1, 1.0), max_hold_ms=HOUR)
        self.assertEqual([t.key for t in sr.single_slot([a, same_bar])], ["a"])
        self.assertEqual([t.key for t in sr.single_slot([next_bar, a])], ["a", "c"])

    def test_sequencing_uses_entry_time_not_signal_order(self) -> None:
        late = sr.slot_trade("late", 1, ok(5 * M1, 50 * M1, 1.0), max_hold_ms=HOUR)
        early = sr.slot_trade("early", 1, ok(1 * M1, 3 * M1, 1.0), max_hold_ms=HOUR)
        self.assertEqual([t.key for t in sr.single_slot([late, early])], ["early", "late"])

    def test_censored_reserves_slot_for_max_hold(self) -> None:
        cens = sr.slot_trade("c", -1, {"status": "INSUFFICIENT_PATH", "entry_time_ms": 0},
                             max_hold_ms=24 * HOUR)
        self.assertIsNone(cens.r)
        self.assertEqual(cens.release_time_ms, 24 * HOUR)
        inside = sr.slot_trade("x", 1, ok(23 * HOUR, 25 * HOUR, 1.0), max_hold_ms=24 * HOUR)
        after = sr.slot_trade("y", 1, ok(24 * HOUR, 25 * HOUR, 1.0), max_hold_ms=24 * HOUR)
        self.assertEqual([t.key for t in sr.single_slot([cens, inside, after])], ["c", "y"])

    def test_no_entry_is_dropped(self) -> None:
        self.assertIsNone(sr.slot_trade("a", 1, {"status": "NO_ENTRY_BAR", "entry_time_ms": None},
                                        max_hold_ms=HOUR))
        self.assertIsNone(sr.slot_trade("a", 1, None, max_hold_ms=HOUR))


class GateTests(unittest.TestCase):
    @staticmethod
    def strong(n: int = 150) -> dict:
        vals = [1.0 if i % 3 else -0.3 for i in range(n)]
        return sr.summarize(vals, n_trials=142, block_size=5, seed=0)

    def bench(self, e: float | None) -> dict:
        return {"expectancy_r": e}

    def test_dsr_uses_noise_variance(self) -> None:
        s = self.strong()
        self.assertAlmostEqual(s["dsr_sr_variance"], 1 / 149)
        self.assertGreater(s["dsr"], 0.95)
        weak = sr.summarize([0.05 if i % 2 else -0.04 for i in range(150)], n_trials=142,
                            block_size=5, seed=0)
        self.assertLess(weak["dsr"], 0.95)

    def test_gate_passes_only_with_all_conditions(self) -> None:
        row = {"single_slot": self.strong(), "bh_reject": True}
        benches = {b: self.bench(0.05) for b in sr.BENCHMARK_RULES}
        self.assertTrue(sr.gate(row, benches)["passes"])
        row_no_bh = {"single_slot": self.strong(), "bh_reject": False}
        self.assertFalse(sr.gate(row_no_bh, benches)["passes"])
        small = {"single_slot": self.strong(60), "bh_reject": True}
        self.assertFalse(sr.gate(small, benches)["checks"]["n_ge_100"])

    def test_gate_fails_closed_on_missing_benchmark(self) -> None:
        row = {"single_slot": self.strong(), "bh_reject": True}
        for missing in ({"BASELINE": self.bench(0.0), "LONG_ONLY": self.bench(0.0)},
                        {"BASELINE": self.bench(0.0), "LONG_ONLY": self.bench(0.0),
                         "SHORT_ONLY": self.bench(None)},
                        {"BASELINE": self.bench(0.0), "LONG_ONLY": None,
                         "SHORT_ONLY": self.bench(0.0)}):
            g = sr.gate(row, missing)
            self.assertFalse(g["passes"])
            self.assertFalse(g["checks"]["benchmarks_known"])

    def test_gate_requires_beating_best_benchmark(self) -> None:
        row = {"single_slot": self.strong(), "bh_reject": True}
        e = row["single_slot"]["expectancy_r"]
        benches = {"BASELINE": self.bench(0.0), "LONG_ONLY": self.bench(e + 0.01),
                   "SHORT_ONLY": self.bench(-1.0)}
        self.assertFalse(sr.gate(row, benches)["checks"]["beats_benchmarks"])

    def test_bh_is_joint_across_family(self) -> None:
        rows = [{"single_slot": {"p_value_mean_gt_0": p}} for p in (0.001, 0.04, 0.5, None)]
        sr.apply_bh(rows, q=0.10)
        # m=4 includes the None: 0.001<=0.025 yes; 0.04<=0.05 yes.
        self.assertEqual([r["bh_reject"] for r in rows], [True, True, False, False])
        rows2 = [{"single_slot": {"p_value_mean_gt_0": p}} for p in [0.04] + [0.9] * 99]
        sr.apply_bh(rows2, q=0.10)
        self.assertFalse(rows2[0]["bh_reject"])


def cand(t: int, direction: int, r: float, feats: dict, *, hold: int = 30 * M1,
         status: str = "OK") -> es.Candidate:
    c = es.Candidate(f"e{t}", t, direction, 80, feats, 1.0)
    for cost in ("BASE", "STRESS"):
        if status == "OK":
            c.outcomes[(cost, 1.25)] = {"status": "OK", "entry_time_ms": t, "exit_time_ms": t + hold,
                                        "exit_reason": "SL", "r_multiple": r}
        else:
            c.outcomes[(cost, 1.25)] = {"status": status, "entry_time_ms": t, "r_multiple": None,
                                        "exit_time_ms": None}
    return c


class StudyPipelineTests(unittest.TestCase):
    def make(self) -> list[es.Candidate]:
        base = wall_ms(2026, 1, 5, 0)
        out = []
        for i in range(400):
            t = base + i * 5 * HOUR
            d = 1 if i % 3 else -1
            r = (1.0 if i % 2 else -0.8) if d > 0 else -0.5
            out.append(cand(t, d, r, {"h1_rv24": float(i % 7 + 1), "h1_vol_ratio": 0.5 + (i % 3) * 0.4,
                                      "h4_dist_ema50_atr": d * float(i % 5)}))
        out.append(cand(base + 400 * 5 * HOUR, 1, 0.0, {}, status="INSUFFICIENT_PATH"))
        return out

    def test_oos_assignment_and_censored_inclusion(self) -> None:
        cands = self.make()
        items, rows = study.oos_assignments(cands, 1.25, n_folds=5, embargo_ms=24 * HOUR,
                                            min_train=50)
        self.assertTrue(rows)
        first_test = min(r["test_start_ms"] for r in rows)
        self.assertTrue(all(c.signal_time_ms >= first_test for c, _ in items))
        self.assertIn(cands[-1], [c for c, _ in items])
        # cuts computed per fold from train rv24 values (1..7)
        self.assertTrue(all(1 <= r["rv24_cuts"][0] <= 7 for r in rows))

    def test_rule_evaluation_and_short_breakdown(self) -> None:
        cands = self.make()
        items, _ = study.oos_assignments(cands, 1.25, n_folds=5, embargo_ms=24 * HOUR, min_train=50)
        kw = dict(max_hold_ms=24 * HOUR, n_trials=142, slot_block=5, event_block=20, seed=0)
        short = study.evaluate_rule(items, 1.25, bucket=None, direction=-1, **kw)
        self.assertAlmostEqual(short["single_slot"]["expectancy_r"], -0.5)
        self.assertEqual(short["single_slot_long"]["n"], 0)
        both = study.evaluate_rule(items, 1.25, bucket="HOUR_00_05", direction=0, **kw)
        self.assertGreater(both["single_slot"]["n"], 0)
        self.assertLessEqual(both["single_slot"]["n"], both["all_events"]["n"])
        self.assertEqual(both["single_slot"]["n"], both["single_slot_long"]["n"]
                         + both["single_slot_short"]["n"])

    def test_report_flags_and_markdown(self) -> None:
        cands = self.make()
        items, _ = study.oos_assignments(cands, 1.25, n_folds=5, embargo_ms=24 * HOUR, min_train=50)
        kw = dict(max_hold_ms=24 * HOUR, n_trials=142, slot_block=5, event_block=20, seed=0)
        rows = []
        for b in sr.ALL_BUCKETS:
            for dn, d in sr.DIRECTIONS.items():
                row = study.evaluate_rule(items, 1.25, bucket=b, direction=d, **kw)
                row.update(hypothesis=sr.hypothesis_key(1.25, b, dn), direction_name=dn)
                rows.append(row)
        sr.apply_bh(rows, q=0.10)
        benches = {f"tp1.25_{n}": study.evaluate_rule(items, 1.25, bucket=None, direction=d, **kw)
                   for n, d in zip(sr.BENCHMARK_RULES, (0, 1, -1))}
        for r in rows:
            r["gate"] = sr.gate(r, {b: benches[f"tp1.25_{b}"]["single_slot"]
                                    for b in sr.BENCHMARK_RULES})
        report = {"broker_clock": {"broker_clock_status": "UNKNOWN"},
                  "params": {"n_hypotheses": len(rows), "n_trials_for_dsr": 142, "gate_cost": "STRESS"},
                  "benchmarks": benches, "hypotheses": rows,
                  "verdict": {"gate": sr.GATE_TEXT, "bh_rejections": [], "candidates_for_paper": [],
                              "note": "n"}}
        md = study.render_markdown(report)
        self.assertIn("tp1.25_RV_LOW_SHORT", md)
        self.assertIn("UNKNOWN", md)

    def test_cli_defaults(self) -> None:
        a = study.parse_args([])
        self.assertIsInstance(a, argparse.Namespace)
        self.assertEqual(a.tp_r, [1.25, 2.0])
        self.assertEqual(a.slot_block_size, 5)
        self.assertEqual(a.block_size, 20)


if __name__ == "__main__":
    unittest.main()
