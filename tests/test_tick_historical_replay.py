"""Deterministic tests for the offline MT5 tick replay engine."""

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from research.tick_historical_replay import (
    ChronologyError,
    DeduplicatingJsonlWriter,
    HORIZONS,
    IngestionStats,
    OutcomeTracker,
    SOURCE,
    TIMEFRAMES,
    Tick,
    directional_outcome,
    discover_inputs,
    iter_ticks,
    main,
    parse_mt5_timestamp,
    replay_files,
    validate_m1,
)


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
REPLAY_PATH = ROOT / "research" / "tick_historical_replay.py"


def row_at(start: datetime, seconds: float, bid: float, ask: float, last="", volume="", flags="4"):
    value = start + timedelta(seconds=seconds)
    return (
        value.strftime("%Y.%m.%d"),
        value.strftime("%H:%M:%S.") + f"{value.microsecond // 1000:03d}",
        str(bid),
        str(ask),
        str(last),
        str(volume),
        str(flags),
    )


def write_ticks(path: Path, rows) -> None:
    lines = ["<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>"]
    lines.extend("\t".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def minute_rows(count: int, *, start=None):
    start = start or datetime(2026, 1, 5, tzinfo=timezone.utc)
    return [row_at(start, minute * 60, 2000 + minute * 0.01, 2000.5 + minute * 0.01) for minute in range(count)]


class TickIngestionTests(unittest.TestCase):
    def test_bid_ask_empty_last_volume_milliseconds_and_spread(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ticks.csv"
            write_ticks(path, [row_at(start, 0.123, 2000.0, 2000.4, "", "", "6")])
            stats = IngestionStats()
            ticks = list(iter_ticks([path], stats))
        self.assertEqual(ticks[0].timestamp_ms % 1000, 123)
        self.assertAlmostEqual(ticks[0].mid, 2000.2)
        self.assertAlmostEqual(ticks[0].spread, 0.4)
        self.assertIsNone(ticks[0].last)
        self.assertIsNone(ticks[0].volume)
        self.assertEqual(ticks[0].flags, "6")
        self.assertEqual(stats.ticks_valid, 1)

    def test_invalid_bid_ask_timestamp_and_crossed_quote_are_discarded(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        rows = [
            row_at(start, 0, "", 2000.5),
            row_at(start, 1, 2001.0, 2000.5),
            ("bad", "time", "2000", "2001", "", "", ""),
            row_at(start, 2, 2000.0, 2000.5),
        ]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ticks.csv"
            write_ticks(path, rows)
            stats = IngestionStats()
            ticks = list(iter_ticks([path], stats))
        self.assertEqual(len(ticks), 1)
        self.assertEqual(stats.ticks_read, 4)
        self.assertEqual(stats.ticks_discarded, 3)
        self.assertEqual(stats.discard_reasons["ask_below_bid"], 1)

    def test_exact_duplicates_are_removed_but_flags_are_not_interpreted(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        duplicate = row_at(start, 0, 2000, 2000.5, flags="130")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ticks.csv"
            write_ticks(path, [duplicate, duplicate])
            stats = IngestionStats()
            ticks = list(iter_ticks([path], stats))
        self.assertEqual(len(ticks), 1)
        self.assertEqual(stats.duplicates, 1)
        self.assertEqual(stats.flag_samples, ["130"])

    def test_multiple_files_are_merged_chronologically(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            first, second = Path(temp) / "b.csv", Path(temp) / "a.csv"
            write_ticks(first, [row_at(start, 2, 2002, 2002.5), row_at(start, 4, 2004, 2004.5)])
            write_ticks(second, [row_at(start, 1, 2001, 2001.5), row_at(start, 3, 2003, 2003.5)])
            ticks = list(iter_ticks([first, second], IngestionStats()))
        self.assertEqual([tick.bid for tick in ticks], [2001, 2002, 2003, 2004])

    def test_out_of_order_inside_one_file_fails_closed(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ticks.csv"
            write_ticks(path, [row_at(start, 2, 2000, 2001), row_at(start, 1, 2000, 2001)])
            with self.assertRaises(ChronologyError):
                list(iter_ticks([path], IngestionStats()))


class InputDirectoryTests(unittest.TestCase):
    def test_input_directory_discovers_csv_and_sorts_by_content_timestamp(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            later = root / "XAUUSD_a.csv"
            earlier = root / "XAUUSD_z.csv"
            write_ticks(later, [row_at(start, 60, 2001, 2002)])
            write_ticks(earlier, [row_at(start, 0, 2000, 2001)])
            (root / "ignore.txt").write_text("not csv", encoding="utf-8")
            discovery = discover_inputs(input_dir=root)
        self.assertEqual(discovery.csv_files_found, 2)
        self.assertEqual([item.path.name for item in discovery.files], ["XAUUSD_z.csv", "XAUUSD_a.csv"])

    def test_overlap_is_reported_and_exact_ticks_are_deduplicated(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        duplicate = row_at(start, 60, 2001, 2002)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first, second = root / "XAUUSD_one.csv", root / "XAUUSD_two.csv"
            write_ticks(first, [row_at(start, 0, 2000, 2001), duplicate])
            write_ticks(second, [duplicate, row_at(start, 120, 2002, 2003)])
            discovery = discover_inputs(input_dir=root)
            report = replay_files([item.path for item in discovery.files], root / "out")
        self.assertEqual(len(discovery.overlaps), 1)
        self.assertEqual(report["duplicates"], 1)
        self.assertEqual(report["ticks_valid"], 3)

    def test_non_xauusd_and_corrupt_csv_are_warned_and_skipped(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_ticks(root / "EURUSD_ticks.csv", [row_at(start, 0, 1.0, 1.1)])
            (root / "XAUUSD_corrupt.csv").write_text("DATE,TIME,OPEN\n", encoding="utf-8")
            write_ticks(root / "XAUUSD_valid.csv", [row_at(start, 0, 2000, 2001)])
            discovery = discover_inputs(input_dir=root)
        self.assertEqual([item.path.name for item in discovery.files], ["XAUUSD_valid.csv"])
        causes = " ".join(issue["cause"] for issue in discovery.issues)
        self.assertIn("SKIPPED_NON_XAUUSD", causes)
        self.assertIn("missing columns", causes)

    def test_runtime_corruption_skips_remainder_and_continues_other_file(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bad, good = root / "XAUUSD_bad.csv", root / "XAUUSD_good.csv"
            write_ticks(
                bad,
                [row_at(start, 0, 2000, 2001), row_at(start, 120, 2002, 2003), row_at(start, 60, 2001, 2002)],
            )
            write_ticks(good, [row_at(start, 30, 2000.5, 2001.5), row_at(start, 180, 2003, 2004)])
            report = replay_files([bad, good], root / "out")
        self.assertEqual(len(report["runtime_file_issues"]), 1)
        self.assertIn("non-chronological", report["runtime_file_issues"][0]["cause"])
        self.assertEqual(report["ticks_valid"], 4)

    def test_cli_input_dir_prints_preflight_and_final_summary(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_ticks(root / "XAUUSD_ticks.csv", [row_at(start, 0, 2000, 2001), row_at(start, 60, 2001, 2002)])
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["--input-dir", str(root), "--output-dir", str(root / "out")])
        text = output.getvalue()
        self.assertEqual(result, 0)
        for marker in (
            "[HISTORICAL] input_dir=",
            "[HISTORICAL] csv_files=1",
            "[HISTORICAL] first_timestamp=",
            "[HISTORICAL] last_timestamp=",
            "[HISTORICAL] estimated_total_size=",
            "[HISTORICAL] files_processed=1",
            "[HISTORICAL] ticks_read=2",
            "[HISTORICAL] ticks_valid=2",
            "[HISTORICAL] bars_M1=1",
            "[HISTORICAL] observations=",
            "[HISTORICAL] outcomes=",
            "[HISTORICAL] ticks_per_second=",
        ):
            self.assertIn(marker, text)


class BarAndReplayTests(unittest.TestCase):
    def test_gaps_do_not_create_synthetic_m1_bars(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(ticks, [row_at(start, 0, 2000, 2001), row_at(start, 120, 2002, 2003)])
            replay_files([ticks], root / "out", chunk_size=1)
            bars = read_jsonl(root / "out" / "historical_bars" / "XAUUSD_M1.jsonl")
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["tick_count"], 1)

    def test_reconstructs_only_m1_m15_h1_h4(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(ticks, minute_rows(241))
            report = replay_files([ticks], root / "out", chunk_size=17)
        self.assertEqual(set(report["bars_generated"]), {"M1", "M15", "H1", "H4"})
        self.assertEqual(report["bars_generated"], {"M1": 240, "M15": 16, "H1": 4, "H4": 1})
        self.assertNotIn("M30", TIMEFRAMES)

    def test_bar_spread_statistics_use_real_quotes(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(
                ticks,
                [
                    row_at(start, 1, 100.0, 100.2),
                    row_at(start, 30, 100.5, 100.9),
                    row_at(start, 60, 101.0, 101.3),
                ],
            )
            replay_files([ticks], root / "out")
            bar = read_jsonl(root / "out" / "historical_bars" / "XAUUSD_M1.jsonl")[0]
        self.assertAlmostEqual(bar["avg_spread"], 0.3)
        self.assertAlmostEqual(bar["min_spread"], 0.2)
        self.assertAlmostEqual(bar["max_spread"], 0.4)

    def test_observations_are_only_m15_h1_h4_and_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(ticks, minute_rows(121))
            replay_files([ticks], root / "out", chunk_size=11)
            observations = read_jsonl(root / "out" / "historical_observations.jsonl")
        self.assertEqual({row["timeframe"] for row in observations}, {"M15", "H1"})
        self.assertTrue(all(row["source"] == SOURCE for row in observations))
        self.assertTrue(all(row["observer_only"] is True for row in observations))
        self.assertTrue(all(row["score_effect"] == 0 for row in observations))

    def test_chunk_size_does_not_change_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(ticks, minute_rows(91))
            replay_files([ticks], root / "small", chunk_size=1)
            replay_files([ticks], root / "large", chunk_size=64)
            small = (root / "small" / "historical_observations.jsonl").read_bytes()
            large = (root / "large" / "historical_observations.jsonl").read_bytes()
        self.assertEqual(small, large)

    def test_resume_reprocesses_causally_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            output = root / "out"
            write_ticks(ticks, minute_rows(121))
            first = replay_files([ticks], output, chunk_size=13)
            counts = {
                path: len(read_jsonl(path))
                for path in output.rglob("*.jsonl")
            }
            second = replay_files([ticks], output, chunk_size=7)
            resumed_counts = {path: len(read_jsonl(path)) for path in output.rglob("*.jsonl")}
        self.assertEqual(counts, resumed_counts)
        self.assertEqual(second["observations_written"], 0)
        self.assertEqual(second["outcomes_written"], 0)
        self.assertGreater(first["observations_written"], 0)

    def test_event_id_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(ticks, minute_rows(31))
            replay_files([ticks], root / "one", chunk_size=2)
            replay_files([ticks], root / "two", chunk_size=9)
            first = read_jsonl(root / "one" / "historical_observations.jsonl")
            second = read_jsonl(root / "two" / "historical_observations.jsonl")
        self.assertEqual([row["event_id"] for row in first], [row["event_id"] for row in second])

    def test_future_ticks_never_change_an_already_emitted_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            short_ticks, full_ticks = root / "short.csv", root / "full.csv"
            write_ticks(short_ticks, minute_rows(31))
            write_ticks(full_ticks, minute_rows(91))
            replay_files([short_ticks], root / "short", chunk_size=5)
            replay_files([full_ticks], root / "full", chunk_size=5)
            short_rows = read_jsonl(root / "short" / "historical_observations.jsonl")
            full_rows = {row["event_id"]: row for row in read_jsonl(root / "full" / "historical_observations.jsonl")}
        for row in short_rows:
            self.assertEqual(row, full_rows[row["event_id"]])


class OutcomeTests(unittest.TestCase):
    def test_outcome_waits_for_horizon_and_uses_tick_mfe_mae_spread(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "outcomes.jsonl"
            writer = DeduplicatingJsonlWriter(path, "outcome_id")
            tracker = OutcomeTracker(writer)
            tracker.add_observation(
                {"event_id": "event-1", "observed_at": 1_000, "close": 100.0, "spread": 0.4}
            )
            tracker.on_tick(Tick(2_000, 104.5, 105.5))
            tracker.on_tick(Tick(3_000, 95.5, 96.5))
            tracker.on_tick(Tick(901_000, 101.5, 102.5))
            self.assertEqual(read_jsonl(path), [])
            tracker.on_tick(Tick(901_001, 102.0, 102.4))
            writer.close()
            row = read_jsonl(path)[0]
        self.assertAlmostEqual(row["future_return_15m"], 0.02)
        self.assertAlmostEqual(row["mfe_15m"], 0.05)
        self.assertAlmostEqual(row["mae_15m"], -0.04)
        self.assertAlmostEqual(row["entry_spread"], 0.4)
        self.assertAlmostEqual(row["max_spread_15m"], 1.0)
        self.assertIsNone(row["future_return_1h"])

    def test_short_direction_inverts_return_and_excursions(self):
        long_values = {"future_return": 0.02, "mfe": 0.05, "mae": -0.04}
        short = directional_outcome(long_values, "SHORT")
        self.assertEqual(short, {"future_return": -0.02, "mfe": 0.04, "mae": -0.05})

    def test_incomplete_end_keeps_later_horizons_absent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(ticks, minute_rows(31))
            replay_files([ticks], root / "out", chunk_size=4)
            outcomes = read_jsonl(root / "out" / "historical_outcomes.jsonl")
        self.assertTrue(outcomes)
        self.assertTrue(all(row["completed_horizon"] == "15m" for row in outcomes))
        self.assertTrue(all(row["future_return_1h"] is None for row in outcomes))

    def test_all_outcomes_are_observer_only_and_zero_effect(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ticks = root / "ticks.csv"
            write_ticks(ticks, minute_rows(121))
            replay_files([ticks], root / "out")
            outcomes = read_jsonl(root / "out" / "historical_outcomes.jsonl")
        self.assertTrue(all(row["source"] == SOURCE for row in outcomes))
        self.assertTrue(all(row["observer_only"] is True for row in outcomes))
        self.assertTrue(all(row["score_effect"] == 0 for row in outcomes))

    def test_all_three_horizons_are_progressive_and_causal(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "outcomes.jsonl"
            writer = DeduplicatingJsonlWriter(path, "outcome_id")
            tracker = OutcomeTracker(writer)
            anchor = 1_000
            tracker.add_observation(
                {"event_id": "event-all", "observed_at": anchor, "close": 100.0, "spread": 0.2}
            )
            tracker.on_tick(Tick(anchor + 15 * 60 * 1000, 100.9, 101.1))
            tracker.on_tick(Tick(anchor + 15 * 60 * 1000 + 1, 101.0, 101.2))
            tracker.on_tick(Tick(anchor + 60 * 60 * 1000, 101.9, 102.1))
            tracker.on_tick(Tick(anchor + 60 * 60 * 1000 + 1, 102.0, 102.2))
            tracker.on_tick(Tick(anchor + 4 * 60 * 60 * 1000, 102.9, 103.1))
            tracker.finish(anchor + 4 * 60 * 60 * 1000)
            writer.close()
            rows = read_jsonl(path)
        self.assertEqual([row["completed_horizon"] for row in rows], ["15m", "1h", "4h"])
        self.assertIsNone(rows[0]["future_return_1h"])
        self.assertIsNotNone(rows[1]["future_return_15m"])
        self.assertIsNotNone(rows[1]["future_return_1h"])
        self.assertIsNotNone(rows[2]["future_return_4h"])


class ValidationAndScopeTests(unittest.TestCase):
    def test_m1_validation_reports_matches_and_differences(self):
        start = datetime(2026, 1, 5, tzinfo=timezone.utc)
        timestamp = parse_mt5_timestamp("2026.01.05", "00:00:00.000")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reconstructed = root / "M1.jsonl"
            reconstructed.write_text(
                json.dumps({"timestamp": timestamp, "open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0}) + "\n",
                encoding="utf-8",
            )
            reference = root / "reference.csv"
            reference.write_text(
                "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\n"
                "2026.01.05\t00:00:00\t100\t102\t99\t101.01\n",
                encoding="utf-8",
            )
            report = validate_m1(reconstructed, reference, tolerance=0.001)
        self.assertEqual(report["bars_compared"], 1)
        self.assertEqual(report["matches"], 0)
        self.assertEqual(report["ohlc_differences"]["close"]["count"], 1)

    def test_replay_module_has_no_live_execution_surface(self):
        source = REPLAY_PATH.read_text(encoding="utf-8")
        for forbidden in ("OrderSend", "PositionOpen", "EnableLiveTrading = true", "RiskPercent ="):
            self.assertNotIn(forbidden, source)
        self.assertIn('SOURCE = "HISTORICAL_MT5_TICKS"', source)

    def test_live_ea_safety_defaults_remain_intact(self):
        ea = EA_PATH.read_text(encoding="utf-8")
        self.assertIn("input bool   EnableLiveTrading      = false", ea)
        self.assertIn("input double RiskPercent             = 5.0", ea)
        self.assertIn("input int    ArmScoreThreshold       = 58", ea)
        self.assertIn("input int    MinScoreToTrade         = 74", ea)


if __name__ == "__main__":
    unittest.main()
