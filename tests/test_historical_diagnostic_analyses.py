"""Tests for behavior-shift and tick-ordered stop diagnostics."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from research.analyze_bias_release import load_stale_episodes
from research.analyze_h1_behavior_shift import _csv_text, baseline_stale_enrichment, h1_classic_transition_rows
from research.analyze_stop_loss_quality import (
    HORIZON_MS,
    M1RangeIndex,
    SignalState,
    _aggregate,
    _post_stop_favorable,
    build_stop_candidates,
    simulate_tick_order,
)
from research.tick_historical_replay import Tick


def signal(direction: str = "LONG", anchor: int = 1_700_000_000_000) -> SignalState:
    return SignalState(
        event_id=f"signal-{direction}-{anchor}",
        anchor_ms=anchor,
        period="TRAIN",
        direction=direction,
        setup="BREAKOUT",
        reference_close=100.0,
        atr=1.0,
        recent_high=101.0,
        recent_low=99.0,
        swing_high=101.0,
        swing_low=99.0,
        structure="BULLISH" if direction == "LONG" else "BEARISH",
        behavior_shift="NONE",
        momentum=True,
        breakout=True,
        pullback=False,
        recovery=False,
        session=None,
        spread=0.2,
        long_score=75.0,
        short_score=25.0,
        source_timeframe="H1",
    )


def tick(timestamp: int, bid: float, ask: float) -> Tick:
    return Tick(timestamp, bid, ask)


def aggregate_row(index: int = 0, **overrides) -> dict:
    value = {
        "event_id": f"event-{index}",
        "period": "TRAIN",
        "direction": "LONG",
        "setup": "BREAKOUT",
        "candidate": "CURRENT",
        "horizon": "1h",
        "stop_hit": False,
        "hit_before_favorable_1atr": False,
        "premature_stop": False,
        "mae_before_favorable_atr": 0.5,
        "stop_distance_atr": 1.2,
        "mfe_after_stop_atr": None,
        "time_to_stop_minutes": None,
        "excessively_wide": False,
        "future_return": 0.01,
        "mfe": 0.02,
        "mae": -0.01,
        "potential_mfe_r": 1.5,
        "momentum": True,
        "breakout": True,
        "pullback": False,
        "recovery": False,
        "structure": "BULLISH",
        "behavior_shift": "NONE",
        "atr_regime": "MEDIUM",
        "spread_quartile": "Q2",
    }
    value.update(overrides)
    return value


class HistoricalDiagnosticAnalysisTests(unittest.TestCase):
    def test_behavior_shift_csv_is_consumable_by_bias_release(self):
        row = {
            "cohort": "BASELINE_STALE_EPISODE",
            "event_id": "HISTORICAL_MT5_TICKS-XAUUSD-H1-1000",
            "period": "OOS",
            "direction_change": "LONG_TO_BEARISH",
            "episode_start": 1_000,
            "classic_change_timestamp": 3_601_000,
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "h1_behavior_shift.csv"
            path.write_text(_csv_text([row]), encoding="utf-8")
            episodes = load_stale_episodes(path)

        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0]["source_timeframe"], "H1")
        self.assertEqual(episodes[0]["stale_direction"], "LONG")
        self.assertEqual(episodes[0]["new_direction"], "SHORT")

    def test_current_structure_atr_and_hybrid_candidates(self):
        candidates = build_stop_candidates("LONG", 100.0, 2.0, 104.0, 96.0, 104.0, 97.0)
        self.assertEqual(set(candidates), {"CURRENT", "STRUCTURE", "ATR_1_0", "ATR_1_5", "ATR_2_0", "HYBRID"})
        self.assertAlmostEqual(candidates["CURRENT"].distance_atr, 2.0)
        self.assertAlmostEqual(candidates["STRUCTURE"].distance_atr, 1.75)
        self.assertAlmostEqual(candidates["HYBRID"].distance_atr, 1.75)
        self.assertAlmostEqual(candidates["ATR_1_0"].distance_atr, 1.0)
        self.assertAlmostEqual(candidates["ATR_1_5"].distance_atr, 1.5)

    def test_short_structure_candidate_is_symmetric(self):
        candidates = build_stop_candidates("SHORT", 100.0, 2.0, 104.0, 96.0, 103.0, 96.0)
        self.assertAlmostEqual(candidates["STRUCTURE"].price, 103.5)
        self.assertAlmostEqual(candidates["STRUCTURE"].distance_atr, 1.75)
        self.assertGreater(candidates["CURRENT"].price, 100.0)

    def test_stop_hit_uses_tick_order_and_bid_for_long(self):
        item = signal("LONG")
        ticks = [
            tick(item.anchor_ms, 99.9, 100.1),
            tick(item.anchor_ms + 1_000, 98.8, 99.0),
            tick(item.anchor_ms + 2_000, 101.0, 101.2),
        ]
        simulate_tick_order([item], ticks, progress_every=0)
        self.assertEqual(item.stops["ATR_1_0"].hit_ms, item.anchor_ms + 1_000)
        self.assertEqual(item.favorable_time_ms, item.anchor_ms + 2_000)

    def test_stop_hit_uses_ask_for_short(self):
        item = signal("SHORT")
        ticks = [
            tick(item.anchor_ms, 99.9, 100.1),
            tick(item.anchor_ms + 1_000, 101.0, 101.2),
            tick(item.anchor_ms + 2_000, 98.8, 99.0),
        ]
        simulate_tick_order([item], ticks, progress_every=0)
        self.assertEqual(item.stops["ATR_1_0"].hit_ms, item.anchor_ms + 1_000)
        self.assertEqual(item.favorable_time_ms, item.anchor_ms + 2_000)

    def test_gap_triggers_at_first_available_tick_without_inventing_ticks(self):
        item = signal("LONG")
        ticks = [
            tick(item.anchor_ms, 99.9, 100.1),
            tick(item.anchor_ms + 600_000, 95.0, 95.2),
        ]
        simulate_tick_order([item], ticks, progress_every=0)
        self.assertEqual(item.stops["CURRENT"].hit_ms, item.anchor_ms + 600_000)

    def test_ticks_before_anchor_cannot_initialize_or_hit_signal(self):
        item = signal("LONG")
        simulate_tick_order(
            [item],
            [tick(item.anchor_ms - 1, 90.0, 90.2), tick(item.anchor_ms, 99.9, 100.1)],
            progress_every=0,
        )
        self.assertEqual(item.entry_tick_ms, item.anchor_ms)
        self.assertIsNone(item.stops["CURRENT"].hit_ms)

    def test_mfe_after_stop_excludes_price_before_stop(self):
        item = signal("LONG", anchor=0)
        item.entry_mid = 100.0
        candidates = build_stop_candidates("LONG", 100.1, 1.0, 101.0, 99.0, 101.0, 99.0)
        stop = candidates["ATR_1_0"]
        stop.hit_ms = 30_000
        stop.post_stop_same_minute_high = 100.2
        stop.post_stop_same_minute_low = 99.0
        ranges = M1RangeIndex(
            [
                {"timestamp": 0, "high": 105.0, "low": 99.0},
                {"timestamp": 60_000, "high": 101.5, "low": 99.5},
            ]
        )
        self.assertAlmostEqual(_post_stop_favorable(item, stop, 120_000, ranges), 1.5)

    def test_mfe_after_stop_does_not_cross_horizon_boundary(self):
        item = signal("LONG", anchor=0)
        item.entry_mid = 100.0
        stop = build_stop_candidates("LONG", 100.1, 1.0, 101.0, 99.0, 101.0, 99.0)["ATR_1_0"]
        stop.hit_ms = 900_000
        stop.hit_mid = 99.0
        stop.post_stop_same_minute_high = 110.0
        stop.post_stop_same_minute_low = 99.0
        ranges = M1RangeIndex([{"timestamp": 900_000, "high": 110.0, "low": 99.0}])
        self.assertEqual(_post_stop_favorable(item, stop, 900_000, ranges), 0.0)

    def test_behavior_shift_uses_latest_signal_and_precedes_classic_flip(self):
        observations = []
        assignments = {}
        for index in range(8):
            state = "BULLISH" if index < 7 else "BEARISH"
            shift = "POSSIBLE_BEARISH_REVERSAL" if index in {2, 5} else "NONE"
            event_id = f"h1-{index}"
            assignments[event_id] = "TRAIN"
            observations.append(
                {
                    "event_id": event_id,
                    "_timestamp_ms": index * 3_600_000,
                    "timestamp": index * 3_600_000,
                    "timeframe": "H1",
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0 - index,
                    "close": 100.0 - index,
                    "h1_structure": {"pivot_state": state},
                    "h1_regime": {"behavior_shift": shift, "atr": 1.0},
                }
            )
        rows = h1_classic_transition_rows(observations, assignments)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["detected_before_classic"])
        self.assertEqual(rows[0]["behavior_shift_timestamp"], 5 * 3_600_000)
        self.assertEqual(rows[0]["lead_h1_bars"], 2)

    def test_stale_episode_uses_h1_signal_and_only_post_signal_m15_adverse_move(self):
        merged = [
            {
                "event_id": "h1-signal",
                "_timestamp_ms": 3_600_000,
                "timestamp": 0,
                "timeframe": "H1",
                "close": 100.0,
                "h1_regime": {
                    "behavior_shift": "POSSIBLE_BEARISH_REVERSAL",
                    "atr": 2.0,
                },
            },
            {
                "event_id": "m15-before",
                "_timestamp_ms": 3_600_000,
                "timestamp": 2_700_000,
                "timeframe": "M15",
                "high": 101.0,
                "low": 80.0,
                "close": 100.0,
            },
            {
                "event_id": "m15-after",
                "_timestamp_ms": 4_500_000,
                "timestamp": 3_600_000,
                "timeframe": "M15",
                "high": 101.0,
                "low": 96.0,
                "close": 97.0,
            },
        ]
        rows = baseline_stale_enrichment(
            [
                {
                    "event_id": "stale-h4",
                    "timeframe": "H4",
                    "bias": "LONG",
                    "started_at": 0,
                    "changed_at": 7_200_000,
                    "adverse_price_move_before_change": 20.0,
                    "adverse_atr": 10.0,
                }
            ],
            merged,
            {"stale-h4": "TRAIN"},
        )
        self.assertTrue(rows[0]["detected_before_classic"])
        self.assertEqual(rows[0]["potential_adverse_avoided_price"], 4.0)
        self.assertEqual(rows[0]["potential_adverse_avoided_atr"], 2.0)

    def test_segmentation_requires_minimum_sample(self):
        enough = _aggregate([aggregate_row(index) for index in range(30)], segmented=True)
        too_few = _aggregate([aggregate_row(index) for index in range(29)], segmented=True)
        self.assertTrue(any(row["segment"] == "setup" for row in enough))
        self.assertEqual(too_few, [])
        self.assertTrue(all(row["score_effect"] == 0 for row in enough))

    def test_horizons_are_fixed_and_no_future_tick_is_needed_for_shorter_horizon(self):
        self.assertEqual(HORIZON_MS, {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000})
        item = signal("LONG")
        simulate_tick_order(
            [item],
            [tick(item.anchor_ms, 99.9, 100.1), tick(item.anchor_ms + HORIZON_MS["4h"] + 1, 90.0, 90.2)],
            progress_every=0,
        )
        self.assertTrue(all(stop.hit_ms is None for stop in item.stops.values()))

if __name__ == "__main__":
    unittest.main()
