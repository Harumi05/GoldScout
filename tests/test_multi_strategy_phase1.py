"""Deterministic phase-1 evidence/causality tests; no live EA imports."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from research.build_data_quality_manifest import build_manifest, validate_manifest
from research.multi_strategy_causal_bars import (
    DAY_MS, MINUTE_MS, CausalBar, ClosedM1, ClosedM1Aggregator, LookAheadError,
    aggregate_closed_m1, parse_broker_day_boundary, require_causal_feature,
)


ROOT = Path(__file__).resolve().parents[1]


def minute(index: int, *, ticks: int = 10, spread: float = 0.3, closed: bool = True,
           available_delay: int = 0) -> ClosedM1:
    start = index * MINUTE_MS
    return ClosedM1(
        start=start, end=start + MINUTE_MS, available_at=start + MINUTE_MS + available_delay,
        open=100.0 + index, high=101.0 + index, low=99.0 + index,
        close=100.5 + index, tick_count=ticks, avg_spread=spread,
        min_spread=spread, max_spread=spread, bar_id=f"M1-{index}", closed=closed,
    )


def record(bar: ClosedM1) -> dict:
    return {
        "bar_id": bar.bar_id, "timeframe": "M1", "timestamp": bar.start,
        "close_timestamp": bar.end, "open": bar.open, "high": bar.high,
        "low": bar.low, "close": bar.close, "tick_count": bar.tick_count,
        "avg_spread": bar.avg_spread, "min_spread": bar.min_spread,
        "max_spread": bar.max_spread,
    }


class CausalBarTests(unittest.TestCase):
    def test_available_at_and_closed_bar_gate_every_confirmation_feature(self):
        m1 = minute(0)
        for feature in ("pivot", "structure", "regime", "setup", "entry_quality"):
            with self.subTest(feature=feature):
                with self.assertRaises(LookAheadError):
                    require_causal_feature(m1, m1.end - 1, feature)
                require_causal_feature(m1, m1.end, feature)
                with self.assertRaises(LookAheadError):
                    require_causal_feature(minute(0, closed=False), m1.end, feature)
        with self.assertRaises(ValueError):
            require_causal_feature(m1, m1.end, "future_outcome")

    def test_m5_complete_ohlc_weighted_spread_and_late_availability(self):
        bars = [minute(index, ticks=index + 1, spread=0.1 * (index + 1)) for index in range(6)]
        result = list(aggregate_closed_m1(bars, "M5"))
        self.assertEqual(len(result), 1)
        value = result[0]
        self.assertEqual((value.start, value.end), (0, 5 * MINUTE_MS))
        self.assertEqual((value.open, value.close, value.high, value.low), (100.0, 104.5, 105.0, 99.0))
        self.assertEqual((value.observed_minutes, value.expected_minutes, value.coverage, value.complete), (5, 5, 1.0, True))
        self.assertEqual(value.tick_count, 15)
        self.assertAlmostEqual(value.avg_spread, sum((i + 1) * 0.1 * (i + 1) for i in range(5)) / 15)
        self.assertEqual(value.available_at, 6 * MINUTE_MS)
        self.assertEqual(value.provenance["first_bar_id"], "M1-0")
        self.assertEqual(value.provenance["source_bar_ids"], [f"M1-{i}" for i in range(5)])
        with self.assertRaises(LookAheadError):
            require_causal_feature(value, 5 * MINUTE_MS, "pivot")
        require_causal_feature(value, 6 * MINUTE_MS, "pivot")

    def test_m5_four_of_five_and_missing_middle_are_sparse_not_filled(self):
        result = list(aggregate_closed_m1((minute(index) for index in (0, 1, 3, 4, 5)), "M5"))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].observed_minutes, 4)
        self.assertEqual(result[0].expected_minutes, 5)
        self.assertEqual(result[0].coverage, 0.8)
        self.assertFalse(result[0].complete)
        self.assertEqual(result[0].tick_count, 40)
        self.assertEqual(result[0].provenance["last_bar_id"], "M1-4")

    def test_weekend_gap_does_not_create_empty_buckets(self):
        monday = 3 * DAY_MS // MINUTE_MS
        result = list(aggregate_closed_m1((minute(i) for i in (0, 1, 2, 3, 4, monday)), "M5"))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].start, 0)
        self.assertEqual(result[0].observed_minutes, 5)

    def test_final_unclosed_bucket_is_not_emitted(self):
        builder = ClosedM1Aggregator("M5")
        self.assertEqual([builder.push(minute(i)) for i in range(5)], [None] * 5)
        self.assertEqual(builder.pending_minutes, 5)
        self.assertEqual(list(aggregate_closed_m1((minute(i) for i in range(5)), "M5")), [])

    def test_d1_normal_day_scheduled_pause_short_sunday_and_missing_hours(self):
        next_day = DAY_MS // MINUTE_MS
        normal = list(aggregate_closed_m1((minute(i) for i in range(next_day + 1)), "D1", broker_day_boundary="00:00"))
        self.assertEqual(len(normal), 1)
        self.assertTrue(normal[0].complete)
        self.assertEqual(normal[0].observed_minutes, 1440)
        self.assertEqual(normal[0].d1_session_status, "UNVERIFIED")
        pause = list(aggregate_closed_m1((minute(i) for i in (*range(0, 500), *range(560, next_day + 1))),
                                         "D1", broker_day_boundary="00:00"))
        self.assertEqual(pause[0].observed_minutes, 1380)
        self.assertFalse(pause[0].complete)
        sunday = list(aggregate_closed_m1((minute(i) for i in (3 * next_day + 100, 3 * next_day + 101,
                                                               4 * next_day + 1)),
                                          "D1", broker_day_boundary="00:00"))
        self.assertEqual(sunday[0].observed_minutes, 2)
        self.assertFalse(sunday[0].complete)
        missing = list(aggregate_closed_m1((minute(i) for i in (0, 120, next_day)),
                                           "D1", broker_day_boundary="00:00"))
        self.assertEqual(missing[0].observed_minutes, 2)

    def test_d1_explicit_boundary_and_final_open_day(self):
        self.assertEqual(parse_broker_day_boundary("17:00"), 17 * 60)
        with self.assertRaises(ValueError):
            ClosedM1Aggregator("D1")
        with self.assertRaises(ValueError):
            parse_broker_day_boundary("24:00")
        builder = ClosedM1Aggregator("D1", broker_day_boundary="17:00")
        self.assertIsNone(builder.push(minute(17 * 60)))
        self.assertIsNone(builder.push(minute(17 * 60 + 1)))
        self.assertEqual(builder.pending_minutes, 2)  # final open D1 is withheld
        finished = builder.push(minute(17 * 60 + DAY_MS // MINUTE_MS))
        self.assertEqual(finished.start, 17 * 60 * MINUTE_MS)
        self.assertEqual(finished.available_at, minute(17 * 60 + DAY_MS // MINUTE_MS).end)

    def test_m1_duplicate_and_open_bar_fail_closed(self):
        builder = ClosedM1Aggregator("M5")
        builder.push(minute(0))
        with self.assertRaises(ValueError):
            builder.push(minute(0))
        with self.assertRaises(LookAheadError):
            builder.push(minute(1, closed=False))
        malformed = record(minute(2))
        malformed["closed"] = "false"
        with self.assertRaises(LookAheadError):
            builder.push(ClosedM1.from_replay_record(malformed))


class DataQualityManifestTests(unittest.TestCase):
    def _fixture(self, root: Path) -> Path:
        bars = root / "historical_bars"
        bars.mkdir()
        files = {}
        for timeframe in ("M1", "M15", "H1", "H4"):
            path = bars / f"XAUUSD_{timeframe}.jsonl"
            payload = [record(minute(i)) for i in (0, 1, 3, 4, 5)] if timeframe == "M1" else [{"timeframe": timeframe}]
            path.write_text("".join(json.dumps(item) + "\n" for item in payload), encoding="utf-8")
            files[f"historical_bars/{path.name}"] = path.stat().st_size
        index = {
            "schema_version": 1, "output_version": "historical-replay-v1",
            "inputs": [{"normalized_path": "C:/private/local/file.csv", "filename": "XAUUSD_fixture.csv",
                        "size_bytes": 123, "modified_time": "2026-01-01T00:00:00Z",
                        "fingerprint": "sha256:" + "a" * 64, "first_timestamp": "2026-01-01T00:00:00.000",
                        "last_timestamp": "2026-01-02T00:00:00.000", "status": "SUCCESS",
                        "ticks_read": None, "ticks_valid": 50, "ticks_discarded": None,
                        "duplicates": None, "adopted_existing_output": True,
                        "verified_output_files": files}],
        }
        (root / "processed_inputs.json").write_text(json.dumps(index), encoding="utf-8")
        return root

    def test_manifest_preserves_unknown_counts_and_never_infers_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = build_manifest(self._fixture(Path(directory)))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["ticks"], {"valid": 50, "read": None, "discarded": None, "duplicates": None})
        self.assertEqual(manifest["bars_existing"]["M1"], 5)
        self.assertEqual(manifest["gaps_m1"]["intervals_gt_1_minute"], 1)
        self.assertEqual(manifest["gaps_m1"]["reason"], "UNKNOWN_REASON")
        self.assertEqual(manifest["derived_capability_estimates"]["M5"]["sparse_buckets"], 1)
        self.assertEqual(manifest["timezone"], {"broker_server_utc_offset": None, "status": "UNKNOWN"})
        self.assertFalse(manifest["session_attribution_allowed"])
        self.assertEqual(manifest["d1_session_status"], "UNVERIFIED")
        self.assertNotIn("normalized_path", manifest["source_files"][0])
        self.assertNotIn("C:/private", json.dumps(manifest))
        validate_manifest(manifest)

    def test_changed_or_missing_verified_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self._fixture(Path(directory))
            (root / "historical_bars" / "XAUUSD_M1.jsonl").unlink()
            with self.assertRaisesRegex(ValueError, "missing or changed verified output"):
                build_manifest(root)

    def test_schema_rejects_timezone_guess_or_incomplete_manifest(self):
        frozen = json.loads((ROOT / "research" / "data_quality_manifest.json").read_text(encoding="utf-8"))
        validate_manifest(frozen)
        guessed = copy.deepcopy(frozen)
        guessed["session_attribution_allowed"] = True
        with self.assertRaises(ValueError):
            validate_manifest(guessed)
        guessed = copy.deepcopy(frozen)
        guessed["timezone"]["broker_server_utc_offset"] = 3
        with self.assertRaises(ValueError):
            validate_manifest(guessed)
        incomplete = copy.deepcopy(frozen)
        del incomplete["source_files"]
        with self.assertRaises(ValueError):
            validate_manifest(incomplete)


if __name__ == "__main__":
    unittest.main()
