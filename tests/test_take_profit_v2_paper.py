"""Deterministic contracts for the PAPER-only Take Profit v2 integration."""

from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
POLICY_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "TakeProfitPolicy.mqh"
OBSERVER_PATH = ROOT / "MT5" / "Include" / "GoldScout" / "MarketObserver.mqh"
DASHBOARD_PATH = ROOT / "dashboard" / "index.html"
SERVER_PATH = ROOT / "dashboard" / "server.py"


@dataclass(frozen=True)
class Obstacle:
    price: float
    confidence: str


def align(price: float, tick_size: float, *, up: bool) -> float:
    ticks = price / tick_size
    return (ceil(ticks - 1e-9) if up else floor(ticks + 1e-9)) * tick_size


def tp_decision(
    *,
    direction: str,
    trade_class: str,
    entry: float,
    stop: float,
    atr: float,
    tick_size: float,
    current_tp: float,
    obstacles: tuple[Obstacle, ...] = (),
    use_v2: bool = False,
    live: bool = False,
) -> dict:
    risk = abs(entry - stop)
    baseline_r = 0.75 if trade_class == "CHILL" else 1.25
    baseline_raw = entry + baseline_r * risk if direction == "LONG" else entry - baseline_r * risk
    v2_tp = align(baseline_raw, tick_size, up=direction == "LONG")
    selected_obstacle = None
    if trade_class == "GOD":
        valid = [
            obstacle
            for obstacle in obstacles
            if obstacle.confidence in {"MEDIUM", "HIGH"}
            and (
                entry < obstacle.price <= v2_tp
                if direction == "LONG"
                else v2_tp <= obstacle.price < entry
            )
        ]
        if valid:
            selected_obstacle = min(valid, key=lambda obstacle: abs(obstacle.price - entry))
            raw = (
                selected_obstacle.price - 0.20 * atr
                if direction == "LONG"
                else selected_obstacle.price + 0.20 * atr
            )
            structural = align(raw, tick_size, up=direction == "SHORT")
            if 0 < abs(structural - entry) < abs(v2_tp - entry):
                v2_tp = structural
    apply_v2 = use_v2 and not live
    selected = v2_tp if apply_v2 else current_tp
    return {
        "current_tp": current_tp,
        "current_rr": abs(current_tp - entry) / risk,
        "v2_tp": v2_tp,
        "v2_rr": abs(v2_tp - entry) / risk,
        "selected_tp": selected,
        "selected_mode": "V2" if apply_v2 else "CURRENT",
        "obstacle": selected_obstacle,
    }


def tp_telemetry(evaluated: bool, **values) -> dict:
    """Projection used by EA/dashboard/observer at a decision boundary."""
    fields = ("current_tp", "current_rr", "v2_tp", "v2_rr", "selected_tp", "selected_rr")
    if not evaluated:
        return {
            "evaluated": False,
            "mode": "NOT_EVALUATED",
            "selection_reason": "NOT_EVALUATED",
            **{field: None for field in fields},
        }
    return {
        "evaluated": True,
        "mode": values.get("mode", "CURRENT"),
        "selection_reason": values.get("selection_reason", "EVALUATED"),
        **{field: values.get(field) for field in fields},
    }


class TakeProfitV2PolicyTests(unittest.TestCase):
    def test_consecutive_decision_does_not_reuse_previous_tp(self):
        first = tp_telemetry(
            True,
            mode="V2",
            current_tp=102.5,
            current_rr=1.25,
            v2_tp=101.5,
            v2_rr=0.75,
            selected_tp=101.5,
            selected_rr=0.75,
        )
        second = tp_telemetry(False, **{key: value for key, value in first.items() if key != "evaluated"})
        self.assertEqual(first["selected_tp"], 101.5)
        self.assertFalse(second["evaluated"])
        self.assertEqual(second["mode"], "NOT_EVALUATED")
        self.assertTrue(all(second[field] is None for field in (
            "current_tp", "current_rr", "v2_tp", "v2_rr", "selected_tp", "selected_rr"
        )))

    def test_chill_long_is_fixed_075r(self):
        result = tp_decision(
            direction="LONG", trade_class="CHILL", entry=100, stop=98,
            atr=1, tick_size=0.01, current_tp=102.5, use_v2=True,
        )
        self.assertAlmostEqual(result["v2_tp"], 101.5)
        self.assertAlmostEqual(result["v2_rr"], 0.75)

    def test_chill_short_is_fixed_075r(self):
        result = tp_decision(
            direction="SHORT", trade_class="CHILL", entry=100, stop=102,
            atr=1, tick_size=0.01, current_tp=97.5, use_v2=True,
        )
        self.assertAlmostEqual(result["v2_tp"], 98.5)
        self.assertAlmostEqual(result["v2_rr"], 0.75)

    def test_god_without_obstacle_is_125r(self):
        result = tp_decision(
            direction="LONG", trade_class="GOD", entry=100, stop=98,
            atr=1, tick_size=0.01, current_tp=104, use_v2=True,
        )
        self.assertAlmostEqual(result["v2_tp"], 102.5)
        self.assertAlmostEqual(result["v2_rr"], 1.25)

    def test_god_short_uses_support_plus_020_atr(self):
        result = tp_decision(
            direction="SHORT", trade_class="GOD", entry=100, stop=102,
            atr=1, tick_size=0.01, current_tp=96,
            obstacles=(Obstacle(98.9, "HIGH"),), use_v2=True,
        )
        self.assertAlmostEqual(result["v2_tp"], 99.1)
        self.assertAlmostEqual(result["v2_rr"], 0.45)

    def test_god_long_uses_resistance_minus_020_atr(self):
        result = tp_decision(
            direction="LONG", trade_class="GOD", entry=100, stop=98,
            atr=1, tick_size=0.01, current_tp=104,
            obstacles=(Obstacle(101.2, "MEDIUM"),), use_v2=True,
        )
        self.assertAlmostEqual(result["v2_tp"], 101.0)
        self.assertAlmostEqual(result["v2_rr"], 0.5)

    def test_high_and_medium_cut_but_low_does_not(self):
        for confidence in ("HIGH", "MEDIUM"):
            with self.subTest(confidence=confidence):
                result = tp_decision(
                    direction="LONG", trade_class="GOD", entry=100, stop=98,
                    atr=1, tick_size=0.01, current_tp=104,
                    obstacles=(Obstacle(101.2, confidence),), use_v2=True,
                )
                self.assertAlmostEqual(result["v2_tp"], 101.0)
        low = tp_decision(
            direction="LONG", trade_class="GOD", entry=100, stop=98,
            atr=1, tick_size=0.01, current_tp=104,
            obstacles=(Obstacle(101.2, "LOW"),), use_v2=True,
        )
        self.assertAlmostEqual(low["v2_tp"], 102.5)

    def test_low_reward_is_kept_in_paper_v2(self):
        result = tp_decision(
            direction="LONG", trade_class="GOD", entry=100, stop=98,
            atr=1, tick_size=0.01, current_tp=104,
            obstacles=(Obstacle(100.7, "HIGH"),), use_v2=True,
        )
        self.assertAlmostEqual(result["v2_rr"], 0.25)
        self.assertEqual(result["selected_mode"], "V2")

    def test_targets_are_aligned_to_tick_grid(self):
        result = tp_decision(
            direction="LONG", trade_class="GOD", entry=100.01, stop=98.01,
            atr=1.03, tick_size=0.05, current_tp=104.05,
            obstacles=(Obstacle(101.23, "HIGH"),), use_v2=True,
        )
        self.assertAlmostEqual(result["v2_tp"] / 0.05, round(result["v2_tp"] / 0.05))
        self.assertLessEqual(result["v2_tp"], 101.23)

    def test_toggle_false_selects_current_exactly(self):
        result = tp_decision(
            direction="LONG", trade_class="CHILL", entry=100, stop=98,
            atr=1, tick_size=0.01, current_tp=102.57, use_v2=False,
        )
        self.assertEqual(result["selected_tp"], 102.57)
        self.assertEqual(result["selected_mode"], "CURRENT")

    def test_v2_is_never_selected_live(self):
        result = tp_decision(
            direction="LONG", trade_class="CHILL", entry=100, stop=98,
            atr=1, tick_size=0.01, current_tp=102.5, use_v2=True, live=True,
        )
        self.assertEqual(result["selected_tp"], 102.5)
        self.assertEqual(result["selected_mode"], "CURRENT")


class TakeProfitV2SourceContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.policy = POLICY_PATH.read_text(encoding="utf-8")
        cls.observer = OBSERVER_PATH.read_text(encoding="utf-8")
        cls.dashboard = DASHBOARD_PATH.read_text(encoding="utf-8")
        cls.server = SERVER_PATH.read_text(encoding="utf-8")

    def test_safe_defaults_and_risk_inputs_are_intact(self):
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input bool   UseTakeProfitV2         = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)
        self.assertIn("input double DailyLossLimitPercent   = 5.0;", self.ea)
        self.assertIn("input int    MinScoreToTrade         = 74;", self.ea)
        self.assertIn("input int    ArmScoreThreshold       = 58;", self.ea)

    def test_policy_is_paper_only_and_current_is_always_shadowed(self):
        self.assertIn("return requested && !liveTrading;", self.policy)
        self.assertIn("decision.currentTP=currentTP;", self.policy)
        self.assertIn('decision.selectedMode=applyV2?"V2":"CURRENT";', self.policy)
        self.assertIn("GSTP_PaperV2Enabled(UseTakeProfitV2,EnableLiveTrading)", self.ea)

    def test_closed_bar_structure_uses_h1_h4_m15_without_m30(self):
        self.assertIn("PERIOD_H1,hATR,30", self.ea)
        self.assertIn("PERIOD_H4,hTPH4ATR,12", self.ea)
        self.assertIn("PERIOD_M15,hM15ATR,40", self.ea)
        self.assertIn("iHighest(_Symbol,PERIOD_H1,MODE_HIGH,10,2)", self.ea)
        self.assertNotIn("PERIOD_M30", self.policy)

    def test_v2_does_not_mutate_score_stop_volume_or_risk(self):
        for forbidden in (
            "longScore+=",
            "shortScore+=",
            "RiskPercent=",
            "DailyLossLimitPercent=",
            "BuildDynamicStop(",
            "PositionSizeForRisk(",
            "PositionOpen(",
        ):
            self.assertNotIn(forbidden, self.policy)

    def test_observer_contract_stays_diagnostic_only(self):
        for field in (
            '\\"current_tp\\"',
            '\\"current_rr\\"',
            '\\"v2_tp\\"',
            '\\"v2_rr\\"',
            '\\"selected_tp\\"',
            '\\"selected_rr\\"',
            '\\"tp_mode\\"',
            '\\"tp_structure_level\\"',
            '\\"tp_structure_confidence\\"',
        ):
            self.assertIn(field, self.observer)
        self.assertIn('\\"observer_only\\":true,\\"score_effect\\":0', self.observer)

    def test_dashboard_exposes_current_v2_and_selected_values(self):
        self.assertIn("TP MODE — PAPER A/B", self.dashboard)
        for identifier in (
            'id="tpMode"',
            'id="tpCurrent"',
            'id="tpCurrentRR"',
            'id="tpV2"',
            'id="tpV2RR"',
            'id="tpSelected"',
            'id="tpSelectedRR"',
            'id="tpLevel"',
            'id="tpConfidence"',
            'id="tpReason"',
        ):
            self.assertIn(identifier, self.dashboard)
        self.assertIn('"take_profit":{"evaluated":False,"mode":"NOT_EVALUATED"', self.server)

    def test_ea_resets_tp_before_each_decision_and_marks_evaluated_only_after_build(self):
        try_trade = self.ea[self.ea.index("void TryTrade()") : self.ea.index("string ExtractTag")]
        self.assertLess(try_trade.index("ResetTakeProfitDiagnostics();"), try_trade.index("BuildSignal("))
        reset = self.ea[self.ea.index("void ResetTakeProfitDiagnostics()") : self.ea.index("void StoreTakeProfitDiagnostics")]
        self.assertIn("g_tpEvaluated=false;", reset)
        self.assertIn('g_tpMode="NOT_EVALUATED";', reset)
        store = self.ea[self.ea.index("void StoreTakeProfitDiagnostics") : self.ea.index("void BuildMarketObserverContext")]
        self.assertIn("g_tpEvaluated=true;", store)

    def test_not_evaluated_tp_is_serialized_as_null(self):
        self.assertIn('"take_profit":{"evaluated":False,"mode":"NOT_EVALUATED"', self.server)
        self.assertIn("JsonNumberOrNull(g_tpEvaluated,g_tpCurrent)", self.ea)
        self.assertIn("GSMO_OptionalNumber(context.tpEvaluated,context.currentTP)", self.observer)
        self.assertIn('context.tpMode="NOT_EVALUATED";', (ROOT / "tests" / "mql" / "MarketObserverCompileHarness.mq5").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
