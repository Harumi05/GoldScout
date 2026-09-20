"""Deterministic checks for the recovered runtime sizing diagnostic."""

from __future__ import annotations

from math import floor
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"


def target_risk(equity: float, risk_percent: float) -> float:
    return equity * min(max(risk_percent, 0.0), 5.0) / 100.0


def planned_risk(target: float, remaining_daily_budget: float) -> float:
    return max(0.0, min(target, max(remaining_daily_budget, 0.0)))


def daily_budget(start_of_day_equity: float, limit_percent: float) -> float:
    return max(0.0, start_of_day_equity * limit_percent / 100.0)


def risk_at_sl(distance: float, volume: float, contract_size: float = 100.0) -> float:
    return abs(distance) * volume * contract_size


def size_down(
    risk_budget: float,
    distance: float,
    volume_min: float = 0.01,
    volume_step: float = 0.01,
) -> tuple[float, float]:
    per_lot = risk_at_sl(distance, 1.0)
    raw = risk_budget / per_lot if per_lot > 0.0 else 0.0
    if raw < volume_min - 1e-9:
        return raw, 0.0
    steps = floor((raw - volume_min) / volume_step + 1e-9)
    return raw, round(volume_min + steps * volume_step, 8)


class SizingDiagnosticRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = EA.read_text(encoding="utf-8")

    def test_target_and_daily_budget_examples(self):
        self.assertEqual(target_risk(1_000.0, 5.0), 50.0)
        self.assertEqual(daily_budget(1_000.0, 5.0), 50.0)
        self.assertEqual(daily_budget(200.0, 5.0), 10.0)
        self.assertEqual(daily_budget(500.0, 5.0), 25.0)
        self.assertEqual(planned_risk(50.0, 12.5), 12.5)
        self.assertIn("double plannedRisk=MathMax(0.0,MathMin(targetRisk,remainingDailyBudget))", self.source)
        self.assertRegex(self.source, r"input\s+double\s+DailyLossLimitPercent\s*=\s*5\.0\s*;")
        self.assertNotIn("DailyLossLimitUSD", self.source)

    def test_minimum_lot_can_block_small_remaining_budget(self):
        raw, rounded = size_down(4.0, 60.0)
        self.assertAlmostEqual(risk_at_sl(60.0, 0.01), 60.0)
        self.assertLess(raw, 0.01)
        self.assertEqual(rounded, 0.0)

    def test_valid_volume_rounds_down(self):
        raw, rounded = size_down(50.0, 40.0)
        self.assertAlmostEqual(raw, 0.0125)
        self.assertEqual(rounded, 0.01)
        self.assertLessEqual(risk_at_sl(40.0, rounded), 50.0)

    def test_no_twenty_dollar_legacy_fallback(self):
        sizing_region = self.source[
            self.source.index("double PlannedRiskAmount()") : self.source.index("bool MarginAllowsOrder")
        ]
        self.assertNotRegex(sizing_region, r"(?:riskAmount|plannedRisk|targetRisk)\s*=\s*20(?:\.0+)?\s*;")
        self.assertNotIn("MathMin(PlannedRiskAmount(),20", sizing_region)

    def test_runtime_log_has_requested_contract_and_risk_fields(self):
        line = next(line for line in self.source.splitlines() if "[GoldScout][SIZING] targetRisk=" in line)
        for field in (
            "targetRisk=", "remainingDailyBudget=", "plannedRisk=", "entry=", "sizingEntry=", "sl=",
            "distance=", "volumeMin=", "volumeMax=", "volumeStep=", "tickSize=", "tickValue=",
            "tickValueProfit=", "tickValueLoss=", "contractSize=", "riskAtMinVolume=", "riskAt0.01=",
            "rawVolume=", "riskAtRaw=", "roundedVolume=", "finalRisk=", "blockReason=",
        ):
            self.assertIn(field, line)

    def test_diagnostic_is_rate_limited_and_does_not_replace_sizing(self):
        self.assertIn("now-g_lastSizingDiagnosticLogTime<30", self.source)
        sizing_call = self.source.index("if(!PositionSizeForRisk(type,worstCasePrice,sl,plannedRisk,contract,lots))")
        failure_log = self.source.index("LogSizingDiagnostic(type,price,worstCasePrice,sl,targetRisk", sizing_call)
        self.assertGreater(failure_log, sizing_call)
        self.assertIn("actualRisk>plannedRisk+1e-6", self.source)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.source)
        self.assertIn("input double RiskPercent             = 5.0;", self.source)


if __name__ == "__main__":
    unittest.main()
