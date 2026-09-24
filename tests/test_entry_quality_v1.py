"""Deterministic causal-contract tests for offline Entry Quality V1."""

from __future__ import annotations

import json
import unittest

from research.analyze_entry_quality_v1 import (BUCKETS, _feature_assessment, analyze_rows,
                                                bucket, directional_outcome, sample_status)
from research.entry_quality_v1 import (CausalFrame, LookAheadError, _ema_atr,
                                       available_pivots, build_entry_row,
                                       confirmed_pivots, context_freshness,
                                       directional_returns, percentile_rank,
                                       range_position, structural_levels)
from research.gold_regime_engine import PARAMETER_VERSION, SOURCE


MINUTE = 60_000


def frame(index: int, timeframe: str = "H1", *, high: float | None = None,
          low: float | None = None, closed: bool = True) -> CausalFrame:
    duration = {"M5": 5, "H1": 60, "H4": 240}[timeframe] * MINUTE
    start = index * duration
    close = 100 + index * .2
    return CausalFrame(timeframe, start, start + duration, start + duration + MINUTE,
                       close, high if high is not None else close + 1,
                       low if low is not None else close - 1, close,
                       10, .2, {}, f"{timeframe}-{index}", closed)


class EntryQualityTests(unittest.TestCase):
    def test_open_m5_h1_h4_rejected(self):
        for tf in ("M5", "H1", "H4"):
            with self.subTest(tf=tf):
                with self.assertRaises(LookAheadError):
                    frame(1, tf, closed=False).require(10**10)

    def test_available_at_and_future_context_rejected(self):
        for tf in ("m5", "h1", "h4"):
            with self.assertRaises(LookAheadError):
                context_freshness({"end": 10, "available_at": 20}, 19, tf)
            with self.assertRaises(LookAheadError):
                context_freshness({"end": 21, "available_at": 20}, 20, tf)

    def test_predeclared_stale_h1_h4(self):
        for tf, threshold in (("h1", 120), ("h4", 480)):
            self.assertEqual(context_freshness({"end": 0, "available_at": 0}, threshold * MINUTE, tf)["freshness_state"], "FRESH")
            state = context_freshness({"end": 0, "available_at": 0}, (threshold + 1) * MINUTE, tf)
            self.assertEqual(state["freshness_state"], "STALE")
            self.assertIn(threshold, state["stale_thresholds_exceeded"])
        self.assertEqual(context_freshness({}, 0, "h1")["freshness_state"], "UNKNOWN")

    def test_pivot_origin_support_resistance_are_confirmed_only(self):
        bars = [frame(i) for i in range(7)]
        bars[2] = frame(2, high=110, low=95)
        bars[4] = frame(4, high=106, low=90)
        pivots = confirmed_pivots(bars)
        self.assertTrue(pivots)
        first = pivots[0]
        self.assertEqual(available_pivots(pivots, first.available_at - 1, 6), [])
        self.assertIn(first, available_pivots(pivots, first.available_at, 6))
        levels = structural_levels(available_pivots(pivots, 10**10, 6), 100, "LONG")
        self.assertEqual(levels["nearest_resistance"], 110)
        self.assertEqual(levels["nearest_support"], 90)
        self.assertEqual(levels["impulse_origin_price"], 90)
        self.assertEqual(levels["room_to_obstacle"], 10)

    def test_open_confirmation_bar_does_not_confirm_pivot(self):
        bars = [frame(i) for i in range(5)]
        bars[2] = frame(2, high=110)
        bars[4] = frame(4, closed=False)
        with self.assertRaises(LookAheadError):
            confirmed_pivots(bars)

    def test_ema_range_acceleration_and_no_lookahead(self):
        bars = [frame(i) for i in range(40)]
        prefix = _ema_atr(bars)
        extension = _ema_atr(bars + [frame(40, high=999)])
        self.assertEqual(prefix, extension[:40])
        self.assertIsNone(range_position(bars, 8, 101, 10)[0])
        position = range_position(bars, 39, bars[39].close, 10)[0]
        self.assertGreaterEqual(position, 0)
        self.assertLessEqual(position, 1)
        self.assertEqual(directional_returns(bars, 5, "SHORT")["recent_return_1"],
                         -directional_returns(bars, 5, "LONG")["recent_return_1"])
        self.assertIsNotNone(directional_returns(bars, 5, "LONG")["momentum_acceleration"])

    def test_spread_percentile_is_reference_only(self):
        frozen_train = [.1, .2, .3, .4]
        self.assertEqual(percentile_rank(.25, frozen_train), .5)
        self.assertEqual(percentile_rank(.25, frozen_train),
                         percentile_rank(.25, frozen_train))
        self.assertIsNone(percentile_rank(.2, []))
        # An OOS spread cannot mutate the frozen TRAIN percentile reference.
        self.assertEqual(frozen_train, [.1, .2, .3, .4])

    def test_build_row_is_causal_diagnostic_and_deterministic(self):
        h1 = [frame(i) for i in range(35)]
        m5 = [frame(i, "M5") for i in range(410)]
        h1_bar, m5_bar = h1[34], m5[409]
        time = max(h1_bar.available_at, m5_bar.available_at)
        context = lambda bar: {"bar_id": bar.bar_id, "end": bar.end,
                               "available_at": bar.available_at,
                               "features": {"atr": 2.0, "ema20": 105.0, "ema50": 104.0},
                               "state": {"trend_state": "UP", "structure_state": "BULLISH"}}
        regime = {"event_id": "event-1", "decision_time": time, "diagnostic_only": True,
                  "score_effect": 0, "source": SOURCE, "parameter_version": PARAMETER_VERSION,
                  "split": "TRAIN", "armed_direction": "LONG",
                  "long_score": 70, "short_score": 30, "h1": context(h1_bar),
                  "m5": context(m5_bar), "h4": {}, "gold_regime": "TREND_UP"}
        observation = {"event_id": "event-1", "observed_at": time, "timestamp": time - MINUTE,
                       "bar_closed": True, "open": 106.0, "high": 107.0, "low": 105.0,
                       "close": 106.5, "spread": .3, "momentum": "LONG"}
        args = (h1, m5, confirmed_pivots(h1), {h1_bar.bar_id: 34},
                {m5_bar.bar_id: 409}, _ema_atr(m5), [.1, .2, .3], [.2], "TRAIN_ONLINE_PREFIX")
        first = build_entry_row(regime, observation, *args)
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(build_entry_row(regime, observation, *args), sort_keys=True))
        self.assertEqual(first["direction"], "LONG")
        self.assertEqual(first["setup"], "MOMENTUM")
        self.assertEqual(first["score_effect"], 0)
        self.assertIs(first["diagnostic_only"], True)
        self.assertIsNone(first["entry_to_current_sl"])
        self.assertIsNone(first["slippage"])
        m15 = {"event_id": "m15", "observed_at": time - 15 * MINUTE,
               "timeframe": "M15", "bar_closed": True, "momentum": "LONG"}
        with_m15 = build_entry_row(regime, observation, *args, m15)
        self.assertEqual(with_m15["m15_timing"]["freshness_state"], "FRESH")
        stale = dict(m15, observed_at=time - 46 * MINUTE)
        self.assertIn("m15_timing", build_entry_row(regime, observation, *args, stale)["stale_dependent_features"])
        with self.assertRaises(LookAheadError):
            build_entry_row(regime, observation, *args, dict(m15, observed_at=time + 1))
        bad = dict(regime)
        bad["h4"] = {"end": time + 1, "available_at": time + 1}
        with self.assertRaises(LookAheadError):
            build_entry_row(bad, observation, *args)

    def test_analysis_direction_and_outcomes_after_features(self):
        item = {"event_id": "x", "decision_time": 10, "parameter_version": "entry-quality-v1",
                "diagnostic_only": True, "score_effect": 0, "split": "OOS",
                "direction": "SHORT", "setup": "MOMENTUM", "gold_regime": "TREND_DOWN"}
        outcome = {"completed_horizon": "1h", "evaluated_at": 10 + 3_600_000,
                   "future_return_1h": -.01,
                   "mfe_1h": .002, "mae_1h": -.015}
        self.assertEqual(directional_outcome(outcome, "SHORT", "1h"), (.01, .015, -.002))
        rows, counts = analyze_rows([item], {"x": {"1h": outcome}})
        self.assertEqual(counts["OOS:1h:measured"], 1)
        self.assertEqual(counts["OOS:4h:unavailable"], 1)
        self.assertTrue(all(row["direction"] in {"ALL", "SHORT"} for row in rows))
        self.assertEqual(item["direction"], "SHORT")
        with self.assertRaises(ValueError):
            analyze_rows([item], {"x": {"1h": dict(outcome, evaluated_at=10)}})

    def test_bins_sample_control_and_no_threshold_search(self):
        self.assertEqual(bucket(1.5, BUCKETS["impulse_distance_atr"]), "[1.5,2)")
        self.assertEqual(bucket(None, BUCKETS["impulse_distance_atr"]), "UNKNOWN")
        self.assertEqual([sample_status(n) for n in (29, 30, 99, 100)],
                         ["INSUFFICIENT", "THIN_SAMPLE", "THIN_SAMPLE", "SUFFICIENT"])
        self.assertEqual(_feature_assessment([], "impulse_distance_atr")[0], "INSUFFICIENT_SAMPLE")


if __name__ == "__main__":
    unittest.main()
