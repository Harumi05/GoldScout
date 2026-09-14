"""Deterministic tests for the offline Adaptive Stop v2 comparison."""

from __future__ import annotations

from pathlib import Path
import unittest

from research.analyze_adaptive_stop_v2 import (
    assess_variants,
    adaptive_setup,
    build_adaptive_candidates,
    constant_risk_lot_multiplier,
    planned_risk_ratio,
    reached_r_before_stop,
    simulate_adaptive_tick_order,
)
from research.analyze_historical_dataset import temporal_split
from research.analyze_stop_loss_quality import SignalState
from research.tick_historical_replay import Tick


def make_signal(direction: str = "LONG", setup: str = "CONTINUATION") -> SignalState:
    breakout = setup == "BREAKOUT"
    pullback = setup == "PULLBACK"
    momentum = setup == "MOMENTUM"
    recovery = setup == "RECOVERY"
    return SignalState(
        event_id=f"adaptive-{direction}-{setup}",
        anchor_ms=1_700_000_000_000,
        period="TRAIN",
        direction=direction,
        setup=setup,
        reference_close=100.0,
        atr=1.0,
        recent_high=100.2,
        recent_low=99.8,
        swing_high=100.2,
        swing_low=99.8,
        structure="BULLISH" if direction == "LONG" else "BEARISH",
        behavior_shift="NONE",
        momentum=momentum,
        breakout=breakout,
        pullback=pullback,
        recovery=recovery,
        session=None,
        spread=0.2,
        long_score=75.0,
        short_score=75.0,
        source_timeframe="H1",
    )


class AdaptiveStopV2Tests(unittest.TestCase):
    def test_floor_1_5_never_allows_a_shorter_stop(self):
        item = make_signal("LONG")
        stops = build_adaptive_candidates(item, 100.1)
        self.assertAlmostEqual(stops["CURRENT"].distance_atr, 1.2)
        self.assertAlmostEqual(stops["FLOOR_1_5"].distance_atr, 1.5)

    def test_floor_2_0_is_exactly_two_atr(self):
        item = make_signal("SHORT")
        stops = build_adaptive_candidates(item, 99.9)
        self.assertAlmostEqual(stops["FLOOR_2_0"].distance_atr, 2.0)

    def test_setup_adaptive_rules(self):
        expected = {
            "BREAKOUT": 2.0,
            "MOMENTUM": 2.0,
            "PULLBACK": 1.5,
            "RECOVERY": 1.5,
        }
        for setup, distance in expected.items():
            with self.subTest(setup=setup):
                item = make_signal("LONG", setup)
                self.assertAlmostEqual(
                    build_adaptive_candidates(item, 100.1)["SETUP_ADAPTIVE_V1"].distance_atr,
                    distance,
                )
        continuation_long = make_signal("LONG")
        continuation_short = make_signal("SHORT")
        self.assertAlmostEqual(build_adaptive_candidates(continuation_long, 100.1)["SETUP_ADAPTIVE_V1"].distance_atr, 2.0)
        self.assertAlmostEqual(build_adaptive_candidates(continuation_short, 99.9)["SETUP_ADAPTIVE_V1"].distance_atr, 1.5)

    def test_recovery_does_not_override_primary_h1_setup(self):
        item = make_signal("LONG", "BREAKOUT")
        item.recovery = True
        self.assertEqual(adaptive_setup(item), "BREAKOUT")

    def test_long_and_short_stops_are_on_the_safe_side(self):
        long_stops = build_adaptive_candidates(make_signal("LONG"), 100.1)
        short_stops = build_adaptive_candidates(make_signal("SHORT"), 99.9)
        self.assertTrue(all(stop.price < 100.1 for stop in long_stops.values()))
        self.assertTrue(all(stop.price > 99.9 for stop in short_stops.values()))

    def test_lotage_is_inverse_to_distance_and_risk_is_constant(self):
        multiplier = constant_risk_lot_multiplier(1.2, 2.0)
        self.assertAlmostEqual(multiplier, 0.6)
        self.assertAlmostEqual(planned_risk_ratio(1.2, 2.0), 1.0)
        self.assertLessEqual(multiplier, 1.0)

    def test_stop_and_r_target_use_tick_order(self):
        item = make_signal("LONG")
        anchor = item.anchor_ms
        ticks = [
            Tick(anchor, 99.9, 100.1),
            Tick(anchor + 1_000, 100.8, 101.0),
            Tick(anchor + 2_000, 98.7, 98.9),
        ]
        simulate_adaptive_tick_order([item], ticks, progress_every=0)
        current = item.stops["CURRENT"]
        self.assertEqual(current.r_hit_ms[0.5], anchor + 1_000)
        self.assertEqual(current.hit_ms, anchor + 2_000)
        self.assertTrue(reached_r_before_stop(current, 0.5, anchor + 3_000))

    def test_r_reached_after_stop_does_not_count(self):
        item = make_signal("LONG")
        anchor = item.anchor_ms
        ticks = [
            Tick(anchor, 99.9, 100.1),
            Tick(anchor + 1_000, 98.7, 98.9),
            Tick(anchor + 2_000, 100.8, 101.0),
        ]
        simulate_adaptive_tick_order([item], ticks, progress_every=0)
        current = item.stops["CURRENT"]
        self.assertEqual(current.hit_ms, anchor + 1_000)
        self.assertEqual(current.r_hit_ms[0.5], anchor + 2_000)
        self.assertFalse(reached_r_before_stop(current, 0.5, anchor + 3_000))

    def test_short_r_target_uses_ask(self):
        item = make_signal("SHORT")
        anchor = item.anchor_ms
        ticks = [Tick(anchor, 99.9, 100.1), Tick(anchor + 1_000, 99.0, 99.2)]
        simulate_adaptive_tick_order([item], ticks, progress_every=0)
        self.assertEqual(item.stops["CURRENT"].r_hit_ms[0.5], anchor + 1_000)

    def test_tick_before_anchor_cannot_hit_stop_or_r(self):
        item = make_signal("LONG")
        simulate_adaptive_tick_order(
            [item],
            [Tick(item.anchor_ms - 1, 90.0, 90.2), Tick(item.anchor_ms, 99.9, 100.1)],
            progress_every=0,
        )
        self.assertEqual(item.entry_tick_ms, item.anchor_ms)
        self.assertTrue(all(stop.hit_ms is None and not stop.r_hit_ms for stop in item.stops.values()))

    def test_split_remains_sixty_twenty_twenty(self):
        observations = [
            {"event_id": f"event-{index}", "_timestamp_ms": index, "timestamp": index}
            for index in range(10)
        ]
        _, counts = temporal_split(observations)
        self.assertEqual(counts, {"TRAIN": 6, "VALIDATION": 2, "OOS": 2})

    def test_robustness_requires_out_of_sample_benefit_without_clear_degradation(self):
        rows = []
        for period in ("TRAIN", "VALIDATION", "OOS"):
            rows.extend(
                [
                    {
                        "period": period,
                        "direction": "ALL",
                        "candidate": "CURRENT",
                        "horizon": "4h",
                        "survival_rate": 0.80,
                        "premature_stop_rate": 0.02,
                        "reached_1r_rate": 0.20,
                        "median_stop_distance_atr": 1.8,
                    },
                    {
                        "period": period,
                        "direction": "ALL",
                        "candidate": "FLOOR_1_5",
                        "horizon": "4h",
                        "survival_rate": 0.81,
                        "premature_stop_rate": 0.018,
                        "reached_1r_rate": 0.19,
                        "median_stop_distance_atr": 2.00002,
                    },
                ]
            )
        assessments = assess_variants(rows, ("direction",))
        self.assertEqual(assessments[("ALL", "FLOOR_1_5")], "ROBUST")

    def test_live_ea_is_unchanged(self):
        source = (
            Path(__file__).resolve().parents[1] / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
        ).read_text(encoding="utf-8-sig")
        self.assertRegex(source, r"input\s+bool\s+EnableLiveTrading\s*=\s*false;")
        self.assertRegex(source, r"input\s+double\s+RiskPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+int\s+ArmScoreThreshold\s*=\s*58;")
        self.assertRegex(source, r"input\s+int\s+MinScoreToTrade\s*=\s*74;")


if __name__ == "__main__":
    unittest.main()
