"""Deterministic tests for Structure-Aware Take Profit v2 research."""

from __future__ import annotations

from pathlib import Path
import unittest

from research.analyze_stop_loss_quality import M1RangeIndex, SignalState, StopState
from research.analyze_structure_aware_tp import StructuralContext, StructuralZone, last_closed_index
from research.analyze_structure_aware_tp_v2 import (
    StructureAwareV2Signal,
    aggregate_v2,
    buffered_target,
    build_v2_targets,
    classify_zone_confidence,
    first_relevant_obstacle,
    select_v2_policy,
    summarize_v2,
    v2_candidate_name,
    v2_detail_rows,
)
from research.analyze_take_profit_and_account_size import simulate_tp_tick_order
from research.tick_historical_replay import Tick


ANCHOR = 1_700_000_000_000


def zone(kind: str, price: float, source: str, *, touches: int = 1, strength: int = 1) -> StructuralZone:
    return StructuralZone(kind, price, touches, strength, strength >= 3, (source,), ANCHOR - 2, ANCHOR - 1)


def signal(direction: str = "LONG", trade_class: str = "CHILL", zones=()) -> StructureAwareV2Signal:
    base = SignalState(
        event_id=f"v2-{direction}-{trade_class}", anchor_ms=ANCHOR, period="OOS", direction=direction,
        setup=f"CONTINUATION_{direction}", reference_close=100.0, atr=1.0,
        recent_high=100.2, recent_low=99.8, swing_high=100.2, swing_low=99.8,
        structure="BULLISH" if direction == "LONG" else "BEARISH", behavior_shift="NONE",
        momentum=False, breakout=False, pullback=False, recovery=False, session=None, spread=0.2,
        long_score=80.0, short_score=80.0, source_timeframe="H1",
    )
    context = StructuralContext(base.structure, base.structure, tuple(zones), None, ANCHOR)
    return StructureAwareV2Signal(base, trade_class, 1.25 if trade_class == "CHILL" else 2.0, context=context)


class StructureAwareTakeProfitV2Tests(unittest.TestCase):
    def test_confidence_priority_high_medium_low(self):
        self.assertEqual(classify_zone_confidence(zone("HIGH", 101.0, "CONFIRMED_H4_PIVOT_HIGH", strength=4)), "HIGH")
        self.assertEqual(classify_zone_confidence(zone("HIGH", 101.0, "EQUAL_HIGH", touches=2)), "HIGH")
        self.assertEqual(classify_zone_confidence(zone("HIGH", 101.0, "CONFIRMED_H1_PIVOT_HIGH", strength=3)), "MEDIUM")
        self.assertEqual(classify_zone_confidence(zone("HIGH", 101.0, "CONFIRMED_M15_PIVOT_HIGH")), "LOW")

    def test_long_uses_first_resistance_between_entry_and_baseline(self):
        levels = [
            zone("HIGH", 101.1, "CONFIRMED_H1_PIVOT_HIGH", strength=3),
            zone("HIGH", 100.8, "CONFIRMED_H1_PIVOT_HIGH", strength=3),
            zone("LOW", 100.5, "CONFIRMED_H1_PIVOT_LOW", strength=3),
        ]
        found = first_relevant_obstacle(levels, "LONG", 100.1, 101.3)
        self.assertEqual(found[0].price, 100.8)

    def test_short_uses_first_support_between_entry_and_baseline(self):
        levels = [
            zone("LOW", 98.9, "CONFIRMED_H1_PIVOT_LOW", strength=3),
            zone("LOW", 99.2, "CONFIRMED_H1_PIVOT_LOW", strength=3),
        ]
        found = first_relevant_obstacle(levels, "SHORT", 99.9, 98.7)
        self.assertEqual(found[0].price, 99.2)

    def test_low_confidence_level_never_shortens_target(self):
        levels = [zone("HIGH", 100.8, "CONFIRMED_M15_PIVOT_HIGH")]
        self.assertIsNone(first_relevant_obstacle(levels, "LONG", 100.1, 101.3))

    def test_buffer_atr_is_applied_before_resistance_and_support(self):
        self.assertAlmostEqual(buffered_target(obstacle_price=101.0, direction="LONG", atr=2.0, buffer_atr=0.10), 100.8)
        self.assertAlmostEqual(buffered_target(obstacle_price=99.0, direction="SHORT", atr=2.0, buffer_atr=0.20), 99.4)

    def test_all_requested_buffers_and_floors_exist(self):
        item = signal(zones=(zone("HIGH", 101.0, "CONFIRMED_H1_PIVOT_HIGH", strength=3),))
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_v2_targets(item, 100.1, stop)
        self.assertIn(v2_candidate_name(0.10, 0.50, "ACCEPT"), targets)
        self.assertIn(v2_candidate_name(0.20, 0.70, "ACCEPT"), targets)
        self.assertEqual(len(item.v2_meta), 18)

    def test_floor_050_accepts_and_060_rejects_same_low_reward_target(self):
        item = signal(zones=(zone("HIGH", 101.0, "CONFIRMED_H1_PIVOT_HIGH", strength=3),))
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_v2_targets(item, 100.1, stop)
        accepted = v2_candidate_name(0.10, 0.50, "REJECT")
        rejected = v2_candidate_name(0.10, 0.60, "REJECT")
        self.assertIn(accepted, targets)
        self.assertNotIn(rejected, targets)
        self.assertEqual(item.v2_meta[rejected].rejection_reason, "LOW_REWARD")

    def test_accept_branch_keeps_target_below_floor(self):
        item = signal(zones=(zone("HIGH", 100.8, "CONFIRMED_H1_PIVOT_HIGH", strength=3),))
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_v2_targets(item, 100.1, stop)
        candidate = v2_candidate_name(0.20, 0.70, "ACCEPT")
        self.assertIn(candidate, targets)
        self.assertLess(targets[candidate].r_multiple, 0.70)
        self.assertTrue(item.v2_meta[candidate].accepted_below_0_75r)

    def test_no_obstacle_preserves_class_baseline(self):
        chill, god = signal(), signal(trade_class="GOD")
        long_stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, chill_targets = build_v2_targets(chill, 100.1, long_stop)
        _, _, god_targets = build_v2_targets(god, 100.1, long_stop)
        self.assertEqual(chill_targets[v2_candidate_name(0.10, 0.70, "REJECT")].price, chill_targets["R_0_75"].price)
        self.assertEqual(god_targets[v2_candidate_name(0.20, 0.70, "REJECT")].price, god_targets["R_1_25"].price)

    def test_v2_never_moves_target_farther_than_baseline(self):
        item = signal(zones=(zone("HIGH", 101.0, "CONFIRMED_H1_PIVOT_HIGH", strength=3),))
        stop = StopState("CURRENT", 98.9, 1.2, 1.2)
        _, _, targets = build_v2_targets(item, 100.1, stop)
        baseline = targets["R_0_75"].price
        for name, target in targets.items():
            if name.startswith("STRUCTURE_AWARE_V2"):
                self.assertLessEqual(target.price, baseline)

    def test_tick_order_records_tp_before_later_sl(self):
        item = signal(zones=(zone("HIGH", 101.0, "CONFIRMED_H1_PIVOT_HIGH", strength=3),))
        ticks = [Tick(ANCHOR, 99.9, 100.1), Tick(ANCHOR + 1_000, 100.91, 101.11), Tick(ANCHOR + 2_000, 98.7, 98.9)]
        simulate_tp_tick_order([item], ticks, progress_every=0, target_builder=build_v2_targets)
        candidate = v2_candidate_name(0.10, 0.50, "ACCEPT")
        self.assertEqual(item.targets[candidate].hit_ms, ANCHOR + 1_000)
        self.assertEqual(item.stop.hit_ms, ANCHOR + 2_000)

    def test_tick_order_records_sl_before_later_tp(self):
        item = signal(zones=(zone("HIGH", 101.0, "CONFIRMED_H1_PIVOT_HIGH", strength=3),))
        ticks = [Tick(ANCHOR, 99.9, 100.1), Tick(ANCHOR + 1_000, 98.7, 98.9), Tick(ANCHOR + 2_000, 100.91, 101.11)]
        simulate_tp_tick_order([item], ticks, progress_every=0, target_builder=build_v2_targets)
        ranges = M1RangeIndex([{"timestamp": ANCHOR // 60_000 * 60_000, "high": 101.0, "low": 98.8}])
        rows = v2_detail_rows([item], {}, ranges)
        candidate = v2_candidate_name(0.10, 0.50, "ACCEPT")
        row = next(row for row in rows if row["candidate"] == candidate and row["horizon"] == "4h")
        self.assertTrue(row["sl_first"])
        self.assertFalse(row["tp_first"])

    def test_summary_has_expectancy_pf_variance_drawdown_and_rates(self):
        rows = [
            {"timestamp": ANCHOR, "event_id": "a", "eligible": True, "shortened_by_structure": True, "low_reward": True, "accepted_below_0_75r": True, "tp_first": True, "sl_first": False, "hypothetical_realized_r": 0.6, "time_to_tp_minutes": 10.0, "time_to_sl_minutes": None, "mfe_after_exit_r": 0.2},
            {"timestamp": ANCHOR + 1, "event_id": "b", "eligible": True, "shortened_by_structure": False, "low_reward": False, "accepted_below_0_75r": False, "tp_first": False, "sl_first": True, "hypothetical_realized_r": -1.0, "time_to_tp_minutes": None, "time_to_sl_minutes": 5.0, "mfe_after_exit_r": None},
        ]
        summary = summarize_v2(rows)
        self.assertAlmostEqual(summary["expectancy_r_resolved"], -0.2)
        self.assertAlmostEqual(summary["profit_factor_resolved"], 0.6)
        self.assertAlmostEqual(summary["structural_tp_rate"], 0.5)
        self.assertAlmostEqual(summary["drawdown_proxy_r"], 1.0)
        self.assertGreater(summary["realized_r_stddev"], 0.0)

    def test_aggregation_preserves_train_validation_oos_and_setup(self):
        base = {
            "event_id": "e", "timestamp": ANCHOR, "period": "VALIDATION", "trade_class": "CHILL", "direction": "SHORT",
            "setup": "PULLBACK", "candidate": "R_0_75", "horizon": "4h", "eligible": True,
            "shortened_by_structure": False, "low_reward": False, "accepted_below_0_75r": False,
            "tp_first": True, "sl_first": False, "hypothetical_realized_r": 0.75,
            "time_to_tp_minutes": 2.0, "time_to_sl_minutes": None, "mfe_after_exit_r": 0.1,
        }
        output = aggregate_v2([base])
        result = next(row for row in output if row["scope"] == "SETUP" and row["period"] == "VALIDATION")
        self.assertEqual((result["direction"], result["setup"]), ("SHORT", "PULLBACK"))
        self.assertEqual(result["score_effect"], 0)

    def test_accept_floor_tie_uses_050_as_canonical_not_false_winner(self):
        rows = []
        for period in ("TRAIN", "VALIDATION", "OOS"):
            for floor in (0.50, 0.70):
                rows.append(
                    {
                        "scope": "GLOBAL", "period": period, "trade_class": "GOD", "horizon": "4h",
                        "candidate": v2_candidate_name(0.20, floor, "ACCEPT"), "expectancy_r_resolved": 0.1,
                        "profit_factor_resolved": 1.2, "acceptance_rate": 0.9, "realized_r_stddev": 0.5,
                        "sl_before_tp_rate": 0.1,
                    }
                )
        self.assertEqual(select_v2_policy(rows, "GOD")[0], v2_candidate_name(0.20, 0.50, "ACCEPT"))

    def test_no_lookahead_excludes_bar_closing_after_observation(self):
        self.assertEqual(last_closed_index([ANCHOR - 1, ANCHOR + 1], ANCHOR), 0)

    def test_live_ea_is_untouched_and_safety_inputs_remain(self):
        source = (Path(__file__).resolve().parents[1] / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8-sig")
        self.assertRegex(source, r"input\s+bool\s+EnableLiveTrading\s*=\s*false;")
        self.assertRegex(source, r"input\s+double\s+RiskPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+double\s+DailyLossLimitPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+int\s+ArmScoreThreshold\s*=\s*58;")
        self.assertRegex(source, r"input\s+int\s+MinScoreToTrade\s*=\s*74;")


if __name__ == "__main__":
    unittest.main()
