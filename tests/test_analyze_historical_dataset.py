"""Deterministic tests for the offline historical dataset analyzer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from research.analyze_historical_dataset import (
    _group_analysis,
    _load_observations,
    _load_outcomes,
    _missed_opportunities,
    _score_rows,
    _stale_bias,
    analyze_dataset,
    temporal_split,
)


def observation(index: int, **overrides) -> dict:
    value = {
        "event_id": f"event-{index}",
        "timestamp": 1_700_000_000_000 + index * 900_000,
        "timeframe": "M15",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "atr": 2.0,
        "spread": 0.4,
        "structure": "NEUTRAL",
        "momentum": "NONE",
        "breakout": "NONE",
        "pullback": "NONE",
        "recovery": "NONE",
        "source": "HISTORICAL_MT5_TICKS",
        "observer_only": True,
        "score_effect": 0,
    }
    value.update(overrides)
    return value


def outcome(event_id: str, horizon: str, future_return: float | None, mfe=0.02, mae=-0.01, **overrides) -> dict:
    value = {
        "event_id": event_id,
        "completed_horizon": horizon,
        "evaluated_at": 1_700_100_000_000,
        f"future_return_{horizon}": future_return,
        f"mfe_{horizon}": mfe if future_return is not None else None,
        f"mae_{horizon}": mae if future_return is not None else None,
        "source": "HISTORICAL_MT5_TICKS",
        "observer_only": True,
        "score_effect": 0,
    }
    value.update(overrides)
    return value


def write_jsonl(path: Path, values: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(value) + "\n" for value in values), encoding="utf-8")


def write_dataset(root: Path, observations: list[dict], outcomes: list[dict]) -> None:
    write_jsonl(root / "historical_observations.jsonl", observations)
    write_jsonl(root / "historical_outcomes.jsonl", outcomes)


def mapped_observations(values: list[dict]) -> list[dict]:
    return [{**value, "_timestamp_ms": int(value["timestamp"])} for value in values]


def mapped_outcomes(values: list[dict]) -> dict:
    result: dict[str, dict[str, dict]] = {}
    for value in values:
        horizon = value["completed_horizon"]
        result.setdefault(value["event_id"], {})[horizon] = {
            "future_return": value[f"future_return_{horizon}"],
            "mfe": value[f"mfe_{horizon}"],
            "mae": value[f"mae_{horizon}"],
        }
    return result


class HistoricalAnalysisTests(unittest.TestCase):
    def test_observation_becomes_available_at_bar_close_not_bar_open(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "observations.jsonl"
            write_jsonl(
                path,
                [observation(0, timestamp=1_700_000_000_000, observed_at=1_700_000_900_000)],
            )
            rows, stats = _load_observations(path)
        self.assertEqual(stats["invalid"], 0)
        self.assertEqual(rows[0]["_timestamp_ms"], 1_700_000_900_000)

    def test_join_by_event_id_and_excludes_null_gap_incomplete_and_missing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observations = [observation(0), observation(1)]
            outcomes = [
                outcome("event-0", "15m", 0.01),
                outcome("event-0", "1h", None),
                outcome("event-0", "4h", None, status="UNRESOLVABLE_GAP"),
                {
                    **outcome("event-1", "15m", 0.02),
                    "mae_15m": None,
                },
                outcome("orphan", "15m", 0.03),
            ]
            write_dataset(root / "input", observations, outcomes)
            summary = analyze_dataset(root / "input", root / "analysis", min_group_size=1)
        quality = summary["outcome_quality"]
        self.assertEqual(quality["valid_event_horizons"], 1)
        self.assertEqual(quality["excluded_event_horizons"], 5)
        self.assertEqual(quality["exclusion_reasons"]["NULL"], 1)
        self.assertEqual(quality["exclusion_reasons"]["UNRESOLVABLE_GAP"], 1)
        self.assertEqual(quality["exclusion_reasons"]["INCOMPLETE"], 1)
        self.assertEqual(quality["exclusion_reasons"]["MISSING"], 2)
        self.assertEqual(quality["orphan_valid_event_horizons"], 1)

    def test_temporal_split_is_ordered_sixty_twenty_twenty(self):
        values = mapped_observations([observation(index) for index in reversed(range(10))])
        assignments, counts = temporal_split(values)
        self.assertEqual(counts, {"TRAIN": 6, "VALIDATION": 2, "OOS": 2})
        self.assertTrue(all(assignments[f"event-{index}"] == "TRAIN" for index in range(6)))
        self.assertTrue(all(assignments[f"event-{index}"] == "VALIDATION" for index in range(6, 8)))
        self.assertTrue(all(assignments[f"event-{index}"] == "OOS" for index in range(8, 10)))

    def test_temporal_split_never_separates_equal_timestamps(self):
        values = mapped_observations([observation(index) for index in range(10)])
        values[5]["_timestamp_ms"] = values[6]["_timestamp_ms"]
        values[5]["timestamp"] = values[6]["timestamp"]
        assignments, _ = temporal_split(values)
        self.assertEqual(assignments["event-5"], assignments["event-6"])

    def test_long_and_short_returns_use_directional_convention(self):
        values = mapped_observations(
            [observation(0, direction="LONG"), observation(1, direction="SHORT")]
        )
        outcomes = mapped_outcomes([outcome("event-0", "1h", 0.02), outcome("event-1", "1h", 0.02)])
        assignments, _ = temporal_split(values)
        from research.analyze_historical_dataset import _horizon_analysis

        result = _horizon_analysis(values, outcomes, assignments)["ALL"]["1h"]
        self.assertAlmostEqual(result["LONG"]["mean_future_return"], 0.02)
        self.assertAlmostEqual(result["SHORT"]["mean_future_return"], -0.02)
        self.assertAlmostEqual(result["SHORT"]["mean_mfe"], 0.01)
        self.assertAlmostEqual(result["SHORT"]["mean_mae"], -0.02)

    def test_score_buckets_cover_boundaries_without_inference(self):
        values = mapped_observations(
            [
                observation(0, long_score=49.9),
                observation(1, long_score=58),
                observation(2, short_score=74),
                observation(3, short_score=90),
            ]
        )
        outcomes = mapped_outcomes([outcome(f"event-{index}", "1h", 0.01) for index in range(4)])
        assignments, _ = temporal_split(values)
        rows = _score_rows(values, outcomes, assignments)
        buckets = {(row["bucket"], row["direction"]) for row in rows if row["period"] == "ALL"}
        self.assertEqual(buckets, {("<50", "LONG"), ("58-64", "LONG"), ("74-79", "SHORT"), ("90+", "SHORT")})

    def test_missed_opportunity_requires_no_trade_atr_and_one_hour_move(self):
        values = mapped_observations(
            [
                observation(0, decision="NO_TRADE", close=100.0, atr=2.0),
                observation(1, decision="NO_TRADE", close=100.0, atr=2.0),
                observation(2, decision="TRADE_TAKEN", close=100.0, atr=2.0),
            ]
        )
        outcomes = mapped_outcomes(
            [outcome("event-0", "1h", 0.02), outcome("event-1", "1h", -0.02), outcome("event-2", "1h", 0.04)]
        )
        assignments, _ = temporal_split(values)
        rows, summary = _missed_opportunities(values, outcomes, assignments, 0.75)
        self.assertEqual(summary["cases"], 2)
        self.assertEqual({row["opportunity_direction"] for row in rows}, {"LONG", "SHORT"})
        self.assertEqual(summary["percentage_of_eligible"], 100.0)

    def test_stale_bias_records_one_episode_until_confirmed_change(self):
        values = mapped_observations(
            [
                observation(0, structure="BULLISH", close=100.0, atr=1.0),
                observation(1, structure="BULLISH", close=99.0, atr=1.0),
                observation(2, structure="BULLISH", close=98.0, atr=1.0),
                observation(3, structure="BEARISH", close=97.0, atr=1.0),
            ]
        )
        outcomes = mapped_outcomes([outcome("event-0", "1h", -0.02)])
        assignments, _ = temporal_split(values)
        rows, summary = _stale_bias(values, outcomes, assignments, 0.75)
        self.assertEqual(summary["cases"], 1)
        self.assertEqual(rows[0]["bias"], "LONG")
        self.assertEqual(rows[0]["bias_source"], "structure")
        self.assertAlmostEqual(rows[0]["adverse_atr"], 3.0)
        self.assertEqual(rows[0]["time_to_change_minutes"], 45.0)

    def test_neutral_structure_terminates_stale_bias_run(self):
        values = mapped_observations(
            [
                observation(0, structure="BULLISH", close=100.0, atr=1.0),
                observation(1, structure="NEUTRAL", close=99.0, atr=1.0),
                observation(2, structure="BULLISH", close=98.0, atr=1.0),
                observation(3, structure="BEARISH", close=97.0, atr=1.0),
            ]
        )
        outcomes = mapped_outcomes([outcome("event-0", "1h", -0.02), outcome("event-2", "1h", -0.02)])
        assignments, _ = temporal_split(values)
        rows, summary = _stale_bias(values, outcomes, assignments, 0.75)
        self.assertEqual(summary["cases"], 2)
        self.assertEqual(rows[0]["time_to_change_minutes"], 15.0)

    def test_groups_below_minimum_sample_are_not_reported(self):
        values = mapped_observations([observation(0, structure="BULLISH"), observation(1, structure="BEARISH")])
        outcomes = mapped_outcomes([outcome("event-0", "15m", 0.01), outcome("event-1", "15m", -0.01)])
        assignments, _ = temporal_split(values)
        grouped = _group_analysis(values, outcomes, assignments, lambda item: item["structure"], 2)
        self.assertEqual(grouped["ALL"], {})

    def test_only_applicable_csv_files_are_generated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observations = [observation(index) for index in range(5)]
            outcomes = [outcome(f"event-{index}", "15m", 0.01) for index in range(5)]
            write_dataset(root / "input", observations, outcomes)
            (root / "analysis").mkdir()
            for filename in ("by_score.csv", "by_session.csv", "missed_opportunities.csv", "stale_bias.csv"):
                (root / "analysis" / filename).write_text("stale\n", encoding="utf-8")
            summary = analyze_dataset(root / "input", root / "analysis", min_group_size=1)
            generated = {path.name for path in (root / "analysis").iterdir()}
        self.assertEqual(generated, {"summary.json", "summary.md"})
        self.assertFalse(summary["score_analysis_available"])
        self.assertIn("by_score.csv", summary["skipped_files"])
        self.assertIn("missed_opportunities.csv", summary["skipped_files"])

    def test_score_and_session_outputs_are_created_when_fields_exist(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            observations = [
                observation(index, direction="LONG", long_score=75, session="LONDON", decision="NO_TRADE")
                for index in range(5)
            ]
            outcomes = [outcome(f"event-{index}", "1h", 0.02) for index in range(5)]
            write_dataset(root / "input", observations, outcomes)
            summary = analyze_dataset(root / "input", root / "analysis", min_group_size=1)
            generated = {path.name for path in (root / "analysis").iterdir()}
        self.assertTrue(summary["score_analysis_available"])
        self.assertTrue({"by_score.csv", "by_session.csv", "missed_opportunities.csv"}.issubset(generated))

    def test_analysis_never_modifies_historical_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_dataset(
                root / "input",
                [observation(index) for index in range(10)],
                [outcome(f"event-{index}", "15m", 0.01) for index in range(10)],
            )
            paths = [root / "input" / "historical_observations.jsonl", root / "input" / "historical_outcomes.jsonl"]
            before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
            analyze_dataset(root / "input", root / "analysis", min_group_size=1)
            after = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.assertEqual(before, after)

    def test_outcome_loader_rejects_conflicting_terminal_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "outcomes.jsonl"
            write_jsonl(path, [outcome("event-0", "15m", 0.01), outcome("event-0", "15m", 0.02)])
            valid, excluded, stats = _load_outcomes(path)
        self.assertNotIn("15m", valid.get("event-0", {}))
        self.assertEqual(excluded[("event-0", "15m")], "CONFLICTING_TERMINAL_STATE")
        self.assertEqual(stats["conflicting_terminal_rows"], 1)


if __name__ == "__main__":
    unittest.main()
