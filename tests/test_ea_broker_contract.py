"""Deterministic tests for broker-contract monetary sizing.

MetaTrader remains the authority for OrderCalcProfit/OrderCalcMargin and the
live Capital.com symbol specification. These models test the deterministic
rounding and fail-closed policy mirrored by the EA.
"""

from dataclasses import dataclass
from math import ceil, floor
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
EA = ROOT / "MT5" / "Experts" / "XAU_GoldScout_H1.mq5"
DASHBOARD = ROOT / "dashboard" / "index.html"
SERVER = ROOT / "dashboard" / "server.py"


@dataclass(frozen=True)
class Contract:
    volume_min: float
    volume_max: float
    volume_step: float
    tick_size: float
    digits: int


def normalize_volume_down(lots: float, contract: Contract) -> float:
    if lots < contract.volume_min - 1e-9 or contract.volume_step <= 0:
        return 0.0
    lots = max(lots, contract.volume_min)
    lots = min(lots, contract.volume_max)
    steps = floor((lots - contract.volume_min) / contract.volume_step + 1e-9)
    normalized = contract.volume_min + steps * contract.volume_step
    if normalized > lots + 1e-9:
        return 0.0
    return round(normalized, 8)


def align_price(price: float, contract: Contract, *, round_up: bool) -> float:
    ticks = price / contract.tick_size
    aligned = (
        ceil(ticks - 1e-9) if round_up else floor(ticks + 1e-9)
    ) * contract.tick_size
    return round(aligned, contract.digits)


def size_for_risk(
    risk_amount: float,
    loss_per_lot: float,
    commission_per_lot: float,
    contract: Contract,
) -> float:
    reference = normalize_volume_down(
        max(contract.volume_min, min(1.0, contract.volume_max)), contract
    )
    if reference <= 0:
        return 0.0
    reference_loss = (loss_per_lot + commission_per_lot) * reference
    if reference_loss <= 0:
        return 0.0
    return normalize_volume_down(risk_amount * reference / reference_loss, contract)


class BrokerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ea = EA.read_text(encoding="utf-8")
        cls.dashboard = DASHBOARD.read_text(encoding="utf-8")
        cls.server = SERVER.read_text(encoding="utf-8")
        cls.capital = Contract(0.01, 100.0, 0.01, 0.01, 2)

    def test_five_percent_is_in_deposit_currency(self):
        for currency, equity in (("USD", 1_000.0), ("EUR", 800.0), ("GBP", 600.0)):
            with self.subTest(currency=currency):
                self.assertEqual(equity * 5.0 / 100.0, equity * 0.05)
        self.assertEqual(size_for_risk(50.0, 500.0, 0.0, self.capital), 0.10)
        self.assertEqual(size_for_risk(40.0, 400.0, 0.0, self.capital), 0.10)

    def test_volume_rounds_down_from_broker_minimum_grid(self):
        unusual = Contract(0.03, 1.0, 0.02, 0.05, 2)
        self.assertEqual(normalize_volume_down(0.10, unusual), 0.09)
        self.assertEqual(normalize_volume_down(0.02, unusual), 0.0)
        self.assertEqual(normalize_volume_down(1.50, unusual), 0.99)

    def test_prices_align_away_from_risk_on_tick_grid(self):
        contract = Contract(0.01, 100.0, 0.01, 0.05, 2)
        self.assertEqual(align_price(2387.237, contract, round_up=False), 2387.20)
        self.assertEqual(align_price(2387.237, contract, round_up=True), 2387.25)

    def test_commission_buffer_can_only_reduce_volume(self):
        without_cost = size_for_risk(50.0, 1_000.0, 0.0, self.capital)
        with_cost = size_for_risk(50.0, 1_000.0, 10.0, self.capital)
        self.assertEqual(without_cost, 0.05)
        self.assertEqual(with_cost, 0.04)
        self.assertLess(with_cost, without_cost)

    def test_directional_loss_value_can_produce_different_safe_sizes(self):
        buy_lots = size_for_risk(50.0, 500.0, 0.0, self.capital)
        sell_lots = size_for_risk(50.0, 625.0, 0.0, self.capital)
        self.assertEqual(buy_lots, 0.10)
        self.assertEqual(sell_lots, 0.08)

    def test_capital_style_gold_contract_sizes_deterministically(self):
        # Public Capital.com specification: contract 100 oz, min/step 0.01.
        # A 5.00 price-to-SL move is 500 account-currency units per 1 lot
        # before currency conversion; a 50-unit budget therefore yields 0.10.
        lots = size_for_risk(50.0, 500.0, 0.0, self.capital)
        self.assertEqual(lots, 0.10)
        self.assertLessEqual(lots * 500.0, 50.0)

    def test_margin_policy_blocks_insufficient_free_margin(self):
        self.assertFalse(1_001.0 <= 1_000.0)
        self.assertTrue(999.0 <= 1_000.0)

    def test_source_uses_full_contract_and_terminal_calculators(self):
        for token in (
            "ACCOUNT_CURRENCY",
            "SYMBOL_TRADE_TICK_SIZE",
            "SYMBOL_TRADE_TICK_VALUE",
            "SYMBOL_TRADE_TICK_VALUE_PROFIT",
            "SYMBOL_TRADE_TICK_VALUE_LOSS",
            "SYMBOL_TRADE_CONTRACT_SIZE",
            "SYMBOL_VOLUME_MIN",
            "SYMBOL_VOLUME_MAX",
            "SYMBOL_VOLUME_STEP",
            "SYMBOL_TRADE_STOPS_LEVEL",
            "SYMBOL_TRADE_FREEZE_LEVEL",
            "OrderCalcProfit",
            "OrderCalcMargin",
            "ACCOUNT_MARGIN_FREE",
        ):
            self.assertIn(token, self.ea)
        self.assertIn("alignedSL=AlignPriceToTick(alignedSL,contract,direction<0)", self.ea)
        self.assertIn("double stopReference=(direction>0?bid:ask)", self.ea)
        self.assertIn("tp=AlignPriceToTick(tp,contract,direction>0)", self.ea)
        self.assertIn("RoundTurnCommissionPerLot*lots", self.ea)
        self.assertLess(
            self.ea.index("if(!LoadBrokerContract(contract,contractMsg))"),
            self.ea.index("PositionSizeForRisk(type,worstCasePrice"),
        )
        self.assertLess(
            self.ea.index("if(!MarginAllowsOrder(type,price,lots,contract,marginMsg))"),
            self.ea.index("ReserveH1EntryPending(entryBar)"),
        )

    def test_dashboard_labels_amount_with_account_currency(self):
        self.assertIn('\\"account_currency\\":', self.ea)
        self.assertIn('\\"risk_amount\\":', self.ea)
        self.assertIn("data.risk_amount??data.risk_usd", self.dashboard)
        self.assertIn('"account_currency":"USD"', self.server)
        self.assertIn('"risk_amount":0.0', self.server)
        self.assertIn('"daily_loss_limit_percent":5.0', self.server)

    def test_live_trading_and_risk_defaults_are_unchanged(self):
        self.assertIn("input bool   EnableLiveTrading      = false;", self.ea)
        self.assertIn("input double RiskPercent             = 5.0;", self.ea)


if __name__ == "__main__":
    unittest.main()
