"""Contract tests for the Observer-only vol-target telemetry (Quant Research V1)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OBSERVER = (ROOT / "MT5" / "Include" / "GoldScout" / "MarketObserver.mqh").read_text(encoding="utf-8")
EA = (ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5").read_text(encoding="utf-8")


def _body(source: str, signature: str) -> str:
    start = source.index(signature)
    depth, i = 0, source.index("{", start)
    for j in range(i, len(source)):
        depth += {"{": 1, "}": -1}.get(source[j], 0)
        if depth == 0:
            return source[i:j + 1]
    raise AssertionError("unbalanced body")


class VolTargetTelemetryContract(unittest.TestCase):
    def test_record_declares_no_score_or_sizing_effect(self) -> None:
        body = _body(OBSERVER, "string VolTargetTelemetryJson(")
        self.assertIn('\\"score_effect\\":0', body)
        self.assertIn('\\"sizing_effect\\":0', body)
        self.assertIn('\\"observer_only\\":true', body)
        self.assertIn("if(m_timeframes[index]!=PERIOD_H1) return \"null\";", body)

    def test_multiplier_is_down_only_and_unknown_is_null(self) -> None:
        body = _body(OBSERVER, "string VolTargetTelemetryJson(")
        self.assertIn("MathMin(1.0,MathMax(0.25,rv120/rv24))", body)
        self.assertIn("GSMO_OptionalNumber(hasMultiplier,multiplier,4)", body)

    def test_uses_closed_bars_only(self) -> None:
        body = _body(OBSERVER, "string VolTargetTelemetryJson(")
        self.assertIn("int firstShift=MathMax(1,shift);", body)

    def test_capture_emits_field(self) -> None:
        body = _body(OBSERVER, "bool Capture(")
        self.assertIn('\\"vol_target_telemetry\\":"+VolTargetTelemetryJson(index,shift)', body)

    def test_ea_never_reads_telemetry(self) -> None:
        for token in ("VolTargetTelemetry", "down_only_multiplier", "RealizedVol("):
            self.assertNotIn(token, EA)
        self.assertIsNone(re.search(r"RiskPercent\s*\*\s*\w*[Mm]ultiplier", EA))


if __name__ == "__main__":
    unittest.main()
