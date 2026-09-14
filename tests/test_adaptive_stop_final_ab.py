"""Deterministic tests for the final conservative stop-loss A/B validation."""

from __future__ import annotations

from pathlib import Path
import unittest

from research.analyze_adaptive_stop_final_ab import (
    ADAPTIVE,
    build_final_ab_candidates,
    evaluate_paper_acceptance,
)
from research.analyze_adaptive_stop_v2 import (
    constant_risk_lot_multiplier,
    planned_risk_ratio,
    simulate_adaptive_tick_order,
)
from research.analyze_stop_loss_quality import SignalState
from research.tick_historical_replay import Tick


def make_signal(direction: str = "LONG", setup: str = "CONTINUATION") -> SignalState:
    return SignalState(
        event_id=f"final-ab-{direction}-{setup}", anchor_ms=1_700_000_000_000,
        period="TRAIN", direction=direction, setup=setup, reference_close=100.0,
        atr=1.0, recent_high=100.2, recent_low=99.8, swing_high=100.2,
        swing_low=99.8, structure="BULLISH" if direction == "LONG" else "BEARISH",
        behavior_shift="NONE", momentum=setup == "MOMENTUM",
        breakout=setup == "BREAKOUT", pullback=setup == "PULLBACK",
        recovery=setup == "RECOVERY", session=None, spread=0.2,
        long_score=75.0, short_score=75.0, source_timeframe="H1",
    )


def global_rows(*, adaptive_survival: float = 0.82, adaptive_premature: float = 0.01,
                adaptive_1r: float = 0.195, adaptive_2r: float = 0.095) -> list[dict]:
    rows = []
    for period in ("TRAIN", "VALIDATION", "OOS"):
        rows.extend([
            {"period": period, "direction": "ALL", "candidate": "CURRENT", "horizon": "4h",
             "survival_rate": 0.80, "premature_stop_rate": 0.02,
             "reached_1r_rate": 0.20, "reached_2r_rate": 0.10},
            {"period": period, "direction": "ALL", "candidate": ADAPTIVE, "horizon": "4h",
             "survival_rate": adaptive_survival, "premature_stop_rate": adaptive_premature,
             "reached_1r_rate": adaptive_1r, "reached_2r_rate": adaptive_2r},
        ])
    return rows


class AdaptiveStopFinalAbTests(unittest.TestCase):
    def test_current_is_preserved_for_breakout_momentum_short_and_recovery(self):
        cases = (("LONG", "BREAKOUT"), ("SHORT", "BREAKOUT"),
                 ("SHORT", "MOMENTUM"), ("LONG", "RECOVERY"), ("SHORT", "RECOVERY"))
        for direction, setup in cases:
            with self.subTest(direction=direction, setup=setup):
                stops = build_final_ab_candidates(make_signal(direction, setup), 100.1)
                self.assertAlmostEqual(stops[ADAPTIVE].distance, stops["CURRENT"].distance)

    def test_momentum_long_uses_two_atr_floor(self):
        stops = build_final_ab_candidates(make_signal("LONG", "MOMENTUM"), 100.1)
        self.assertAlmostEqual(stops[ADAPTIVE].distance_atr, 2.0)

    def test_continuation_and_pullback_use_one_point_five_atr_floor(self):
        cases = (("LONG", "CONTINUATION"), ("SHORT", "CONTINUATION"),
                 ("LONG", "PULLBACK"), ("SHORT", "PULLBACK"))
        for direction, setup in cases:
            with self.subTest(direction=direction, setup=setup):
                entry = 100.1 if direction == "LONG" else 99.9
                stops = build_final_ab_candidates(make_signal(direction, setup), entry)
                self.assertAlmostEqual(stops[ADAPTIVE].distance_atr, 1.5)

    def test_conservative_stop_never_shrinks_current(self):
        for direction in ("LONG", "SHORT"):
            for setup in ("BREAKOUT", "MOMENTUM", "CONTINUATION", "PULLBACK", "RECOVERY"):
                with self.subTest(direction=direction, setup=setup):
                    entry = 100.1 if direction == "LONG" else 99.9
                    stops = build_final_ab_candidates(make_signal(direction, setup), entry)
                    self.assertGreaterEqual(stops[ADAPTIVE].distance, stops["CURRENT"].distance)

    def test_inverse_lotage_keeps_planned_monetary_risk_constant(self):
        stops = build_final_ab_candidates(make_signal("LONG", "MOMENTUM"), 100.1)
        current = stops["CURRENT"].distance
        adaptive = stops[ADAPTIVE].distance
        self.assertAlmostEqual(constant_risk_lot_multiplier(current, adaptive), current / adaptive)
        self.assertLessEqual(constant_risk_lot_multiplier(current, adaptive), 1.0)
        self.assertAlmostEqual(planned_risk_ratio(current, adaptive), 1.0)

    def test_tick_order_and_no_look_ahead_apply_to_both_candidates(self):
        item = make_signal("LONG", "MOMENTUM")
        ticks = [Tick(item.anchor_ms - 1, 90.0, 90.2), Tick(item.anchor_ms, 99.9, 100.1),
                 Tick(item.anchor_ms + 1_000, 98.7, 98.9), Tick(item.anchor_ms + 2_000, 97.9, 98.1)]
        simulate_adaptive_tick_order([item], ticks, progress_every=0,
                                     candidate_builder=build_final_ab_candidates)
        self.assertEqual(item.entry_tick_ms, item.anchor_ms)
        self.assertEqual(item.stops["CURRENT"].hit_ms, item.anchor_ms + 1_000)
        self.assertEqual(item.stops[ADAPTIVE].hit_ms, item.anchor_ms + 2_000)

    def test_acceptance_requires_oos_benefit_and_constant_risk(self):
        details = [{"candidate": ADAPTIVE, "planned_monetary_risk_ratio_vs_current": 1.0,
                    "stop_distance_atr": 2.0}]
        result = evaluate_paper_acceptance(global_rows(), details)
        self.assertTrue(result["accepted_for_paper"])
        self.assertFalse(result["tp_reproducible"])

    def test_acceptance_rejects_material_oos_r_degradation(self):
        details = [{"candidate": ADAPTIVE, "planned_monetary_risk_ratio_vs_current": 1.0,
                    "stop_distance_atr": 2.0}]
        result = evaluate_paper_acceptance(global_rows(adaptive_1r=0.18, adaptive_2r=0.08), details)
        self.assertFalse(result["accepted_for_paper"])
        self.assertFalse(result["checks"]["oos_plus_1r_within_1pp"])

    def test_live_ea_settings_remain_intact(self):
        source = (Path(__file__).resolve().parents[1] / "MT5" / "Experts" /
                  "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8-sig")
        self.assertRegex(source, r"input\s+bool\s+EnableLiveTrading\s*=\s*false;")
        self.assertRegex(source, r"input\s+double\s+RiskPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+int\s+ArmScoreThreshold\s*=\s*58;")
        self.assertRegex(source, r"input\s+int\s+MinScoreToTrade\s*=\s*74;")


if __name__ == "__main__":
    unittest.main()
