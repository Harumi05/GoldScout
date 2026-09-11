"""Deterministic contracts for the observation-only MT5 market dataset."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
OBSERVER_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketObserver.mqh"
SERVER_PATH = DASHBOARD / "server.py"
INDEX_PATH = DASHBOARD / "index.html"
sys.path.insert(0, str(DASHBOARD))

import market_observer_service as observer_service  # noqa: E402


FIXED_NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)


def record(event_id: str, timeframe: str, captured_at: int, **overrides) -> dict:
    value = {
        "event_id": event_id,
        "source": "MT5",
        "observer_only": True,
        "score_effect": 0,
        "snapshot_type": "BAR_CLOSE",
        "captured_at": captured_at,
        "timestamp": captured_at - 900,
        "symbol": "XAUUSD",
        "timeframe": timeframe,
        "decision": "NO_TRADE",
    }
    value.update(overrides)
    return value


@dataclass
class ClosedBarCadence:
    """Reference model for one append attempt per closed bar and timeframe."""

    last_bar: dict[str, int] = field(default_factory=dict)

    def poll(self, timeframe: str, closed_bar: int, persist) -> bool:
        if timeframe not in {"M15", "H1", "H4"} or closed_bar <= 0:
            return False
        if self.last_bar.get(timeframe) == closed_bar:
            return False
        if not persist(timeframe, closed_bar):
            return False
        self.last_bar[timeframe] = closed_bar
        return True


def decision_outcome(*, live_trading: bool, decision: str, reason: str = "") -> str:
    """Reference model for the dataset's non-executing decision taxonomy."""
    text = f"{decision} {reason}".lower()
    if live_trading and ("orden enviada" in text or "orden confirmada" in text):
        return "TRADE_TAKEN"
    if "señal simulada" in text or "senal simulada" in text:
        return "NO_TRADE"
    if "margen" in text or "margin" in text:
        return "BLOCKED_MARGIN"
    if "noticia" in text or "calendario" in text or "news" in text:
        return "BLOCKED_NEWS"
    if any(word in text for word in ("riesgo", "drawdown", "pérdida diaria", "spread", "presupuesto")):
        return "BLOCKED_RISK"
    if any(word in text for word in ("score final", "sin setup", "esperando condiciones", "armado ")):
        return "INSUFFICIENT_SCORE"
    return "NO_TRADE"


class MarketObserverServiceTests(unittest.TestCase):
    def setUp(self):
        observer_service.reset_cache()

    def test_m15_h1_h4_are_projected_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_observations.jsonl"
            rows = [
                record("m15", "M15", 1_789_052_400),
                record("h1", "H1", 1_789_052_401),
                record("h4", "H4", 1_789_052_402),
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            result = observer_service.observation_snapshot([path], now=FIXED_NOW)
        self.assertEqual(result["observation_count"], 3)
        self.assertEqual(set(result["latest_by_timeframe"]), {"M15", "H1", "H4"})
        self.assertEqual(result["latest_by_timeframe"]["M15"]["event_id"], "m15")

    def test_m30_and_untrusted_records_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_observations.jsonl"
            rows = [
                record("m30", "M30", 1_789_052_400),
                record("wrong-source", "M15", 1_789_052_401, source="other"),
                record("scored", "H1", 1_789_052_402, score_effect=4),
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            result = observer_service.observation_snapshot([path], now=FIXED_NOW)
        self.assertEqual(result["status"], "NO_DATA")
        self.assertEqual(result["observation_count"], 0)

    def test_reader_tracks_append_only_growth_incrementally(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_observations.jsonl"
            path.write_text(json.dumps(record("one", "M15", 1_789_052_400)) + "\n", encoding="utf-8")
            first = observer_service.observation_snapshot([path], now=FIXED_NOW)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record("two", "H1", 1_789_052_401)) + "\n")
            second = observer_service.observation_snapshot([path], now=FIXED_NOW)
        self.assertEqual(first["observation_count"], 1)
        self.assertEqual(second["observation_count"], 2)
        self.assertEqual(second["latest_by_timeframe"]["H1"]["event_id"], "two")

    def test_projection_forces_observation_only_and_zero_score_effect(self):
        empty = observer_service.observation_snapshot([])
        self.assertIs(empty["observer_only"], True)
        self.assertEqual(empty["score_effect"], 0)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_observations.jsonl"
            path.write_text(json.dumps(record("one", "M15", int(FIXED_NOW.timestamp()))) + "\n", encoding="utf-8")
            result = observer_service.observation_snapshot([path], now=FIXED_NOW)
        self.assertIs(result["observer_only"], True)
        self.assertEqual(result["score_effect"], 0)

    def test_last_decision_is_exposed_without_rewriting_dataset(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market_observations.jsonl"
            rows = [
                record("no-trade", "M15", 1_789_052_400, decision="NO_TRADE"),
                record("taken", "H1", 1_789_052_401, decision="TRADE_TAKEN"),
            ]
            payload = "".join(json.dumps(row) + "\n" for row in rows)
            path.write_text(payload, encoding="utf-8")
            result = observer_service.observation_snapshot([path], now=FIXED_NOW)
            after = path.read_text(encoding="utf-8")
        self.assertEqual(result["last_decision"], "TRADE_TAKEN")
        self.assertEqual(after, payload)

    def test_new_bar_is_saved_once_and_failed_persistence_is_retried(self):
        cadence = ClosedBarCadence()
        calls: list[tuple[str, int]] = []

        def persist(timeframe, bar):
            calls.append((timeframe, bar))
            return len(calls) > 1

        self.assertFalse(cadence.poll("M15", 100, persist))
        self.assertTrue(cadence.poll("M15", 100, persist))
        self.assertFalse(cadence.poll("M15", 100, persist))
        self.assertEqual(calls, [("M15", 100), ("M15", 100)])

    def test_persistence_failure_does_not_block_trading_flow(self):
        cadence = ClosedBarCadence()
        trading_flow_reached = False
        cadence.poll("H1", 200, lambda *_: False)
        trading_flow_reached = True
        self.assertTrue(trading_flow_reached)
        self.assertNotIn("H1", cadence.last_bar)

    def test_no_trade_and_paper_signal_are_never_labeled_as_real_trades(self):
        self.assertEqual(
            decision_outcome(live_trading=False, decision="Sin setup"),
            "INSUFFICIENT_SCORE",
        )
        self.assertEqual(
            decision_outcome(
                live_trading=False,
                decision="SEÑAL SIMULADA | riesgo 50.00 USD",
            ),
            "NO_TRADE",
        )

    def test_trade_taken_requires_live_execution_confirmation(self):
        self.assertEqual(
            decision_outcome(live_trading=True, decision="ORDEN ENVIADA"),
            "TRADE_TAKEN",
        )
        self.assertEqual(
            decision_outcome(live_trading=False, decision="ORDEN ENVIADA"),
            "NO_TRADE",
        )


class MarketObserverSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.observer = OBSERVER_PATH.read_text(encoding="utf-8")
        cls.server = SERVER_PATH.read_text(encoding="utf-8")
        cls.index = INDEX_PATH.read_text(encoding="utf-8")

    def test_ea_observer_uses_only_m15_h1_h4(self):
        self.assertIn("m_timeframes[0]=PERIOD_M15", self.observer)
        self.assertIn("m_timeframes[1]=PERIOD_H1", self.observer)
        self.assertIn("m_timeframes[2]=PERIOD_H4", self.observer)
        self.assertNotIn("PERIOD_M30", self.observer)

    def test_closed_bar_poll_deduplicates_and_retries_after_failure(self):
        self.assertIn("closedBar=iTime(m_symbol,m_timeframes[i],1)", self.observer)
        self.assertIn("closedBar==m_lastClosedBar[i]", self.observer)
        self.assertIn('if(Capture(i,1,"BAR_CLOSE","BAR_CLOSE",context))', self.observer)
        self.assertIn("m_lastClosedBar[i]=closedBar", self.observer)
        self.assertIn("LoadRecentIds();", self.observer)
        self.assertIn("if(IsRecentId(eventId)) return true;", self.observer)

    def test_dataset_contract_contains_market_indicators_and_future_placeholders(self):
        required = (
            '\\"observer_only\\":true,\\"score_effect\\":0',
            '\\"plus_di\\"',
            '\\"minus_di\\"',
            '\\"ema20\\"',
            '\\"ema200\\"',
            '\\"structure\\"',
            '\\"breakout\\"',
            '\\"momentum\\"',
            '\\"pullback\\"',
            '\\"recovery\\"',
            '\\"future_return_15m\\":null',
            '\\"mfe_4h\\":null',
            '\\"mae_4h\\":null',
        )
        for token in required:
            self.assertIn(token, self.observer)

    def test_missed_opportunity_outcomes_are_explicit(self):
        for outcome in (
            "TRADE_TAKEN",
            "NO_TRADE",
            "BLOCKED_RISK",
            "BLOCKED_NEWS",
            "BLOCKED_MARGIN",
            "INSUFFICIENT_SCORE",
        ):
            self.assertIn(outcome, self.observer)

    def test_special_snapshots_cover_required_state_transitions(self):
        for snapshot_type in (
            "SIGNAL_ARMED",
            "TRADE_EXECUTED",
            "RELEVANT_REJECTION",
            "STATE_CHANGE",
        ):
            self.assertIn(snapshot_type, self.observer)
        self.assertIn("CaptureMarketObserverState();", self.ea)

    def test_observer_is_polled_by_timer_without_gating_trading(self):
        on_timer = self.ea.split("void OnTimer()", 1)[1].split("void OnTick()", 1)[0]
        self.assertIn("PollMarketObserverClosedBars();", on_timer)
        self.assertNotIn("if(!PollMarketObserverClosedBars", on_timer)
        self.assertIn("trading y scoring continúan sin cambios", self.ea)

    def test_observer_include_has_no_execution_or_scoring_mutation(self):
        forbidden = (
            "PositionOpen(",
            "trade.Buy",
            "trade.Sell",
            "OrderSend(",
            "longScore+=",
            "shortScore+=",
            "MinScoreToTrade=",
            "RiskPercent=",
        )
        for token in forbidden:
            self.assertNotIn(token, self.observer)
        self.assertIn("input bool   EnableLiveTrading      = false", self.ea)
        self.assertIn("input double RiskPercent             = 5.0", self.ea)

    def test_dashboard_exposes_observer_without_removing_tradingview(self):
        self.assertIn("MARKET OBSERVER — MT5", self.index)
        self.assertIn("Solo observación · score_effect=0", self.index)
        self.assertIn("TRADINGVIEW — OBSERVACIÓN", self.index)
        self.assertIn("read_market_observer_snapshot", self.server)
        self.assertIn("d['tradingview']=read_tradingview()", self.server)


if __name__ == "__main__":
    unittest.main()
