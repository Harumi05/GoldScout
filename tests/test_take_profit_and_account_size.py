"""Deterministic tests for TP efficiency and minimum viable capital research."""

from __future__ import annotations

from pathlib import Path
import unittest

from research.analyze_historical_dataset import temporal_split
from research.analyze_stop_loss_quality import M1RangeIndex, SignalState
from research.analyze_take_profit_and_account_size import (
    TPSignal,
    account_can_execute,
    aggregate_capital,
    aggregate_tp,
    build_target_candidates,
    capital_detail_rows,
    classify_trade_class,
    minimum_equity_for_risk,
    risk_at_minimum_lot,
    simulate_tp_tick_order,
    summarize_capital,
    summarize_tp,
    tp_detail_rows,
)
from research.tick_historical_replay import Tick


ANCHOR = 1_700_000_000_000


def make_signal(direction: str = "LONG", setup: str = "MOMENTUM", trade_class: str = "CHILL") -> TPSignal:
    base = SignalState(
        event_id=f"tp-{direction}-{setup}-{trade_class}",
        anchor_ms=ANCHOR,
        period="TRAIN",
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
        momentum=setup == "MOMENTUM",
        breakout=setup == "BREAKOUT",
        pullback=setup == "PULLBACK",
        recovery=setup == "RECOVERY",
        session=None,
        spread=0.2,
        long_score=80.0,
        short_score=80.0,
        source_timeframe="H1",
    )
    return TPSignal(base, trade_class, 1.25 if trade_class == "CHILL" else 2.0)


class TakeProfitAndAccountSizeTests(unittest.TestCase):
    def test_chill_and_god_match_live_clear_classification(self):
        god = classify_trade_class(
            direction="LONG", h4_trend="BULLISH", h1_ema_trend="BULLISH",
            adx=25.0, rsi=55.0, hh=True, hl=True, lh=False, ll=False,
            breakout=False, pullback=False, htf_pullback=False,
        )
        chill = classify_trade_class(
            direction="SHORT", h4_trend="BEARISH", h1_ema_trend="BEARISH",
            adx=24.9, rsi=45.0, hh=False, hl=False, lh=True, ll=True,
            breakout=False, pullback=False, htf_pullback=False,
        )
        self.assertEqual(god, ("GOD", 2.0))
        self.assertEqual(chill, ("CHILL", 1.25))

    def test_only_requested_candidates_exist_for_each_class(self):
        _, _, chill = build_target_candidates(
            trade_class="CHILL", current_target_r=1.25, direction="LONG",
            entry_price=100.1, stop_price=98.9,
        )
        _, _, god = build_target_candidates(
            trade_class="GOD", current_target_r=2.0, direction="SHORT",
            entry_price=99.9, stop_price=101.1,
        )
        self.assertEqual(set(chill), {"CURRENT", "R_0_75", "R_1_0", "R_1_25"})
        self.assertEqual(set(god), {"CURRENT", "R_1_25", "R_1_5", "R_2_0"})

    def test_planned_r_uses_worst_allowed_fill(self):
        worst, risk_distance, targets = build_target_candidates(
            trade_class="CHILL", current_target_r=1.25, direction="LONG",
            entry_price=100.1, stop_price=98.9,
        )
        self.assertAlmostEqual(worst, 100.4)
        self.assertAlmostEqual(risk_distance, 1.5)
        self.assertAlmostEqual(targets["R_1_0"].price, 101.6)

    def test_tick_order_long_records_tp_before_later_sl(self):
        item = make_signal("LONG")
        ticks = [
            Tick(ANCHOR - 1, 120.0, 120.2),
            Tick(ANCHOR, 99.9, 100.1),
            Tick(ANCHOR + 1_000, 101.3, 101.5),
            Tick(ANCHOR + 2_000, 98.7, 98.9),
        ]
        simulate_tp_tick_order([item], ticks, progress_every=0)
        self.assertEqual(item.base.entry_tick_ms, ANCHOR)
        self.assertEqual(item.targets["R_0_75"].hit_ms, ANCHOR + 1_000)
        self.assertEqual(item.stop.hit_ms, ANCHOR + 2_000)

    def test_tick_order_short_uses_ask_for_tp_and_stop(self):
        item = make_signal("SHORT")
        ticks = [
            Tick(ANCHOR, 99.9, 100.1),
            Tick(ANCHOR + 1_000, 98.4, 98.6),
            Tick(ANCHOR + 2_000, 101.1, 101.3),
        ]
        simulate_tp_tick_order([item], ticks, progress_every=0)
        self.assertEqual(item.targets["R_0_75"].hit_ms, ANCHOR + 1_000)
        self.assertEqual(item.stop.hit_ms, ANCHOR + 2_000)

    def test_sl_before_tp_is_preserved_within_the_same_candle(self):
        item = make_signal("LONG")
        ticks = [
            Tick(ANCHOR, 99.9, 100.1),
            Tick(ANCHOR + 1_000, 98.7, 98.9),
            Tick(ANCHOR + 2_000, 101.3, 101.5),
        ]
        simulate_tp_tick_order([item], ticks, progress_every=0)
        ranges = M1RangeIndex([{"timestamp": ANCHOR // 60_000 * 60_000, "high": 101.4, "low": 98.8}])
        outcomes = {item.base.event_id: {"15m": {"future_return": 0.0, "mfe": 0.014, "mae": -0.012}}}
        row = next(
            row for row in tp_detail_rows([item], outcomes, ranges)
            if row["candidate"] == "R_0_75" and row["horizon"] == "15m"
        )
        self.assertTrue(row["sl_first"])
        self.assertFalse(row["tp_first"])
        self.assertLess(row["hypothetical_realized_r"], 0.0)

    def test_tick_before_anchor_never_changes_entry_or_outcome(self):
        item = make_signal("LONG")
        simulate_tp_tick_order(
            [item], [Tick(ANCHOR - 1, 200.0, 200.2), Tick(ANCHOR, 99.9, 100.1)],
            progress_every=0,
        )
        self.assertEqual(item.base.entry_tick_ms, ANCHOR)
        self.assertIsNone(item.stop.hit_ms)
        self.assertTrue(all(target.hit_ms is None for target in item.targets.values()))

    def test_realized_r_is_only_assigned_after_tp_or_sl(self):
        item = make_signal("LONG")
        simulate_tp_tick_order([item], [Tick(ANCHOR, 99.9, 100.1)], progress_every=0)
        ranges = M1RangeIndex([{"timestamp": ANCHOR // 60_000 * 60_000, "high": 100.0, "low": 100.0}])
        outcomes = {item.base.event_id: {"15m": {"future_return": 0.0, "mfe": 0.0, "mae": 0.0}}}
        rows = tp_detail_rows([item], outcomes, ranges)
        row = next(row for row in rows if row["candidate"] == "CURRENT" and row["horizon"] == "15m")
        self.assertIsNone(row["hypothetical_realized_r"])
        self.assertTrue(row["unresolved"])

    def test_too_far_and_too_short_definitions(self):
        too_far = {
            "tp_hit": False, "sl_hit": True, "tp_first": False, "sl_first": True, "unresolved": False,
            "hypothetical_realized_r": -0.8, "time_to_tp_minutes": None,
            "time_to_sl_minutes": 2.0, "mfe_r": 0.8, "mae_r": 1.0,
            "mfe_lost_after_tp_r": None, "tp_too_far": True, "tp_too_short": False,
        }
        too_short = {
            **too_far, "tp_hit": True, "sl_hit": False, "tp_first": True, "sl_first": False,
            "hypothetical_realized_r": 1.0, "tp_too_far": False,
            "tp_too_short": True, "mfe_lost_after_tp_r": 0.6,
        }
        summary = summarize_tp([too_far, too_short])
        self.assertEqual(summary["too_far_rate"], 0.5)
        self.assertEqual(summary["too_short_rate"], 0.5)
        self.assertEqual(summary["resolved_cases"], 2)
        self.assertAlmostEqual(summary["expectancy_r_resolved"], 0.1)
        self.assertAlmostEqual(summary["profit_factor_resolved"], 1.25)

    def test_capital_com_min_lot_risk_and_minimum_equity(self):
        risk = risk_at_minimum_lot(
            direction="LONG", entry_price=100.1, stop_price=98.9,
            contract_size=100.0, volume_min=0.01,
        )
        self.assertAlmostEqual(risk, 1.5)
        self.assertAlmostEqual(minimum_equity_for_risk(risk), 30.0)

    def test_account_sizes_respect_trade_and_daily_five_percent_budget(self):
        self.assertFalse(account_can_execute(200.0, 10.01))
        self.assertTrue(account_can_execute(200.0, 10.0))
        self.assertTrue(account_can_execute(500.0, 25.0))
        self.assertFalse(account_can_execute(500.0, 25.01))
        self.assertTrue(account_can_execute(1000.0, 50.0))

    def test_capital_percentiles_and_execution_rates(self):
        rows = []
        for equity in (100.0, 200.0, 300.0, 400.0):
            row = {"minimum_equity": equity, "risk_at_min_lot": equity * 0.05}
            for account in (200, 300, 500, 750, 1000):
                row[f"executable_{account}"] = equity <= account
            rows.append(row)
        summary = summarize_capital(rows)
        self.assertAlmostEqual(summary["minimum_equity_p50"], 250.0)
        self.assertEqual(summary["executable_count_200"], 2)
        self.assertEqual(summary["min_lot_risk_too_high_200"], 2)

    def test_aggregates_keep_long_short_setup_and_zero_score_effect(self):
        base = {
            "event_id": "e", "period": "TRAIN", "trade_class": "CHILL",
            "direction": "LONG", "setup": "BREAKOUT", "candidate": "CURRENT",
            "target_r": 1.25, "horizon": "4h", "tp_hit": True, "sl_hit": False,
            "tp_first": True, "sl_first": False, "unresolved": False,
            "time_to_tp_minutes": 10.0, "time_to_sl_minutes": None,
            "hypothetical_realized_r": 1.25, "mfe_r": 1.4, "mae_r": 0.2,
            "mfe_lost_after_tp_r": 0.1, "tp_too_far": False, "tp_too_short": False,
        }
        output = aggregate_tp([base])
        setup = next(row for row in output if row["scope"] == "SETUP" and row["period"] == "TRAIN")
        self.assertEqual((setup["direction"], setup["setup"]), ("LONG", "BREAKOUT"))
        self.assertEqual(setup["score_effect"], 0)

    def test_capital_rows_do_not_change_stop_to_fit_small_accounts(self):
        item = make_signal("LONG")
        simulate_tp_tick_order([item], [Tick(ANCHOR, 99.9, 100.1)], progress_every=0)
        original = item.stop.price
        rows = capital_detail_rows([item])
        self.assertEqual(item.stop.price, original)
        self.assertIn(rows[0]["status_200"], {"EXECUTABLE", "MIN_LOT_RISK_TOO_HIGH"})
        aggregate = aggregate_capital(rows)
        self.assertTrue(all(row["score_effect"] == 0 for row in aggregate))

    def test_split_is_sixty_twenty_twenty(self):
        records = [{"event_id": str(index), "_timestamp_ms": index, "timestamp": index} for index in range(10)]
        _, counts = temporal_split(records)
        self.assertEqual(counts, {"TRAIN": 6, "VALIDATION": 2, "OOS": 2})

    def test_live_ea_risk_thresholds_and_execution_are_untouched(self):
        source = (Path(__file__).resolve().parents[1] / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8-sig")
        self.assertRegex(source, r"input\s+bool\s+EnableLiveTrading\s*=\s*false;")
        self.assertRegex(source, r"input\s+double\s+RiskPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+double\s+DailyLossLimitPercent\s*=\s*5\.0;")
        self.assertRegex(source, r"input\s+int\s+ArmScoreThreshold\s*=\s*58;")
        self.assertRegex(source, r"input\s+int\s+MinScoreToTrade\s*=\s*74;")


if __name__ == "__main__":
    unittest.main()
