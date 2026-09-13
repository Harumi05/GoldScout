"""Deterministic tests for append-only forward outcome labels."""

from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
OBSERVER_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketObserver.mqh"
OUTCOMES_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketOutcomes.mqh"
DOC_PATH = ROOT / "docs" / "MARKET_OBSERVER_ES.md"
HORIZONS = {"15m": 15 * 60, "1h": 60 * 60, "4h": 4 * 60 * 60}


@dataclass(frozen=True)
class MinuteBar:
    timestamp: int
    high: float
    low: float
    close: float


def calculate_horizon(*, anchor, initial_price, horizon, now, bars):
    """Reference model: only complete M1 bars strictly available by horizon."""
    target = anchor + horizon
    if anchor <= 0 or initial_price <= 0 or now < target:
        return None
    start_minute = ((anchor + 59) // 60) * 60
    end_minute = (target // 60) * 60 - 60
    expected_times = list(range(start_minute, end_minute + 1, 60))
    selected = [bar for bar in bars if start_minute <= bar.timestamp <= end_minute]
    if [bar.timestamp for bar in selected] != expected_times:
        return None
    maximum = max([initial_price, *(bar.high for bar in selected)])
    minimum = min([initial_price, *(bar.low for bar in selected)])
    final_close = selected[-1].close
    return {
        "future_return": (final_close - initial_price) / initial_price,
        "mfe": max(0.0, (maximum - initial_price) / initial_price),
        "mae": min(0.0, (minimum - initial_price) / initial_price),
    }


def minute_bars(anchor, count, *, initial=100.0):
    return [
        MinuteBar(
            timestamp=anchor + index * 60,
            high=initial + 1.0 + index * 0.01,
            low=initial - 0.5,
            close=initial + index * 0.01,
        )
        for index in range(count)
    ]


class AppendOnlyOutcomeLedger:
    """Reference append/dedup model using deterministic event+horizon identity."""

    def __init__(self, path):
        self.path = Path(path)
        self.ids = set()
        self.values = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.ids.add(row["outcome_id"])
                    event_values = self.values.setdefault(row["event_id"], {})
                    for completed in HORIZONS:
                        value = row.get(f"future_return_{completed}")
                        if value is not None:
                            event_values[completed] = {
                                "future_return": value,
                                "mfe": row[f"mfe_{completed}"],
                                "mae": row[f"mae_{completed}"],
                            }

    def append(self, event_id, horizon, values):
        outcome_id = f"{event_id}|{horizon}"
        if outcome_id in self.ids:
            return False
        event_values = self.values.setdefault(event_id, {})
        event_values[horizon] = values
        row = {
            "outcome_id": outcome_id,
            "event_id": event_id,
            "evaluated_at": 1_789_052_400,
            "source": "MT5",
            "observer_only": True,
            "score_effect": 0,
            "return_convention": "LONG_XAUUSD_DECIMAL",
            "status": "COMPLETED",
            "horizon": horizon,
            "completed_horizon": horizon,
            "future_return_15m": None,
            "future_return_1h": None,
            "future_return_4h": None,
            "mfe_15m": None,
            "mae_15m": None,
            "mfe_1h": None,
            "mae_1h": None,
            "mfe_4h": None,
            "mae_4h": None,
        }
        for suffix, completed_values in event_values.items():
            row[f"future_return_{suffix}"] = completed_values["future_return"]
            row[f"mfe_{suffix}"] = completed_values["mfe"]
            row[f"mae_{suffix}"] = completed_values["mae"]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.ids.add(outcome_id)
        return True


@dataclass
class QueueEvent:
    event_id: str
    states: dict[str, str]


class OutcomeQueueModel:
    """Reference model for useful-work limits and terminal gap outcomes."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.ids = {row["outcome_id"] for row in self.rows}
        self.pending = []

    def track(self, event):
        self.pending.append(event)

    def poll(self, maximum_events=8):
        useful_work = 0
        index = 0
        while index < len(self.pending) and useful_work < maximum_events:
            event = self.pending[index]
            for horizon in HORIZONS:
                outcome_id = f"{event.event_id}|{horizon}"
                if outcome_id in self.ids:
                    continue
                state = event.states.get(horizon, "PENDING")
                if state == "PENDING":
                    break
                row = {
                    "outcome_id": outcome_id,
                    "event_id": event.event_id,
                    "horizon": horizon,
                    "completed_horizon": horizon,
                    "status": "UNRESOLVABLE_GAP" if state == "UNRESOLVABLE_GAP" else "COMPLETED",
                    "future_return_15m": None,
                    "future_return_1h": None,
                    "future_return_4h": None,
                    "mfe_15m": None,
                    "mae_15m": None,
                    "mfe_1h": None,
                    "mae_1h": None,
                    "mfe_4h": None,
                    "mae_4h": None,
                }
                if state == "RESOLVABLE":
                    row[f"future_return_{horizon}"] = 0.01
                    row[f"mfe_{horizon}"] = 0.02
                    row[f"mae_{horizon}"] = -0.005
                self.rows.append(row)
                self.ids.add(outcome_id)
                useful_work += 1
                if useful_work >= maximum_events:
                    break
            if all(f"{event.event_id}|{horizon}" in self.ids for horizon in HORIZONS):
                self.pending.pop(index)
            else:
                index += 1
        return useful_work


class MarketOutcomeCalculationTests(unittest.TestCase):
    def test_15_minute_label_uses_exactly_15_closed_minutes(self):
        anchor = 1_800_000_000
        result = calculate_horizon(
            anchor=anchor,
            initial_price=100.0,
            horizon=HORIZONS["15m"],
            now=anchor + HORIZONS["15m"],
            bars=minute_bars(anchor, 15),
        )
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["future_return"], 0.0014)

    def test_one_hour_label_uses_60_closed_minutes(self):
        anchor = 1_800_000_000
        result = calculate_horizon(
            anchor=anchor,
            initial_price=100.0,
            horizon=HORIZONS["1h"],
            now=anchor + HORIZONS["1h"],
            bars=minute_bars(anchor, 60),
        )
        self.assertAlmostEqual(result["future_return"], 0.0059)

    def test_four_hour_label_uses_240_closed_minutes(self):
        anchor = 1_800_000_000
        result = calculate_horizon(
            anchor=anchor,
            initial_price=100.0,
            horizon=HORIZONS["4h"],
            now=anchor + HORIZONS["4h"],
            bars=minute_bars(anchor, 240),
        )
        self.assertAlmostEqual(result["future_return"], 0.0239)

    def test_incomplete_history_keeps_result_null(self):
        anchor = 1_800_000_000
        bars = minute_bars(anchor, 15)
        result = calculate_horizon(
            anchor=anchor,
            initial_price=100.0,
            horizon=HORIZONS["15m"],
            now=anchor + HORIZONS["15m"],
            bars=bars[:7] + bars[8:],
        )
        self.assertIsNone(result)

    def test_horizon_is_never_evaluated_before_real_time_elapsed(self):
        anchor = 1_800_000_000
        result = calculate_horizon(
            anchor=anchor,
            initial_price=100.0,
            horizon=HORIZONS["15m"],
            now=anchor + HORIZONS["15m"] - 1,
            bars=minute_bars(anchor, 15),
        )
        self.assertIsNone(result)

    def test_long_xauusd_directional_convention_is_bounded_by_initial_price(self):
        anchor = 1_800_000_000
        bars = minute_bars(anchor, 15)
        bars[4] = MinuteBar(bars[4].timestamp, 105.0, 99.0, 101.0)
        bars[-1] = MinuteBar(bars[-1].timestamp, 102.0, 96.0, 98.0)
        result = calculate_horizon(
            anchor=anchor,
            initial_price=100.0,
            horizon=HORIZONS["15m"],
            now=anchor + HORIZONS["15m"],
            bars=bars,
        )
        self.assertAlmostEqual(result["future_return"], -0.02)
        self.assertAlmostEqual(result["mfe"], 0.05)
        self.assertAlmostEqual(result["mae"], -0.04)

    def test_intrabar_anchor_excludes_the_partial_minute_before_capture(self):
        anchor = 1_800_000_005
        bars = minute_bars(1_800_000_000, 16)
        result = calculate_horizon(
            anchor=anchor,
            initial_price=100.0,
            horizon=HORIZONS["15m"],
            now=anchor + HORIZONS["15m"],
            bars=bars,
        )
        self.assertIsNotNone(result)
        self.assertNotEqual(bars[0].timestamp, ((anchor + 59) // 60) * 60)


class MarketOutcomePersistenceTests(unittest.TestCase):
    def test_horizons_are_append_only_and_have_distinct_deterministic_ids(self):
        values = {"future_return": 0.01, "mfe": 0.02, "mae": -0.005}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_outcomes.jsonl"
            ledger = AppendOnlyOutcomeLedger(path)
            self.assertTrue(ledger.append("event-1", "15m", values))
            self.assertTrue(ledger.append("event-1", "1h", values))
            self.assertTrue(ledger.append("event-1", "4h", values))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row["outcome_id"] for row in rows}), 3)
        self.assertTrue(all(row["event_id"] == "event-1" for row in rows))

    def test_restart_recovers_ids_and_does_not_duplicate_outcome(self):
        values = {"future_return": 0.01, "mfe": 0.02, "mae": -0.005}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_outcomes.jsonl"
            self.assertTrue(AppendOnlyOutcomeLedger(path).append("event-1", "15m", values))
            restarted = AppendOnlyOutcomeLedger(path)
            self.assertFalse(restarted.append("event-1", "15m", values))
            self.assertTrue(restarted.append("event-1", "1h", values))
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[-1]["future_return_15m"], values["future_return"])
            self.assertEqual(rows[-1]["future_return_1h"], values["future_return"])

    def test_every_outcome_is_observer_only_with_zero_score_effect(self):
        values = {"future_return": 0.01, "mfe": 0.02, "mae": -0.005}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_outcomes.jsonl"
            AppendOnlyOutcomeLedger(path).append("event-1", "15m", values)
            row = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(row["observer_only"], True)
        self.assertEqual(row["score_effect"], 0)


class MarketOutcomeQueueTests(unittest.TestCase):
    @staticmethod
    def event(event_id, state="PENDING", horizons=("15m",)):
        return QueueEvent(event_id, {horizon: state for horizon in horizons})

    def test_one_permanent_gap_does_not_block_later_valid_outcome(self):
        queue = OutcomeQueueModel()
        queue.track(self.event("gap", "UNRESOLVABLE_GAP"))
        queue.track(self.event("valid", "RESOLVABLE"))
        queue.poll()
        self.assertIn("valid|15m", queue.ids)

    def test_eight_permanent_gaps_allow_later_outcome_on_next_poll(self):
        queue = OutcomeQueueModel()
        for index in range(8):
            queue.track(self.event(f"gap-{index}", "UNRESOLVABLE_GAP"))
        queue.track(self.event("valid", "RESOLVABLE"))
        self.assertEqual(queue.poll(), 8)
        self.assertNotIn("valid|15m", queue.ids)
        queue.poll()
        self.assertIn("valid|15m", queue.ids)

    def test_more_than_eight_permanent_gaps_eventually_drain(self):
        queue = OutcomeQueueModel()
        for index in range(17):
            queue.track(self.event(f"gap-{index}", "UNRESOLVABLE_GAP"))
        queue.track(self.event("valid", "RESOLVABLE"))
        for _ in range(4):
            queue.poll()
        self.assertIn("valid|15m", queue.ids)

    def test_weekend_gap_is_terminal_for_all_horizons(self):
        queue = OutcomeQueueModel()
        queue.track(self.event("weekend", "UNRESOLVABLE_GAP", tuple(HORIZONS)))
        queue.poll()
        rows = [row for row in queue.rows if row["event_id"] == "weekend"]
        self.assertEqual({row["horizon"] for row in rows}, set(HORIZONS))
        self.assertTrue(all(row["status"] == "UNRESOLVABLE_GAP" for row in rows))

    def test_gap_rows_cover_15m_1h_and_4h_with_null_values(self):
        queue = OutcomeQueueModel()
        queue.track(self.event("gap-all", "UNRESOLVABLE_GAP", tuple(HORIZONS)))
        queue.poll()
        for row in queue.rows:
            horizon = row["horizon"]
            self.assertIsNone(row[f"future_return_{horizon}"])
            self.assertIsNone(row[f"mfe_{horizon}"])
            self.assertIsNone(row[f"mae_{horizon}"])

    def test_restart_does_not_retry_terminal_gap(self):
        first = OutcomeQueueModel()
        first.track(self.event("gap", "UNRESOLVABLE_GAP"))
        first.poll()
        restarted = OutcomeQueueModel(first.rows)
        restarted.track(self.event("gap", "UNRESOLVABLE_GAP"))
        restarted.poll()
        self.assertEqual(len(restarted.rows), len(first.rows))

    def test_repeated_polls_do_not_duplicate_terminal_ids(self):
        queue = OutcomeQueueModel()
        queue.track(self.event("gap", "UNRESOLVABLE_GAP"))
        queue.poll()
        queue.poll()
        self.assertEqual(len(queue.ids), len(queue.rows))

    def test_pending_work_does_not_consume_useful_work_limit(self):
        queue = OutcomeQueueModel()
        for index in range(12):
            queue.track(self.event(f"pending-{index}"))
        queue.track(self.event("valid", "RESOLVABLE"))
        self.assertEqual(queue.poll(maximum_events=1), 1)
        self.assertIn("valid|15m", queue.ids)

    def test_null_gap_is_preserved_while_later_event_completes(self):
        queue = OutcomeQueueModel()
        queue.track(self.event("gap", "UNRESOLVABLE_GAP"))
        queue.track(self.event("valid", "RESOLVABLE"))
        queue.poll()
        gap = next(row for row in queue.rows if row["event_id"] == "gap")
        valid = next(row for row in queue.rows if row["event_id"] == "valid")
        self.assertIsNone(gap["future_return_15m"])
        self.assertEqual(valid["future_return_15m"], 0.01)


class MarketOutcomeSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.observer = OBSERVER_PATH.read_text(encoding="utf-8")
        cls.outcomes = OUTCOMES_PATH.read_text(encoding="utf-8")
        cls.documentation = DOC_PATH.read_text(encoding="utf-8")

    def test_outcome_file_and_all_required_fields_are_present(self):
        self.assertIn('MarketOutcomeFile        = "market_outcomes.jsonl"', self.ea)
        for field in (
            "event_id",
            "evaluated_at",
            "status",
            "horizon",
            "future_return_15m",
            "future_return_1h",
            "future_return_4h",
            "mfe_15m",
            "mae_15m",
            "mfe_1h",
            "mae_1h",
            "mfe_4h",
            "mae_4h",
        ):
            self.assertIn(f'\\"{field}\\"', self.outcomes)

    def test_terminal_calculation_uses_only_closed_m1_history_after_elapsed_time(self):
        self.assertIn("CopyRates(m_symbol,PERIOD_M1,startMinute,endMinute,bars)", self.outcomes)
        self.assertIn("if(now<target) return GOLDSCOUT_OUTCOME_PENDING;", self.outcomes)
        self.assertIn("SERIES_SYNCHRONIZED", self.outcomes)
        self.assertIn("SERIES_LASTBAR_DATE", self.outcomes)
        self.assertIn("return GOLDSCOUT_OUTCOME_UNRESOLVABLE_GAP;", self.outcomes)
        self.assertNotIn("PERIOD_M30", self.outcomes)

    def test_gap_status_is_terminal_and_only_useful_work_consumes_limit(self):
        self.assertIn('"UNRESOLVABLE_GAP":"COMPLETED"', self.outcomes)
        self.assertIn('json+="\\"status\\":\\""+status+"\\",";', self.outcomes)
        self.assertIn("int usefulWork=0;", self.outcomes)
        self.assertIn("usefulWork++;", self.outcomes)
        self.assertNotIn("if(due) processed++;", self.outcomes)

    def test_restart_recovery_and_deduplication_are_explicit(self):
        self.assertIn("ReadRecentOutcomeIds();", self.outcomes)
        self.assertIn("RecoverPendingObservations();", self.outcomes)
        self.assertIn("if(IsRecentOutcomeId(outcomeId)) return true;", self.outcomes)
        self.assertIn("outcomeId=OutcomeId(pending.eventId,horizonIndex)", self.outcomes)
        self.assertIn("if((m_pending[i].completedMask&bit)!=0) continue;", self.outcomes)
        self.assertNotIn("for(int prior=0;prior<=horizon;prior++)", self.outcomes)
        self.assertIn(
            "StringCompare(observationFilename,outcomeFilename,false)==0",
            self.outcomes,
        )

    def test_outcomes_are_polled_without_gating_existing_trading_flow(self):
        self.assertIn("g_marketObserver.PollOutcomes();", self.ea)
        self.assertNotIn("if(!g_marketObserver.PollOutcomes", self.ea)
        self.assertNotIn("PositionOpen(", self.outcomes)
        self.assertNotIn("OrderSend(", self.outcomes)

    def test_scoring_risk_and_execution_defaults_remain_unchanged(self):
        self.assertIn("input bool   EnableLiveTrading      = false", self.ea)
        self.assertIn("input double RiskPercent             = 5.0", self.ea)
        self.assertIn("input int    ArmScoreThreshold       = 58", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74", self.ea)
        for forbidden in ("longScore+=", "shortScore+=", "RiskPercent=", "MinScoreToTrade="):
            self.assertNotIn(forbidden, self.outcomes)

    def test_directional_convention_and_zero_effect_are_documented_and_persisted(self):
        self.assertIn("LONG_XAUUSD_DECIMAL", self.outcomes)
        self.assertIn('\\"observer_only\\":true,\\"score_effect\\":0', self.outcomes)
        self.assertIn("perspectiva LONG de XAUUSD", self.documentation)
        self.assertIn("`score_effect=0`", self.documentation)


if __name__ == "__main__":
    unittest.main()
