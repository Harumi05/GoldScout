"""Deterministic tests for the research-only structure-aware TP engine."""

from __future__ import annotations

from pathlib import Path
import unittest

from research.analyze_stop_loss_quality import M1RangeIndex, SignalState, StopState
from research.analyze_structure_aware_tp import (
    LevelEvidence,
    StructuralContext,
    StructuralZone,
    StructureAwareSignal,
    aggregate_structure_tp,
    build_structure_aware_targets,
    choose_diagnostic_policy,
    cluster_level_evidence,
    last_closed_index,
    structure_examples,
    structure_tp_detail_rows,
    summarize_structure_tp,
)
from research.tick_historical_replay import Tick
from research.analyze_take_profit_and_account_size import simulate_tp_tick_order


ANCHOR = 1_700_000_000_000


def zone(kind: str, price: float, *, major: bool = True, strength: int = 4) -> StructuralZone:
    return StructuralZone(kind, price, 2 if major else 1, strength, major, ("TEST",), ANCHOR - 2, ANCHOR - 1)


def item(
    direction: str = "LONG",
    trade_class: str = "CHILL",
    *,
    zones: tuple[StructuralZone, ...] = (),
    momentum: bool = False,
    breakout: bool = False,
    setup: str = "CONTINUATION_LONG",
    projection: float | None = None,
) -> StructureAwareSignal:
    base = SignalState(
        event_id=f"structure-tp-{direction}-{trade_class}-{setup}",
        anchor_ms=ANCHOR,
        period="OOS",
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
        pullback=setup == "PULLBACK",
        recovery=setup == "RECOVERY",
        session=None,
        spread=0.2,
        long_score=80.0,
        short_score=80.0,
        source_timeframe="H1",
    )
    context = StructuralContext(
        "BULLISH" if direction == "LONG" else "BEARISH",
        "BULLISH" if direction == "LONG" else "BEARISH",
        zones,
        projection,
        ANCHOR,
    )
    return StructureAwareSignal(base, trade_class, 1.25 if trade_class == "CHILL" else 2.0, context=context)


class StructureAwareTakeProfitTests(unittest.TestCase):
    def test_open_bar_is_never_selected_as_closed_context(self):
        self.assertEqual(last_closed_index([100, 200, 300], 250), 1)
        self.assertEqual(last_closed_index([100, 200, 300], 300), 2)

    def test_equal_highs_merge_into_major_resistance(self):
        evidence = [
            LevelEvidence("HIGH", 102.00, 100, "H1", "PIVOT", 2),
            LevelEvidence("HIGH", 102.10, 200, "H1", "PIVOT", 2),
        ]
        result = cluster_level_evidence(evidence, 1.0)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].major)
        self.assertIn("EQUAL_HIGH", result[0].sources)

    def test_opposite_level_types_never_merge(self):
        evidence = [
            LevelEvidence("HIGH", 102.0, 100, "H1", "HIGH", 3),
            LevelEvidence("LOW", 102.0, 200, "H1", "LOW", 3),
        ]
        self.assertEqual(len(cluster_level_evidence(evidence, 1.0)), 2)

    def test_invalid_atr_fails_closed_without_zones(self):
        evidence = [LevelEvidence("HIGH", 102.0, 100, "H1", "HIGH", 3)]
        self.assertEqual(cluster_level_evidence(evidence, 0.0), ())

    def test_chill_long_selects_near_structure_above_minimum_rr(self):
        signal = item(zones=(zone("HIGH", 102.4),))
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, risk, targets = build_structure_aware_targets(signal, 100.1, stop)
        self.assertGreaterEqual(targets["STRUCTURE_AWARE"].r_multiple, 1.25)
        self.assertEqual(signal.selected_structural_candidate, "TP_STRUCTURAL_NEAR")
        self.assertLess(targets["STRUCTURE_AWARE"].price, 102.4)
        self.assertGreater(risk, 0.0)

    def test_short_targets_support_from_above(self):
        signal = item("SHORT", zones=(zone("LOW", 97.5),), setup="CONTINUATION_SHORT")
        stop = StopState("CURRENT", 101.1, 1.2, 1.2)
        _, _, targets = build_structure_aware_targets(signal, 99.9, stop)
        self.assertGreater(targets["STRUCTURE_AWARE"].price, 97.5)
        self.assertLess(targets["STRUCTURE_AWARE"].price, 99.9)

    def test_major_obstacle_below_minimum_marks_poor_reward(self):
        signal = item(zones=(zone("HIGH", 100.8), zone("HIGH", 103.0)))
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_structure_aware_targets(signal, 100.1, stop)
        self.assertNotIn("STRUCTURE_AWARE", targets)
        self.assertEqual(signal.structural_rejection, "POOR_REWARD")

    def test_never_selects_target_beyond_first_major_obstacle(self):
        signal = item(
            "LONG", "GOD", zones=(zone("HIGH", 102.4), zone("HIGH", 104.0)),
            momentum=True, breakout=True, setup="BREAKOUT", projection=103.5,
        )
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_structure_aware_targets(signal, 100.1, stop)
        selected = targets.get("STRUCTURE_AWARE")
        self.assertIsNotNone(selected)
        obstacle_target = signal.first_major_obstacle.price - 0.05
        self.assertLessEqual(selected.price, obstacle_target)
        self.assertNotIn("TP_STRUCTURAL_EXTENDED", signal.structural_candidates)

    def test_god_without_strong_continuation_uses_near_not_extension(self):
        signal = item("LONG", "GOD", zones=(zone("HIGH", 102.4),), projection=103.0)
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_structure_aware_targets(signal, 100.1, stop)
        self.assertEqual(signal.selected_structural_candidate, "TP_STRUCTURAL_NEAR")
        self.assertNotIn("TP_STRUCTURAL_EXTENDED", targets)

    def test_god_extension_requires_momentum_breakout_and_no_obstacle(self):
        signal = item(
            "LONG", "GOD", zones=(zone("HIGH", 101.0, major=False, strength=1),),
            momentum=True, breakout=True, setup="BREAKOUT", projection=103.0,
        )
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_structure_aware_targets(signal, 100.1, stop)
        self.assertEqual(signal.selected_structural_candidate, "TP_STRUCTURAL_EXTENDED")
        self.assertEqual(targets["STRUCTURE_AWARE"].price, targets["TP_STRUCTURAL_EXTENDED"].price)

    def test_current_crossing_major_resistance_is_diagnosed(self):
        signal = item(zones=(zone("HIGH", 101.8),))
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        signal.entry_price = 100.1
        signal.stop = stop
        build_structure_aware_targets(signal, 100.1, stop)
        self.assertTrue(signal.current_crosses_major_sr)
        examples = structure_examples([signal])
        self.assertTrue(examples[0]["avoided_target_beyond_sr"])
        self.assertEqual(examples[0]["score_effect"], 0)

    def test_tick_order_uses_same_current_stop_and_bid_for_long_tp(self):
        signal = item(zones=(zone("HIGH", 102.4),))
        ticks = [
            Tick(ANCHOR, 99.9, 100.1),
            Tick(ANCHOR + 1_000, 102.36, 102.56),
            Tick(ANCHOR + 2_000, 98.7, 98.9),
        ]
        simulate_tp_tick_order([signal], ticks, progress_every=0, target_builder=build_structure_aware_targets)
        self.assertIsNotNone(signal.targets["STRUCTURE_AWARE"].hit_ms)
        self.assertEqual(signal.stop.hit_ms, ANCHOR + 2_000)

    def test_rejected_structure_policy_is_no_trade_zero_r(self):
        signal = item(zones=(zone("HIGH", 100.8),))
        simulate_tp_tick_order([signal], [Tick(ANCHOR, 99.9, 100.1)], progress_every=0, target_builder=build_structure_aware_targets)
        ranges = M1RangeIndex([{"timestamp": ANCHOR // 60_000 * 60_000, "high": 100.0, "low": 100.0}])
        rows = structure_tp_detail_rows([signal], {}, ranges)
        row = next(row for row in rows if row["candidate"] == "STRUCTURE_AWARE" and row["horizon"] == "4h")
        self.assertFalse(row["eligible"])
        self.assertTrue(row["poor_reward"])
        self.assertEqual(row["hypothetical_realized_r"], 0.0)

    def test_summary_reports_expectancy_profit_factor_and_rejection(self):
        rows = [
            {"eligible": True, "poor_reward": False, "tp_hit": True, "sl_hit": False, "tp_first": True, "sl_first": False, "hypothetical_realized_r": 1.5, "time_to_tp_minutes": 10.0, "mfe_r": 2.0, "mae_r": 0.2, "mfe_after_close_r": 0.5, "current_crosses_major_sr": True},
            {"eligible": True, "poor_reward": False, "tp_hit": False, "sl_hit": True, "tp_first": False, "sl_first": True, "hypothetical_realized_r": -1.0, "time_to_tp_minutes": None, "mfe_r": 0.5, "mae_r": 1.0, "mfe_after_close_r": None, "current_crosses_major_sr": False},
            {"eligible": False, "poor_reward": True, "tp_hit": False, "sl_hit": False, "tp_first": False, "sl_first": False, "hypothetical_realized_r": 0.0, "time_to_tp_minutes": None, "mfe_r": None, "mae_r": None, "mfe_after_close_r": None, "current_crosses_major_sr": True},
        ]
        summary = summarize_structure_tp(rows)
        self.assertAlmostEqual(summary["expectancy_r_resolved"], 0.25)
        self.assertAlmostEqual(summary["expectancy_r_all_cases"], 1.0 / 6.0)
        self.assertAlmostEqual(summary["profit_factor_resolved"], 1.5)
        self.assertAlmostEqual(summary["poor_reward_rate"], 1.0 / 3.0)

    def test_low_coverage_policy_cannot_be_selected_as_best(self):
        rows = []
        for period in ("VALIDATION", "OOS"):
            for candidate, expectancy, acceptance in (
                ("CURRENT", -0.10, 1.0),
                ("R_1_25", -0.02, 1.0),
                ("STRUCTURE_AWARE", 0.01, 0.05),
            ):
                rows.append(
                    {
                        "scope": "GLOBAL", "period": period, "trade_class": "GOD", "horizon": "4h",
                        "candidate": candidate, "expectancy_r_all_cases": expectancy, "acceptance_rate": acceptance,
                    }
                )
        self.assertEqual(choose_diagnostic_policy(rows, "GOD")[0], "R_1_25")

    def test_aggregation_preserves_period_direction_setup_and_zero_effect(self):
        row = {
            "event_id": "e", "timestamp": ANCHOR, "period": "OOS", "trade_class": "CHILL", "direction": "LONG",
            "setup": "BREAKOUT", "candidate": "CURRENT", "selected_structural_candidate": None, "target_r": 1.25,
            "horizon": "4h", "eligible": True, "rejection_reason": None, "poor_reward": False, "tp_hit": True,
            "sl_hit": False, "tp_first": True, "sl_first": False, "unresolved": False, "time_to_tp_minutes": 10.0,
            "hypothetical_realized_r": 1.25, "mfe_r": 1.4, "mae_r": 0.2, "mfe_after_close_r": 0.1,
            "current_crosses_major_sr": False, "observer_only": True, "diagnostic_only": True, "score_effect": 0,
        }
        output = aggregate_structure_tp([row])
        segment = next(result for result in output if result["scope"] == "SETUP" and result["period"] == "OOS")
        self.assertEqual((segment["direction"], segment["setup"]), ("LONG", "BREAKOUT"))
        self.assertEqual(segment["score_effect"], 0)

    def test_live_ea_risk_scoring_and_take_profit_are_untouched(self):
        source = (Path(__file__).resolve().parents[1] / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8-sig")
        self.assertRegex(source, r"input\s+bool\s+EnableLiveTrading\s*=\s*false;")
        self.assertRegex(source, r"input\s+double\s+RiskPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+double\s+DailyLossLimitPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+int\s+ArmScoreThreshold\s*=\s*58;")
        self.assertRegex(source, r"input\s+int\s+MinScoreToTrade\s*=\s*74;")


if __name__ == "__main__":
    unittest.main()
