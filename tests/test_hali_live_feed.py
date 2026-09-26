"""Read-only live chart feed contracts for Hali."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA_PATH = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
FEED_PATH = ROOT / "MT5" / "Indicators" / "HaliLiveFeed.mq5"
SERVER_PATH = ROOT / "dashboard" / "server.py"
INDEX_PATH = ROOT / "dashboard" / "index.html"


class HaliLiveFeedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA_PATH.read_text(encoding="utf-8")
        cls.feed = FEED_PATH.read_text(encoding="utf-8")
        cls.server = SERVER_PATH.read_text(encoding="utf-8")
        cls.index = INDEX_PATH.read_text(encoding="utf-8")

    def test_live_feed_is_separate_from_trading_ea(self):
        self.assertIn("HaliLiveFeed", self.feed)
        self.assertNotIn("LiveMarketJson()", self.ea)
        on_tick = self.ea.split("void OnTick()", 1)[1].split("}", 1)[0]
        self.assertNotIn("UpdateDashboard", on_tick)
        self.assertNotIn("FileWriteString", on_tick)

    def test_feed_has_no_trading_or_scoring_actions(self):
        forbidden = (
            "OrderSend(",
            "trade.Buy",
            "trade.Sell",
            "PositionOpen(",
            "MinScoreToTrade",
            "RiskPercent",
            "DailyLossLimitPercent",
            "EnableLiveTrading",
        )
        for token in forbidden:
            self.assertNotIn(token, self.feed)

    def test_feed_exports_current_m15_h1_h4_and_tick_time(self):
        for token in (
            'BarJson(PERIOD_M15,"M15")',
            'BarJson(PERIOD_H1,"H1")',
            'BarJson(PERIOD_H4,"H4")',
            '\"tick_epoch\"',
            '\"tick_time_msc\"',
            "EventSetMillisecondTimer",
            "hali_live_market.json",
        ):
            self.assertIn(token, self.feed)

    def test_dashboard_prefers_live_feed_file(self):
        self.assertIn("read_json('hali_live_market.json')", self.server)
        self.assertIn("'live_bar':live_bar", self.server)
        self.assertIn("'tick_epoch':live_market.get('tick_epoch')", self.server)

    def test_browser_polls_live_chart_each_second(self):
        self.assertIn("setInterval(loadChart,1000)", self.index)
        self.assertIn("FEED ONLINE · SIN TICKS", self.index)
        self.assertIn("chart-live live", self.index)


if __name__ == "__main__":
    unittest.main()
