"""Phase-2 clock and materialized-bar tests; no live EA imports."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from research.broker_clock_analysis import analyze_clock, collect_pause_evidence, validate_clock_report
from research.build_causal_m5_d1 import (
    BUILDER_VERSION, SCHEMA_VERSION, compare_mt5_reference, materialize,
)
from research.multi_strategy_causal_bars import CausalBar, LookAheadError, MINUTE_MS, require_causal_feature


ROOT = Path(__file__).resolve().parents[1]
FROZEN = json.loads((ROOT / "research" / "data_quality_manifest.json").read_text(encoding="utf-8"))
DAY_MINUTES = 1440


def _source_row(minute: int, *, closed: bool = True) -> dict:
    value = 100.0 + minute / 100
    return {
        "bar_id": f"M1-{minute}", "source": "HISTORICAL_MT5_TICKS", "symbol": "XAUUSD",
        "timeframe": "M1", "timestamp_unit": "epoch_ms_mt5_server_wall_time",
        "timestamp": minute * MINUTE_MS, "close_timestamp": (minute + 1) * MINUTE_MS,
        "open": value, "high": value + 1, "low": value - 1, "close": value + 0.5,
        "tick_count": 10, "avg_spread": 0.3, "min_spread": 0.2, "max_spread": 0.4,
        "closed": closed,
    }


def _fixture(root: Path, *, minutes: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6, 8, 9, 10, DAY_MINUTES),
             open_minute: int | None = None) -> tuple[dict, dict]:
    bars = root / "historical_bars"
    bars.mkdir()
    source = bars / "XAUUSD_M1.jsonl"
    source.write_text("".join(json.dumps(_source_row(minute, closed=minute != open_minute)) + "\n"
                              for minute in minutes), encoding="utf-8")
    manifest = copy.deepcopy(FROZEN)
    manifest["dataset_id"] = "XAUUSD_MT5_TICKS_testfixture"
    manifest["bars_existing"]["M1"] = len(minutes)
    fingerprint = "sha256:" + "a" * 64
    manifest["fingerprint"] = fingerprint
    manifest["source_files"] = [{"filename": "fixture.csv", "fingerprint": fingerprint,
                                 "size_bytes": 123, "modified_time": "2026-01-01T00:00:00Z", "status": "SUCCESS"}]
    index = {"inputs": [{"status": "SUCCESS", "fingerprint": fingerprint,
                          "verified_output_files": {"historical_bars/XAUUSD_M1.jsonl": source.stat().st_size}}]}
    (root / "processed_inputs.json").write_text(json.dumps(index), encoding="utf-8")
    return manifest, analyze_clock(manifest)


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class BrokerClockTests(unittest.TestCase):
    def test_available_evidence_keeps_clock_unknown_and_sessions_disabled(self):
        report = analyze_clock(FROZEN)
        self.assertEqual(report["broker_clock_status"], "UNKNOWN")
        self.assertIsNone(report["broker_server_utc_offset"])
        self.assertIsNone(report["broker_day_rollover"])
        self.assertEqual(report["dst_behavior"], "UNKNOWN")
        self.assertFalse(report["session_attribution_allowed"])
        self.assertEqual(report["evidence"][1]["reason"], "UNKNOWN_REASON")

    def test_two_trusted_pairs_are_only_partial_without_rollover(self):
        pairs = [
            {"capture_id": "one", "source": "MT5_SAME_INSTANT_SERVER_UTC_CAPTURE",
             "server_wall_time": "2026-01-02T14:00:00", "utc_time": "2026-01-02T12:00:00Z"},
            {"capture_id": "two", "source": "MT5_SAME_INSTANT_SERVER_UTC_CAPTURE",
             "server_wall_time": "2026-01-04T14:00:00", "utc_time": "2026-01-04T12:00:00Z"},
        ]
        report = analyze_clock(FROZEN, pairs)
        self.assertEqual(report["broker_clock_status"], "PARTIAL")
        self.assertEqual(report["broker_server_utc_offset"], 2)
        self.assertIsNone(report["broker_day_rollover"])
        self.assertFalse(report["session_attribution_allowed"])

    def test_pc_or_untrusted_pair_cannot_promote_and_verified_requires_proof(self):
        with self.assertRaisesRegex(ValueError, "trusted source"):
            analyze_clock(FROZEN, [{"capture_id": "pc", "source": "PC_LOCAL_TIME",
                                    "server_wall_time": "2026-01-01T12:00:00",
                                    "utc_time": "2026-01-01T10:00:00Z"}])
        forged = analyze_clock(FROZEN)
        forged.update({"broker_clock_status": "VERIFIED", "broker_server_utc_offset": 2,
                       "broker_day_rollover": "17:00", "dst_behavior": "UNKNOWN"})
        with self.assertRaisesRegex(ValueError, "independent evidence"):
            validate_clock_report(forged)

    def test_observed_pause_is_wall_time_evidence_not_utc_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture(root, minutes=(0, 1, 200, 201))
            pauses = collect_pause_evidence(root / "historical_bars" / "XAUUSD_M1.jsonl")
        self.assertEqual(pauses["intervals_gt_1_hour"], 1)
        self.assertEqual(pauses["largest"][0]["elapsed_minutes"], 199)
        self.assertFalse(pauses["utc_anchor"])
        report = analyze_clock(FROZEN, pause_evidence=pauses)
        self.assertEqual(report["broker_clock_status"], "UNKNOWN")
        self.assertIsNone(report["broker_server_utc_offset"])


class MaterializationTests(unittest.TestCase):
    def test_m5_d1_causal_rows_gaps_coverage_final_bucket_and_versioning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, clock = _fixture(root)
            output = root / "derived"
            index = materialize(root, output, manifest, clock)
            m5, d1 = _rows(output / "XAUUSD_M5.jsonl"), _rows(output / "XAUUSD_D1.jsonl")
            self.assertEqual((len(m5), len(d1)), (3, 1))
            self.assertEqual([row["start"] for row in m5], [0, 5 * MINUTE_MS, 10 * MINUTE_MS])
            self.assertNotIn(DAY_MINUTES * MINUTE_MS, [row["start"] for row in m5])
            self.assertNotIn(DAY_MINUTES * MINUTE_MS, [row["start"] for row in d1])
            self.assertEqual((m5[0]["observed_minutes"], m5[0]["expected_minutes"], m5[0]["coverage"]),
                             (5, 5, 1.0))
            self.assertTrue(m5[0]["complete"])
            self.assertEqual((m5[1]["observed_minutes"], m5[1]["coverage"]), (4, 0.8))
            self.assertFalse(m5[1]["complete"])
            self.assertEqual(m5[1]["provenance"]["source_bar_ids"], ["M1-5", "M1-6", "M1-8", "M1-9"])
            self.assertEqual(m5[0]["available_at"], 6 * MINUTE_MS)
            self.assertGreater(m5[2]["available_at"], m5[2]["end"])
            self.assertEqual(d1[0]["bucket_type"], "TECHNICAL_DATE_BUCKET")
            self.assertEqual(d1[0]["rollover_status"], "UNVERIFIED")
            self.assertEqual(d1[0]["d1_session_status"], "UNVERIFIED")
            self.assertNotIn("session", d1[0])
            self.assertNotIn("overnight_label", d1[0])
            self.assertFalse(d1[0]["complete"])
            self.assertEqual(d1[0]["observed_minutes"], 10)
            self.assertEqual(index["source_closed_m1_count"], 11)
            self.assertEqual(index["quality"]["M5"]["incomplete_buckets"], 2)
            self.assertEqual(index["quality"]["D1"]["incomplete_buckets"], 1)
            self.assertEqual(index["broker_day_boundary"], "00:00")
            for row in (*m5, *d1):
                self.assertEqual(row["schema_version"], SCHEMA_VERSION)
                self.assertEqual(row["builder_version"], BUILDER_VERSION)
                self.assertEqual(row["source_manifest_id"], manifest["dataset_id"])
                self.assertTrue(row["generated_at"].endswith("Z"))
                self.assertEqual(row["source"], "HISTORICAL_MT5_TICKS")
                self.assertGreaterEqual(row["available_at"], row["end"])

    def test_short_technical_sunday_is_sparse_not_rejected_as_corrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sunday = 3 * DAY_MINUTES  # 1970-01-04 in server-wall encoding
            manifest, clock = _fixture(root, minutes=(sunday, sunday + 1, sunday + DAY_MINUTES))
            output = root / "derived"
            index = materialize(root, output, manifest, clock)
            d1 = _rows(output / "XAUUSD_D1.jsonl")
            self.assertEqual(len(d1), 1)
            self.assertFalse(d1[0]["complete"])
            self.assertEqual(d1[0]["observed_minutes"], 2)
            self.assertEqual(d1[0]["bucket_type"], "TECHNICAL_DATE_BUCKET")
            self.assertEqual(index["quality"]["D1"]["technical_sunday_buckets"], 1)

    def test_independently_verified_rollover_uses_explicit_broker_day_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rollover_minute = 17 * 60
            minutes = (rollover_minute - 1, rollover_minute, rollover_minute + 1,
                       rollover_minute + DAY_MINUTES)
            manifest, clock = _fixture(root, minutes=minutes)
            clock.update({"broker_clock_status": "VERIFIED", "broker_server_utc_offset": 2,
                          "broker_day_rollover": "17:00", "dst_behavior": "NO_DST",
                          "session_attribution_allowed": True})
            clock["evidence"].append({"type": "BROKER_ROLLOVER_VERIFIED",
                                      "source_id": "synthetic-test-evidence"})
            output = root / "derived"
            index = materialize(root, output, manifest, clock)
            d1 = _rows(output / "XAUUSD_D1.jsonl")
            self.assertEqual(index["broker_day_boundary"], "17:00")
            self.assertEqual(index["rollover_status"], "VERIFIED")
            self.assertEqual(d1[1]["start"], rollover_minute * MINUTE_MS)
            self.assertEqual(d1[1]["bucket_type"], "BROKER_DAY")
            self.assertEqual(d1[1]["d1_session_status"], "VERIFIED")

    def test_m5_and_d1_not_available_before_confirmed_close(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, clock = _fixture(root)
            output = root / "derived"
            materialize(root, output, manifest, clock)
            for filename in ("XAUUSD_M5.jsonl", "XAUUSD_D1.jsonl"):
                row = _rows(output / filename)[0]
                bar = CausalBar(**{field: row[field] for field in CausalBar.__dataclass_fields__
                                   if field in row})
                for feature in ("pivot", "structure", "regime", "setup", "entry_quality"):
                    with self.subTest(filename=filename, feature=feature):
                        with self.assertRaises(LookAheadError):
                            require_causal_feature(bar, row["end"] - 1, feature)
                        with self.assertRaises(LookAheadError):
                            require_causal_feature(bar, row["available_at"] - 1, feature)
                        require_causal_feature(bar, row["available_at"], feature)

    def test_identical_rerun_skips_and_incompatible_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, clock = _fixture(root)
            output = root / "derived"
            first = materialize(root, output, manifest, clock)
            self.assertFalse(first["skipped_identical"])
            second = materialize(root, output, manifest, clock)
            self.assertTrue(second["skipped_identical"])
            self.assertEqual(first["outputs"], second["outputs"])
            changed = copy.deepcopy(manifest)
            changed["dataset_id"] = "changed"
            changed_clock = analyze_clock(changed)
            with self.assertRaisesRegex(FileExistsError, "incompatible"):
                materialize(root, output, changed, changed_clock)
            partial_clock = analyze_clock(manifest, [
                {"capture_id": "one", "source": "MT5_SAME_INSTANT_SERVER_UTC_CAPTURE",
                 "server_wall_time": "2026-01-02T14:00:00", "utc_time": "2026-01-02T12:00:00Z"},
                {"capture_id": "two", "source": "MT5_SAME_INSTANT_SERVER_UTC_CAPTURE",
                 "server_wall_time": "2026-01-04T14:00:00", "utc_time": "2026-01-04T12:00:00Z"},
            ])
            with self.assertRaisesRegex(FileExistsError, "incompatible"):
                materialize(root, output, manifest, partial_clock)
            self.assertEqual(_rows(output / "XAUUSD_M5.jsonl")[0]["source_manifest_id"], manifest["dataset_id"])

    def test_partial_existing_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, clock = _fixture(root)
            output = root / "derived"
            output.mkdir()
            partial = output / "XAUUSD_M5.jsonl"
            partial.write_text("partial", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "partial"):
                materialize(root, output, manifest, clock)
            self.assertEqual(partial.read_text(encoding="utf-8"), "partial")

    def test_open_source_m1_fails_without_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, clock = _fixture(root, open_minute=3)
            output = root / "derived"
            with self.assertRaisesRegex(ValueError, "invalid closed M1"):
                materialize(root, output, manifest, clock)
            self.assertFalse((output / "XAUUSD_M5.jsonl").exists())
            self.assertFalse((output / "XAUUSD_D1.jsonl").exists())

    def test_reference_comparison_reports_match_difference_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest, clock = _fixture(root)
            output = root / "derived"
            materialize(root, output, manifest, clock)
            m5 = _rows(output / "XAUUSD_M5.jsonl")
            starts = [row["start"] for row in m5]
            no_ref = compare_mt5_reference(output / "XAUUSD_M5.jsonl", None, starts)
            self.assertEqual(no_ref["status"], "NO_MT5_REFERENCE")
            self.assertEqual(no_ref["compared"], 0)
            reference = root / "reference.jsonl"
            copy_rows = [{field: m5[0][field] for field in ("start", "open", "high", "low", "close")},
                         {field: m5[1][field] for field in ("start", "open", "high", "low", "close")}]
            copy_rows[1]["high"] += 0.5
            reference.write_text("".join(json.dumps(row) + "\n" for row in copy_rows), encoding="utf-8")
            result = compare_mt5_reference(output / "XAUUSD_M5.jsonl", reference, starts, tolerance=0.01)
            self.assertEqual((result["compared"], result["matches"], len(result["differences"])), (2, 1, 1))
            self.assertEqual(result["missing_reference"], [starts[2]])


if __name__ == "__main__":
    unittest.main()
