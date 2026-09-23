"""Phase-3 causal regime contracts and post-freeze analysis, synthetic only."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from research.analyze_regime_edge import (analyze_rows, causal_direction, causal_setups,
                                           directional_outcome, sample_status)
from research.gold_regime_engine import (CausalFrame, LookAheadError, PARAMETER_VERSION,
                                          alignment, asof, causal_higher_bar, causal_m5,
                                          classify, features_for_frames, fit_training_thresholds,
                                          RegimeHysteresis, split_for_index, wrap_higher_bars)


M5 = 300_000
H1 = 3_600_000
H4 = 14_400_000


def m5(index: int, *, close: float = 100.0, closed: bool = True) -> dict:
    start = index * M5
    return {"bar_id": f"M5-{index}", "source": "HISTORICAL_MT5_TICKS", "symbol": "XAUUSD",
            "timeframe": "M5", "timestamp_unit": "epoch_ms_mt5_server_wall_time",
            "start": start, "end": start + M5, "available_at": start + 2 * M5,
            "open": close, "high": close + 1, "low": close - 1, "close": close,
            "tick_count": 10, "avg_spread": .3, "closed": closed,
            "provenance": {"aggregation": "CLOSED_M1_ONLY", "source_bar_ids": [f"M1-{index}"]}}


def higher(timeframe: str, *, closed: bool = True) -> dict:
    duration = H1 if timeframe == "H1" else H4
    return {"bar_id": f"{timeframe}-0", "source": "HISTORICAL_MT5_TICKS", "symbol": "XAUUSD",
            "timeframe": timeframe, "timestamp_unit": "epoch_ms_mt5_server_wall_time",
            "timestamp": 0, "close_timestamp": duration, "open": 100.0, "high": 101.0,
            "low": 99.0, "close": 100.0, "tick_count": 100,
            "avg_spread": .3, "closed": closed}


def feature(**changes: object) -> dict:
    value = {"atr": 1.0, "ema20_slope_atr": .5, "structure": "BULLISH",
             "atr_percentile": .5, "range_percentile": .5, "spread_percentile": .5}
    value.update(changes)
    return value


PARAMS = {"atr_low": .7, "atr_high": 1.3, "slope_entry": .2, "slope_exit": .1}


class CausalWrapperTests(unittest.TestCase):
    def test_no_open_m5(self):
        with self.assertRaises(LookAheadError):
            causal_m5(m5(0, closed=False))

    def test_no_open_h1_and_h4(self):
        for timeframe, confirming in (("H1", 12), ("H4", 48)):
            with self.subTest(timeframe=timeframe), self.assertRaises(LookAheadError):
                causal_higher_bar(higher(timeframe, closed=False), causal_m5(m5(confirming)), timeframe)

    def test_h1_h4_require_later_closed_m5_and_availability(self):
        for timeframe, confirming in (("H1", 12), ("H4", 48)):
            row = higher(timeframe)
            too_early = causal_m5(m5(confirming - 1))
            with self.subTest(timeframe=timeframe), self.assertRaises(LookAheadError):
                causal_higher_bar(row, too_early, timeframe)
            bar = causal_higher_bar(row, causal_m5(m5(confirming)), timeframe)
            self.assertGreater(bar.available_at, bar.end)
            self.assertEqual(bar.provenance["confirmation_bar_id"], f"M5-{confirming}")
            with self.assertRaises(LookAheadError):
                bar.require(bar.available_at - 1)
            bar.require(bar.available_at)

    def test_no_future_higher_bar_without_confirming_m5(self):
        self.assertEqual(wrap_higher_bars([higher("H1")], [causal_m5(m5(11))], "H1"), [])

    def test_asof_never_returns_open_or_future_bar(self):
        bars = [causal_m5(m5(0)), causal_m5(m5(1))]
        self.assertIsNone(asof(bars, 2 * M5 - 1)[1])
        self.assertEqual(asof(bars, 2 * M5)[1].bar_id, "M5-0")
        with self.assertRaises(LookAheadError):
            asof([replace(bars[0], closed=False)], 2 * M5)

    def test_future_bar_cannot_change_past_features_or_pivot(self):
        bars = [causal_m5(m5(i, close=100 + (i % 7))) for i in range(50)]
        before = features_for_frames(bars)
        after = features_for_frames(bars + [causal_m5(m5(50, close=200))])
        self.assertEqual(before, after[:-1])


class RegimeTests(unittest.TestCase):
    def test_train_only_thresholds_ignore_oos(self):
        values = [feature(atr=.5 + i / 100, ema20_slope_atr=i / 100) for i in range(50)]
        times = list(range(50))
        a = fit_training_thresholds(values, times, 39)
        b = fit_training_thresholds(values + [feature(atr=999, ema20_slope_atr=999)], times + [100], 39)
        self.assertEqual(a, b)
        self.assertEqual(a["trained_through"], 39)
        with self.assertRaises(ValueError):
            fit_training_thresholds(values, times, 39, evaluation_time=38)

    def test_60_20_20_split(self):
        self.assertEqual([split_for_index(i, 10) for i in range(10)],
                         ["TRAIN"] * 6 + ["VALIDATION"] * 2 + ["OOS"] * 2)

    def test_volatility_expansion_and_contraction(self):
        self.assertEqual(classify(feature(atr=2), PARAMS)["volatility_state"], "EXPANSION")
        self.assertEqual(classify(feature(atr=.4), PARAMS)["volatility_state"], "CONTRACTION")

    def test_trend_up_down_and_range(self):
        self.assertEqual(classify(feature(), PARAMS)["gold_regime"], "TREND_UP")
        self.assertEqual(classify(feature(ema20_slope_atr=-.5, structure="BEARISH"), PARAMS)["gold_regime"], "TREND_DOWN")
        self.assertEqual(classify(feature(ema20_slope_atr=0, structure="RANGE"), PARAMS)["gold_regime"], "RANGE")

    def test_transition_and_shock_are_known_at_t(self):
        self.assertEqual(classify(feature(ema20_slope_atr=-.5, structure="BEARISH"), PARAMS, "UP")["gold_regime"], "TRANSITION")
        self.assertEqual(classify(feature(ema20_slope_atr=0, structure="RANGE"), PARAMS, "UP")["gold_regime"], "TRANSITION")
        shock = feature(atr_percentile=.99, range_percentile=.99)
        self.assertEqual(classify(shock, PARAMS)["gold_regime"], "EVENT_SHOCK")

    def test_missing_data_is_unknown_and_deterministic(self):
        self.assertEqual(classify({}, PARAMS)["gold_regime"], "UNKNOWN")
        self.assertEqual(classify(feature(), PARAMS), classify(feature(), PARAMS))

    def test_hysteresis_needs_distinct_h1_bars(self):
        machine = RegimeHysteresis(minimum_bars=2)
        self.assertEqual(machine.update("TREND_UP", .8, 1)[0], "UNKNOWN")
        self.assertEqual(machine.update("TREND_UP", .8, 1)[0], "UNKNOWN")
        self.assertEqual(machine.update("TREND_UP", .8, 2)[0], "TREND_UP")
        self.assertEqual(machine.update("RANGE", .8, 3)[0], "TREND_UP")
        self.assertEqual(machine.update("TREND_UP", .8, 4)[0], "TREND_UP")

    def test_contemporaneous_shock_can_enter_immediately(self):
        machine = RegimeHysteresis(minimum_bars=2)
        self.assertEqual(machine.update("EVENT_SHOCK", 1.0, 1)[0], "EVENT_SHOCK")
        self.assertEqual(machine.update("TREND_UP", .8, 2)[0], "EVENT_SHOCK")

    def test_low_confidence_breaks_persistence_streak(self):
        machine = RegimeHysteresis(minimum_bars=2)
        self.assertEqual(machine.update("TREND_UP", .8, 1)[0], "UNKNOWN")
        self.assertEqual(machine.update("TREND_UP", .1, 2)[0], "UNKNOWN")
        self.assertEqual(machine.update("TREND_UP", .8, 3)[0], "UNKNOWN")

    def test_alignment_preserves_pullback_without_signal(self):
        a, detail = alignment({"trend_state": "DOWN"}, {"trend_state": "UP"}, {"trend_state": "UP"})
        self.assertEqual((a, detail), ("ALIGNED_UP", "higher_tf_up_with_lower_tf_pullback"))

    def test_no_session_dxy_yields_or_ea_dependency(self):
        source = (Path(__file__).resolve().parents[1] / "research/gold_regime_engine.py").read_text(encoding="utf-8")
        self.assertNotIn("session=", source)
        self.assertNotIn("dxy", source.lower().replace("dxy and yields", ""))
        self.assertNotIn("OrderSend", source)


class AnalysisTests(unittest.TestCase):
    def test_direction_is_causal_and_outcome_applied_after(self):
        self.assertEqual(causal_direction({"long_score": 60, "short_score": 40}), "LONG")
        self.assertEqual(causal_direction({"long_score": 40, "short_score": 60}), "SHORT")
        self.assertIsNone(causal_direction({"long_score": 50, "short_score": 50}))
        outcome = {"completed_horizon": "1h", "future_return_1h": .01, "mfe_1h": .02, "mae_1h": -.005}
        self.assertEqual(directional_outcome(outcome, "LONG"), (.01, .02, -.005))
        self.assertEqual(directional_outcome(outcome, "SHORT"), (-.01, .005, -.02))

    def test_setups_directional_and_continuation_unavailable(self):
        observation = {"momentum": "LONG", "breakout": "SHORT", "pullback": "NONE"}
        self.assertEqual(causal_setups(observation, "LONG"), ["MOMENTUM"])
        self.assertEqual(causal_setups(observation, "SHORT"), ["BREAKOUT"])
        self.assertEqual(sample_status(6), "INSUFFICIENT")
        self.assertEqual(sample_status(50), "THIN_SAMPLE")
        self.assertEqual(sample_status(100), "SUFFICIENT")

    def test_post_freeze_join_and_diagnostic_contract(self):
        row = {"event_id": "e1", "decision_time": 10, "parameter_version": PARAMETER_VERSION,
               "diagnostic_only": True, "score_effect": 0, "gold_regime": "TREND_UP", "split": "TRAIN",
               "h1": {"bar_id": "h1", "end": 9, "available_at": 10},
               "long_score": 70, "short_score": 30, "decision": "ARMED"}
        obs = {"e1": {"observed_at": 10, "momentum": "LONG"}}
        outcome = {"e1": {"completed_horizon": "1h", "future_return_1h": .01,
                          "mfe_1h": .02, "mae_1h": -.003}}
        rows, meta = analyze_rows([row], obs, outcome)
        self.assertEqual(meta["valid_outcome_rows"], 1)
        self.assertTrue(any(item["setup"] == "MOMENTUM" for item in rows))
        with self.assertRaises(ValueError):
            analyze_rows([{**row, "score_effect": 1}], obs, outcome)
        with self.assertRaises(ValueError):
            analyze_rows([row, row], obs, outcome)
        with self.assertRaises(ValueError):
            analyze_rows([{**row, "h1": {"bar_id": "h1", "available_at": 11, "end": 10}}], obs, outcome)


if __name__ == "__main__":
    unittest.main()
