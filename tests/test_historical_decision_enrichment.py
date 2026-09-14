"""Deterministic tests for causal GoldScout decision replay enrichment."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.analyze_historical_dataset import analyze_dataset
from research.enrich_historical_decisions import (
    THRESHOLD_ARM,
    THRESHOLD_TRADE,
    TIMEFRAME_SECONDS,
    build_state_points,
    enrich_historical_decisions,
    h1_regime,
    m15_timing_adjustment,
    session_context,
    structural_bucket,
)


BASE_TIMESTAMP = int(datetime(2025, 1, 6, tzinfo=timezone.utc).timestamp() * 1000)


def bar(timeframe: str, index: int, *, start: int = BASE_TIMESTAMP) -> dict:
    duration = TIMEFRAME_SECONDS[timeframe] * 1000
    price = 2_600.0 + index * 0.1
    return {
        "bar_id": f"HISTORICAL_MT5_TICKS-XAUUSD-{timeframe}-{start + index * duration}",
        "source": "HISTORICAL_MT5_TICKS",
        "symbol": "XAUUSD",
        "timeframe": timeframe,
        "timestamp": start + index * duration,
        "close_timestamp": start + (index + 1) * duration,
        "timestamp_unit": "epoch_ms_mt5_server_wall_time",
        "open": price - 0.04,
        "high": price + 0.12,
        "low": price - 0.12,
        "close": price,
        "tick_count": 1_000 + index % 17,
        "avg_spread": 0.3,
        "min_spread": 0.3,
        "max_spread": 0.3,
    }


def write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def make_replay_input(root: Path) -> tuple[Path, dict]:
    input_dir = root / "output"
    counts = {"M15": 3_520, "H1": 880, "H4": 220}
    for timeframe, count in counts.items():
        write_jsonl(
            input_dir / "historical_bars" / f"XAUUSD_{timeframe}.jsonl",
            [bar(timeframe, index) for index in range(count)],
        )
    observed_at = BASE_TIMESTAMP + counts["H4"] * TIMEFRAME_SECONDS["H4"] * 1000
    observation = {
        "event_id": "historical-observation-1",
        "timestamp": observed_at - TIMEFRAME_SECONDS["H1"] * 1000,
        "observed_at": observed_at,
        "symbol": "XAUUSD",
        "timeframe": "H1",
        "open": 2_600.0,
        "high": 2_601.0,
        "low": 2_599.0,
        "close": 2_600.5,
        "atr": 1.0,
        "source": "HISTORICAL_MT5_TICKS",
        "observer_only": True,
        "score_effect": 0,
    }
    write_jsonl(input_dir / "historical_observations.jsonl", [observation])
    return input_dir, observation


class HistoricalDecisionEnrichmentTests(unittest.TestCase):
    def test_structural_bucket_matches_existing_cap_and_neutral_policy(self):
        self.assertEqual(structural_bucket("BULLISH", 1, False, False, False), 15)
        self.assertEqual(structural_bucket("BULLISH", 1, True, True, True), 25)
        self.assertEqual(structural_bucket("BEARISH", -1, True, False, True), 25)
        self.assertEqual(structural_bucket("NEUTRAL", 1, True, True, True), 0)
        self.assertEqual(structural_bucket("BULLISH", -1, True, True, True), 0)

    def test_m15_policy_matches_soft_bounded_confirmation(self):
        self.assertEqual(m15_timing_adjustment("BULLISH", 1, 1, 0, 1), 4)
        self.assertEqual(m15_timing_adjustment("BULLISH", -1, 1, 0, 1), -4)
        self.assertEqual(m15_timing_adjustment("BULLISH", -1, 0, 0, 0), -3)
        self.assertEqual(m15_timing_adjustment("NEUTRAL", 1, 1, 0, 1), 0)
        for structure in ("BULLISH", "BEARISH", "NEUTRAL", "INSUFFICIENT"):
            for direction in (-1, 1):
                value = m15_timing_adjustment(structure, direction, 1, -1, 1)
                self.assertGreaterEqual(value, -4)
                self.assertLessEqual(value, 4)

    def test_session_is_unavailable_without_server_offset_and_matches_dst_windows_with_it(self):
        wall = int(datetime(2025, 7, 1, 14, 0, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertFalse(session_context(wall, None)["available"])
        context = session_context(wall, 2.0)
        self.assertEqual(context["name"], "LONDRES")
        self.assertEqual(context["points"], 1)
        self.assertIn("LONDRES", context["active_centers"])

    def test_h1_regime_has_30_10_5_3_windows_and_no_score_effect(self):
        points = build_state_points([bar("H1", index) for index in range(50)], 0.001)
        regime = h1_regime(points, 49, 0.001)
        self.assertTrue(regime["available"])
        self.assertEqual(regime["score_effect"], 0)
        self.assertGreater(regime["window_30"]["slope_atr_per_bar"], 0.0)
        self.assertGreater(regime["window_10"]["net_displacement_atr"], 0.0)
        self.assertEqual(regime["window_5"]["bars"], 5)
        self.assertEqual(regime["window_3"]["bars"], 3)
        self.assertEqual(regime["ema20_relation"], "ABOVE")

    def test_enrichment_is_causal_idempotent_and_preserves_original_observations(self):
        with tempfile.TemporaryDirectory() as temp:
            input_dir, _ = make_replay_input(Path(temp))
            observations_path = input_dir / "historical_observations.jsonl"
            before = hashlib.sha256(observations_path.read_bytes()).hexdigest()
            output = input_dir / "historical_decisions.jsonl"
            first = enrich_historical_decisions(input_dir, output_path=output)
            first_record = read_jsonl(output)[0]

            for timeframe in ("M15", "H1", "H4"):
                path = input_dir / "historical_bars" / f"XAUUSD_{timeframe}.jsonl"
                existing = read_jsonl(path)
                existing.append(bar(timeframe, len(existing)))
                write_jsonl(path, existing)
            second = enrich_historical_decisions(input_dir, output_path=output)
            second_record = read_jsonl(output)[0]

            after = hashlib.sha256(observations_path.read_bytes()).hexdigest()
        self.assertEqual(first["written"], 1)
        self.assertEqual(second["written"], 0)
        self.assertEqual(second["deduplicated"], 1)
        self.assertEqual(first_record, second_record)
        self.assertEqual(before, after)
        self.assertTrue(first_record["observer_only"])
        self.assertEqual(first_record["score_effect"], 0)
        self.assertEqual(first_record["threshold_arm"], 58)
        self.assertEqual(first_record["threshold_trade"], 74)
        self.assertIn(first_record["decision"], {"NO_TRADE", "ARMED"})
        self.assertIn(first_record["armed_direction"], {None, "LONG", "SHORT"})
        self.assertEqual(first_record["score_reproduction"]["status"], "PARTIAL_EXACT_CLOSED_BAR_CORE")
        self.assertTrue(first_record["score_reproduction"]["patterns_assumed_zero"])
        self.assertTrue(first_record["score_reproduction"]["news_assumed_zero"])
        self.assertTrue(first_record["score_reproduction"]["intrabar_assumed_zero"])

    def test_only_m15_h1_h4_are_replay_timeframes(self):
        self.assertEqual(set(TIMEFRAME_SECONDS), {"M15", "H1", "H4"})

    def test_live_defaults_and_thresholds_remain_the_source_values(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8-sig")
        self.assertRegex(source, r"input\s+bool\s+EnableLiveTrading\s*=\s*false;")
        self.assertRegex(source, r"input\s+double\s+RiskPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+int\s+ArmScoreThreshold\s*=\s*58;")
        self.assertRegex(source, r"input\s+int\s+MinScoreToTrade\s*=\s*74;")
        self.assertEqual((THRESHOLD_ARM, THRESHOLD_TRADE), (58, 74))

    def test_analyzer_merges_sidecar_and_keeps_train_fitted_regime_buckets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            input_dir = root / "input"
            observations = []
            outcomes = []
            decisions = []
            for index in range(10):
                timestamp = BASE_TIMESTAMP + index * 900_000
                event_id = f"event-{index}"
                observations.append(
                    {
                        "event_id": event_id,
                        "timestamp": timestamp,
                        "timeframe": "M15",
                        "close": 100.0,
                        "atr": 1.0,
                        "spread": 0.3,
                        "structure": "NEUTRAL",
                        "source": "HISTORICAL_MT5_TICKS",
                        "observer_only": True,
                        "score_effect": 0,
                    }
                )
                outcomes.append(
                    {
                        "event_id": event_id,
                        "completed_horizon": "1h",
                        "future_return_1h": 0.01,
                        "mfe_1h": 0.02,
                        "mae_1h": -0.01,
                    }
                )
                slope = float(index if index < 6 else 100 + index)
                decisions.append(
                    {
                        "event_id": event_id,
                        "timestamp": timestamp,
                        "observer_only": True,
                        "score_effect": 0,
                        "long_score": 60 + index,
                        "short_score": 30,
                        "armed_direction": "LONG",
                        "decision": "NO_TRADE",
                        "decision_reason": "diagnostic partial replay",
                        "session": "LONDRES",
                        "score_reproduction": {"status": "PARTIAL_EXACT_CLOSED_BAR_CORE"},
                        "h1_regime": {
                            "available": True,
                            "score_effect": 0,
                            "behavior_shift": "NONE",
                            "recent_acceleration": slope,
                            "window_30": {"slope_atr_per_bar": slope},
                            "window_10": {"slope_atr_per_bar": slope},
                            "window_3": {"slope_atr_per_bar": slope},
                        },
                    }
                )
            write_jsonl(input_dir / "historical_observations.jsonl", observations)
            write_jsonl(input_dir / "historical_outcomes.jsonl", outcomes)
            write_jsonl(input_dir / "historical_decisions.jsonl", decisions)
            summary = analyze_dataset(input_dir, root / "analysis", min_group_size=1)
        self.assertEqual(summary["general"]["split_counts"], {"TRAIN": 6, "VALIDATION": 2, "OOS": 2})
        self.assertEqual(summary["decision_enrichment"]["matched"], 10)
        self.assertEqual(summary["field_availability"]["direction"], 10)
        self.assertTrue(summary["score_analysis_available"])
        self.assertTrue(summary["missed_opportunities"]["available"])
        regime = summary["h1_regime_analysis"]
        self.assertEqual(regime["enriched_observations"], 10)
        self.assertLess(regime["numeric_features"]["h1_30_slope_atr_per_bar"]["thresholds_from_train"]["q75"], 6.0)


if __name__ == "__main__":
    unittest.main()
