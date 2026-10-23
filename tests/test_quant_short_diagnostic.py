"""Synthetic tests for the Quant SHORT diagnostic helpers."""

from __future__ import annotations

import csv
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from research.quant import barriers, edge_study as es, short_diagnostic as sd
from research.quant.features import FEATURE_NAMES, Bar, CausalBarSeries

HOUR = es.HOUR_MS
MIN = 60_000
ZERO = barriers.COST_SCENARIOS["ZERO"]


def cand(direction: int, **feats) -> es.Candidate:
    base = {"h4_trend_sign": -1.0, "h1_tsmom_120": -0.5, "h1_rsi14": 40.0,
            "h1_dist_ema20_atr": -0.5}
    base.update(feats)
    return es.Candidate("e", 0, direction, 80, base, 1.0)


def slot_cand(t: int, direction: int, r: float, **feats) -> es.Candidate:
    c = cand(direction, **feats)
    c.event_id, c.signal_time_ms = f"e{t}", t
    for cost in ("BASE", "STRESS"):
        c.outcomes[(cost, 1.25)] = {"status": "OK", "r_multiple": r, "exit_time_ms": t + HOUR}
    return c


def path(prices: list[tuple[float, float, float, float]]) -> barriers.M1Path:
    return barriers.M1Path([{"timestamp": i * MIN, "open": o, "high": h, "low": lo, "close": c}
                            for i, (o, h, lo, c) in enumerate(prices)])


class RuleTests(unittest.TestCase):
    def test_rules_require_short_and_known_features(self) -> None:
        self.assertTrue(sd.rule_s1(cand(-1)))
        self.assertFalse(sd.rule_s1(cand(1)))
        self.assertFalse(sd.rule_s1(cand(-1, h4_trend_sign=None)))
        self.assertTrue(sd.rule_s2(cand(-1)))
        self.assertFalse(sd.rule_s2(cand(-1, h1_tsmom_120=0.1)))
        self.assertFalse(sd.rule_s3(cand(-1, h1_rsi14=29.9)))
        self.assertTrue(sd.rule_s3(cand(-1, h1_rsi14=30.0, h1_dist_ema20_atr=-1.5)))
        self.assertFalse(sd.rule_s3(cand(-1, h1_dist_ema20_atr=-1.6)))
        self.assertFalse(sd.rule_s3(cand(-1, h1_rsi14=None)))
        self.assertTrue(sd.rule_s4(cand(-1)))
        self.assertFalse(sd.rule_s4(cand(-1, h1_rsi14=20.0)))
        self.assertEqual(len(sd.PREREGISTERED_RULES), 4)


class BucketTests(unittest.TestCase):
    def test_bucket_edges(self) -> None:
        self.assertEqual(sd.rsi_bucket(cand(-1, h1_rsi14=29.99)), "<30")
        self.assertEqual(sd.rsi_bucket(cand(-1, h1_rsi14=30.0)), "30-40")
        self.assertEqual(sd.rsi_bucket(cand(-1, h1_rsi14=70.0)), ">=70")
        self.assertIsNone(sd.rsi_bucket(cand(-1, h1_rsi14=None)))
        self.assertEqual(sd.extension_bucket(cand(-1, h1_dist_ema20_atr=-2.0)), "<-1.5")
        self.assertEqual(sd.extension_bucket(cand(-1, h1_dist_ema20_atr=0.0)), "-0.5..0.5")
        self.assertEqual(sd.extension_bucket(cand(-1, h1_dist_ema20_atr=1.5)), ">=1.5")
        self.assertEqual(sd.trend_bucket(cand(-1, h1_tsmom_120=0.2)), "h4-_mom120+")
        self.assertIsNone(sd.trend_bucket(cand(-1, h1_tsmom_120=None)))
        c = cand(-1)
        for score, label in ((74, "74-79"), (80, "80-85"), (86, ">=86")):
            c.score = score
            self.assertEqual(sd.score_bucket(c), label)

    def test_quarter_label(self) -> None:
        ms = int(datetime(2026, 4, 1, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(sd.quarter_label(ms), "2026Q2")
        self.assertEqual(sd.quarter_label(ms - 1), "2026Q1")

    def test_bucket_rows_and_bh(self) -> None:
        cs = [slot_cand(i * HOUR, -1, 1.0 if i % 2 else -1.0, h1_rsi14=25.0) for i in range(40)]
        cs += [slot_cand(i * HOUR, -1, 0.5, h1_rsi14=45.0) for i in range(40)]
        rows = sd.apply_bh(sd.bucket_rows(cs, sd.rsi_bucket, family="rsi", cost="STRESS",
                                          tp_r=1.25, block_size=20, seed=0), q=0.10)
        by = {r["bucket"]: r for r in rows}
        self.assertEqual(by["<30"]["n"], 40)
        self.assertAlmostEqual(by["<30"]["expectancy_r"], 0.0)
        self.assertTrue(by["40-50"]["bh_reject"])  # constant +0.5R
        self.assertFalse(by["<30"]["bh_reject"])


class PathHelperTests(unittest.TestCase):
    def test_forward_drift_is_direction_aligned_and_censored(self) -> None:
        p = path([(100.0, 100.0, 100.0, 100.0 + i) for i in range(120)])
        self.assertAlmostEqual(sd.forward_drift_r(p, entry_time_ms=0, direction=1, stop_dist=10.0,
                                                  horizon_ms=HOUR), 5.9)
        self.assertAlmostEqual(sd.forward_drift_r(p, entry_time_ms=0, direction=-1,
                                                  stop_dist=10.0, horizon_ms=HOUR), -5.9)
        self.assertIsNone(sd.forward_drift_r(p, entry_time_ms=0, direction=1, stop_dist=10.0,
                                             horizon_ms=3 * HOUR))

    def test_premature_stop_detection(self) -> None:
        # SHORT, stop 1.0: price spikes to SL then falls 1R+ below entry.
        bars = [(100.0, 100.0, 100.0, 100.0), (100.0, 101.5, 100.0, 101.0)]
        bars += [(101.0, 101.0, 98.5, 99.0)] + [(99.0, 99.0, 99.0, 99.0)] * 10
        p = path(bars)
        o = barriers.triple_barrier(p, signal_time_ms=0, direction=-1, stop_dist=1.0, tp_r=2.0,
                                    max_hold_ms=10 * MIN, cost=ZERO)
        self.assertEqual(o["exit_reason"], "SL")
        self.assertTrue(sd.reached_after_exit(p, outcome=o, direction=-1, stop_dist=1.0,
                                              window_end_ms=10 * MIN))
        # Window ends before the reversal bar -> not premature.
        self.assertFalse(sd.reached_after_exit(p, outcome=o, direction=-1, stop_dist=1.0,
                                               window_end_ms=2 * MIN))
        self.assertIsNone(sd.reached_after_exit(p, outcome=o, direction=-1, stop_dist=1.0,
                                                window_end_ms=60 * MIN))  # not covered
        self.assertIsNone(sd.reached_after_exit(p, outcome={**o, "exit_reason": "TP"},
                                                direction=-1, stop_dist=1.0,
                                                window_end_ms=10 * MIN))

    def test_quantiles_and_exit_mix(self) -> None:
        self.assertEqual(sd.quantiles([4.0, 1.0, 3.0, 2.0, None]), [1.75, 2.5, 3.25])
        self.assertEqual(sd.quantiles([]), [None, None, None])
        mix = sd.exit_mix([{"status": "OK", "exit_reason": "SL"},
                           {"status": "OK", "exit_reason": "TP"},
                           {"status": "INSUFFICIENT_PATH"}])
        self.assertEqual((mix["n"], mix["SL"], mix["TP"], mix["TIME"]), (2, 1, 1, 0))
        self.assertEqual(mix["SL_share"], 0.5)


class TrendAndDriftTests(unittest.TestCase):
    def test_long_horizon_logret_is_causal(self) -> None:
        bars = [Bar("H1", i * HOUR, (i + 1) * HOUR, 0.0, 0.0, 0.0, 100.0 + i, 0.3)
                for i in range(10)]
        h1 = CausalBarSeries(bars)
        # As of 6h only bars 0..5 are closed.
        self.assertAlmostEqual(sd.long_horizon_logret(h1, 6 * HOUR, bars=5),
                               math.log(105.0 / 100.0))
        self.assertIsNone(sd.long_horizon_logret(h1, 3 * HOUR, bars=5))

    def test_market_and_candidate_drift(self) -> None:
        closes = [100.0 + i for i in range(30)]
        starts = [i * HOUR for i in range(30)]
        m = sd.market_drift_by_quarter(closes, starts, horizon_bars=24)
        self.assertEqual(m["1970Q1"]["n"], 6)
        self.assertEqual(m["1970Q1"]["mean_move"], 24.0)
        ends = [(i + 1) * HOUR for i in range(30)]
        short = slot_cand(2 * HOUR, -1, 0.0)
        rows = sd.candidate_drift_by_quarter([short], closes, ends, horizon_bars=24)
        self.assertEqual(rows[0]["mean_aligned_move"], -24.0)


class GateTests(unittest.TestCase):
    @staticmethod
    def slot(n: int, e: float, p: float = 0.5) -> dict:
        return {"single_slot": {"n": n, "expectancy_r": e, "ci_lower": 0.05, "dsr": 0.99,
                                "profit_factor": 1.5, "p_value_mean_gt_0": p}}

    def test_small_sample_is_insufficient_even_if_strong(self) -> None:
        oos = {"tp1.25_BASELINE_STRESS": self.slot(200, 0.05),
               "tp1.25_LONG_ONLY_STRESS": self.slot(130, 0.10),
               "tp1.25_SHORT_ONLY_STRESS": self.slot(65, -0.03),
               "tp1.25_S2_SHORT_TREND_ALIGNED_STRESS": self.slot(60, 0.50, 0.001),
               "tp1.25_S3_SHORT_NOT_EXHAUSTED_STRESS": self.slot(150, 0.50, 0.001)}
        v = sd.gate(oos, q=0.10)
        by = {r["rule"]: r for r in v["rules"]}
        self.assertEqual(by["tp1.25_S2_SHORT_TREND_ALIGNED_STRESS"]["status"], "INSUFFICIENT_SAMPLE")
        self.assertEqual(by["tp1.25_S3_SHORT_NOT_EXHAUSTED_STRESS"]["status"], "PAPER_CANDIDATE")
        self.assertTrue(by["tp1.25_S2_SHORT_TREND_ALIGNED_STRESS"]["beats_short_only"])

    def test_oos_candidates_exclude_first_block(self) -> None:
        cs = [slot_cand(i * HOUR, -1 if i % 3 else 1, 0.1) for i in range(600)]
        test = sd.oos_test_candidates(cs, 1.25, n_folds=5, embargo_ms=0, min_train=50)
        self.assertEqual(len(test), 500)
        self.assertEqual(min(c.signal_time_ms for c in test), 100 * HOUR)
        oos = sd.evaluate_rules(test, 1.25, {"SHORT_ONLY": sd.BENCHMARKS["SHORT_ONLY"]},
                                n_trials=42, sr_variance=0.01, slot_block=5, seed=0)
        self.assertEqual(oos["tp1.25_SHORT_ONLY_STRESS"]["events_n"],
                         sum(c.direction < 0 for c in test))


class LoadingTests(unittest.TestCase):
    def test_csv_roundtrip_keeps_none(self) -> None:
        header = ["event_id", "signal_time_ms", "direction", "score", "stop_dist",
                  "r_BASE_tp1.25", "r_STRESS_tp1.25", *FEATURE_NAMES]
        row = ["x", "1000", "-1", "77", "12.5", "-1.0", "", *(["0.5"] * len(FEATURE_NAMES))]
        row[header.index("h1_rsi14")] = ""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "c.csv"
            with p.open("w", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerows([header, row])
            (c,) = sd.load_candidates_csv(p)
        self.assertEqual((c.direction, c.score, c.stop_dist), (-1, 77, 12.5))
        self.assertIsNone(c.features["h1_rsi14"])
        self.assertEqual(c.features["h4_trend_sign"], 0.5)
        self.assertEqual(c.csv_r[("BASE", 1.25)], -1.0)
        self.assertIsNone(c.csv_r[("STRESS", 1.25)])


if __name__ == "__main__":
    unittest.main()
