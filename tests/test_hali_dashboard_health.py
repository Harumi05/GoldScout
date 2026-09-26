"""Health projection regression tests for the Hali dashboard."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboard"
sys.path.insert(0, str(DASHBOARD))

import server  # noqa: E402


class HaliDashboardHealthTests(unittest.TestCase):
    def setUp(self):
        self.original_candidates = server.CANDIDATES

    def tearDown(self):
        server.CANDIDATES = self.original_candidates

    def test_missing_artifact_is_reported_without_path_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            server.CANDIDATES = [Path(temp)]
            result = server.file_health("xau_goldscout_dashboard.json")
        self.assertEqual(
            result,
            {"exists": False, "age_seconds": None, "size_bytes": None},
        )
        self.assertNotIn("path", result)

    def test_fresh_demo_snapshot_reports_online_components(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server.CANDIDATES = [root]
            (root / "xau_goldscout_dashboard.json").write_text(
                json.dumps(
                    {
                        "demo_execution": {
                            "broker_connected": True,
                            "execution_allowed": True,
                            "account_mode": "DEMO",
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "gold_news_analysis.json").write_text(
                json.dumps({"stale_after_seconds": 900}),
                encoding="utf-8",
            )
            (root / "market_observations.jsonl").write_text("{}\n", encoding="utf-8")
            health = server.system_health_snapshot()

        self.assertEqual(health["backend"]["status"], "ONLINE")
        self.assertEqual(health["ea_feed"]["status"], "ONLINE")
        self.assertEqual(health["broker"]["status"], "CONNECTED")
        self.assertEqual(health["demo_execution"]["status"], "READY")
        self.assertEqual(health["news_engine"]["status"], "ONLINE")
        self.assertEqual(health["market_observer"]["status"], "AVAILABLE")

    def test_stale_dashboard_snapshot_is_not_reported_online(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            server.CANDIDATES = [root]
            dashboard = root / "xau_goldscout_dashboard.json"
            dashboard.write_text(
                json.dumps(
                    {
                        "demo_execution": {
                            "broker_connected": False,
                            "execution_allowed": False,
                            "account_mode": "DEMO",
                        }
                    }
                ),
                encoding="utf-8",
            )
            old = time.time() - 120
            os.utime(dashboard, (old, old))
            health = server.system_health_snapshot()

        self.assertEqual(health["ea_feed"]["status"], "STALE")
        self.assertEqual(health["broker"]["status"], "DISCONNECTED")
        self.assertEqual(health["demo_execution"]["status"], "BLOCKED")


class HaliHealthUiSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (DASHBOARD / "index.html").read_text(encoding="utf-8")
        cls.server_source = (DASHBOARD / "server.py").read_text(encoding="utf-8")

    def test_system_health_ui_and_endpoint_are_present(self):
        self.assertIn('id="health"', self.index)
        self.assertIn("ESTADO DEL SISTEMA", self.index)
        self.assertIn("loadSystemHealth", self.index)
        self.assertIn("system_health_snapshot", self.server_source)

    def test_health_ui_does_not_change_trading_policy(self):
        for token in (
            "RiskPercent=",
            "MinScoreToTrade=",
            "DailyLossLimitPercent=",
            "EnableLiveTrading=true",
        ):
            self.assertNotIn(token, self.index)


if __name__ == "__main__":
    unittest.main()
