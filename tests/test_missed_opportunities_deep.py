"""Deterministic tests for the offline missed-opportunity diagnosis."""

from __future__ import annotations

from pathlib import Path
import unittest

from research.analyze_historical_dataset import temporal_split
from research.analyze_missed_opportunities_deep import (
    ARM_THRESHOLD,
    TRADE_THRESHOLD,
    analysis_score_bucket,
    apply_stale_bias_crossover,
    build_no_trade_counterfactual_population,
    classify_dominant_cause,
    counterfactual_rows,
    distance_bucket,
    distance_to_threshold,
    score_components,
)


ROOT = Path(__file__).resolve().parents[1]
MODULE = (ROOT / "research" / "analyze_missed_opportunities_deep.py").read_text(encoding="utf-8")
EA = (ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8")


def observation(long_score: int = 40, short_score: int = 30) -> dict:
    return {
        "event_id": "HISTORICAL_MT5_TICKS-XAUUSD-H1-1000",
        "_timestamp_ms": 2000,
        "timestamp": 1000,
        "decision": "NO_TRADE",
        "long_score": long_score,
        "short_score": short_score,
        "long_technical_score": long_score,
        "short_technical_score": short_score,
        "h4_context": {"trend": "BEARISH"},
        "h1_structure": {
            "ema_trend": "BULLISH",
            "pivot_state": "BULLISH",
            "breakout_long": False,
            "pullback_long": False,
            "momentum_long": False,
            "structural_points_long_without_patterns": 15,
            "structural_points_short_without_patterns": 0,
        },
        "m15_timing": {"structure": "BULLISH", "long_adjustment": 0, "short_adjustment": -4},
    }


def missed_row(score: int, direction: str = "LONG", period: str = "TRAIN", timestamp: int = 1000) -> dict:
    return {
        "event_id": f"HISTORICAL_MT5_TICKS-XAUUSD-H1-{timestamp}",
        "timestamp": timestamp,
        "period": period,
        "timeframe": "H1",
        "direction": direction,
        "long_score": score if direction == "LONG" else 20,
        "short_score": score if direction == "SHORT" else 20,
        "relevant_score": float(score),
        "future_return_15m": 0.001,
        "mfe_15m": 0.002,
        "mae_15m": -0.001,
        "future_return_1h": 0.003,
        "mfe_1h": 0.004,
        "mae_1h": -0.002,
        "future_return_4h": 0.005,
        "mfe_4h": 0.006,
        "mae_4h": -0.003,
        "m15_state": "BULLISH" if direction == "LONG" else "BEARISH",
        "stale_bias_crossover": False,
        "m15_aligned_new_direction": False,
        "dominant_score_opposite": False,
    }


class MissedOpportunityDeepTests(unittest.TestCase):
    def test_distance_to_threshold_and_requested_buckets(self):
        self.assertEqual(distance_to_threshold(55, ARM_THRESHOLD), 3)
        self.assertEqual(distance_to_threshold(75, TRADE_THRESHOLD), 0)
        self.assertEqual(distance_bucket(4), "<5")
        self.assertEqual(distance_bucket(5), "5-9")
        self.assertEqual(distance_bucket(10), "10-14")
        self.assertEqual(distance_bucket(15), "15+")
        self.assertEqual(analysis_score_bucket(58), "58-64")
        self.assertEqual(analysis_score_bucket(69), "65-69")
        self.assertEqual(analysis_score_bucket(73), "70-73")

    def test_h4_conflict_is_dominant_when_it_has_largest_missing_weight(self):
        item = observation()
        components = score_components(item, "LONG")
        components.update({"adx": 0.0, "rsi": 0.0, "volume": 0.0})
        cause, contributing = classify_dominant_cause(item, "LONG", components)
        self.assertEqual(cause, "H4_CONFLICT")
        self.assertIn("ADX_FILTER", contributing)

    def test_tied_largest_causes_are_not_arbitrarily_selected(self):
        item = observation()
        item["h4_context"]["trend"] = "BULLISH"
        components = score_components(item, "LONG")
        components.update({"adx": 0.0, "rsi": 0.0, "volume": 5.0})
        cause, contributing = classify_dominant_cause(item, "LONG", components)
        self.assertEqual(cause, "MULTIPLE_CAUSES")
        self.assertIn("ADX_FILTER", contributing)
        self.assertIn("RSI_FILTER", contributing)

    def test_armed_below_trade_is_distinguished_from_no_arm(self):
        item = observation(long_score=70)
        components = score_components(item, "LONG")
        cause, _ = classify_dominant_cause(item, "LONG", components)
        self.assertEqual(cause, "SCORE_ARMED_BELOW_TRADE")

    def test_trade_threshold_counterfactual_does_not_rescue_sub_arm_rows(self):
        rows = [missed_row(57), missed_row(55, timestamp=2000)]
        output = counterfactual_rows(rows)
        all_one_hour = {
            (row["counterfactual"], row["threshold"]): row
            for row in output
            if row["period"] == "ALL" and row["direction"] == "ALL" and row["horizon"] == "1h"
        }
        for threshold in (74, 72, 70, 68):
            self.assertEqual(all_one_hour[("TRADE_THRESHOLD", threshold)]["selected_cases"], 0)
        self.assertEqual(all_one_hour[("ARM_THRESHOLD", 58)]["selected_cases"], 0)
        self.assertEqual(all_one_hour[("ARM_THRESHOLD", 56)]["selected_cases"], 1)
        self.assertEqual(all_one_hour[("ARM_THRESHOLD", 54)]["selected_cases"], 2)

    def test_counterfactual_direction_comes_from_score_not_future_return(self):
        item = observation(long_score=20, short_score=56)
        outcomes = {
            item["event_id"]: {
                "1h": {"future_return": 0.01, "mfe": 0.02, "mae": -0.005},
                "15m": {"future_return": 0.002, "mfe": 0.003, "mae": -0.001},
            }
        }
        rows = build_no_trade_counterfactual_population([item], outcomes, {item["event_id"]: "OOS"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["direction"], "SHORT")
        self.assertEqual(rows[0]["relevant_score"], 56)
        self.assertEqual(rows[0]["future_return_1h"], -0.01)

    def test_long_and_short_are_evaluated_independently(self):
        rows = [missed_row(56, "LONG"), missed_row(56, "SHORT", timestamp=2000)]
        output = counterfactual_rows(rows)
        selected = [
            row for row in output
            if row["counterfactual"] == "ARM_THRESHOLD" and row["threshold"] == 56
            and row["period"] == "ALL" and row["horizon"] == "1h"
        ]
        by_direction = {row["direction"]: row["selected_cases"] for row in selected}
        self.assertEqual(by_direction, {"ALL": 2, "LONG": 1, "SHORT": 1})

    def test_stale_bias_crossover_requires_opposite_stale_direction(self):
        rows = [missed_row(50, "LONG", timestamp=2000), missed_row(50, "SHORT", timestamp=2500)]
        episodes = [{"timeframe": "H1", "start": 1000, "end": 3000, "stale_direction": "SHORT"}]
        summary = apply_stale_bias_crossover(rows, episodes)
        self.assertEqual(summary["missed_during_opposite_stale_bias"], 1)
        self.assertTrue(rows[0]["stale_bias_crossover"])
        self.assertTrue(rows[0]["m15_aligned_new_direction"])
        self.assertFalse(rows[1]["stale_bias_crossover"])
        self.assertIsNone(summary["armed_opposite"])

    def test_temporal_split_is_sixty_twenty_twenty_without_reordering(self):
        values = [{"event_id": str(index), "_timestamp_ms": index} for index in range(100)]
        assignments, counts = temporal_split(values)
        self.assertEqual(counts, {"TRAIN": 60, "VALIDATION": 20, "OOS": 20})
        self.assertEqual(assignments["59"], "TRAIN")
        self.assertEqual(assignments["60"], "VALIDATION")
        self.assertEqual(assignments["80"], "OOS")

    def test_analysis_has_no_live_execution_surface(self):
        for forbidden in ("PositionOpen(", "OrderSend(", "RiskPercent=", "EnableLiveTrading="):
            self.assertNotIn(forbidden, MODULE)
        self.assertIn("input bool   EnableLiveTrading      = false;", EA)
        self.assertIn("input double RiskPercent             = 5.0;", EA)
        self.assertIn("input int    ArmScoreThreshold       = 58;", EA)
        self.assertIn("input int    MinScoreToTrade         = 74;", EA)


if __name__ == "__main__":
    unittest.main()
