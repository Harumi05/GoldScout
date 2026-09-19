from dataclasses import dataclass
from pathlib import Path
import unittest


MAGIC = 8_202_609
SYMBOL = "XAUUSD"
EA_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "MT5"
    / "Experts"
    / "XAU_GoldScout_H1.mq5"
)


@dataclass(frozen=True)
class Exposure:
    symbol: str
    magic: int
    volume: float = 1.0


def position_state_allows_entry(mode, one_position_at_a_time, positions, orders):
    """Deterministic model of the EA's ownership policy."""
    if mode not in {"NETTING", "EXCHANGE", "HEDGING"}:
        return False
    if mode == "HEDGING" and not one_position_at_a_time:
        return True

    require_our_magic = mode == "HEDGING"
    for exposure in (*positions, *orders):
        if exposure.symbol != SYMBOL:
            continue
        if require_our_magic and exposure.magic != MAGIC:
            continue
        return False
    return True


class PositionManagementPolicyTests(unittest.TestCase):
    def test_netting_blocks_manual_position_even_if_option_is_disabled(self):
        self.assertFalse(
            position_state_allows_entry(
                "NETTING", False, [Exposure(SYMBOL, 0)], []
            )
        )

    def test_netting_blocks_other_ea_position(self):
        self.assertFalse(
            position_state_allows_entry(
                "NETTING", True, [Exposure(SYMBOL, MAGIC + 1)], []
            )
        )

    def test_netting_blocks_any_active_order_for_symbol(self):
        self.assertFalse(
            position_state_allows_entry(
                "NETTING", True, [], [Exposure(SYMBOL, 0)]
            )
        )

    def test_other_symbol_never_blocks(self):
        self.assertTrue(
            position_state_allows_entry(
                "NETTING",
                True,
                [Exposure("EURUSD", MAGIC)],
                [Exposure("USDJPY", 0)],
            )
        )

    def test_hedging_ignores_manual_and_other_ea_positions(self):
        self.assertTrue(
            position_state_allows_entry(
                "HEDGING",
                True,
                [Exposure(SYMBOL, 0), Exposure(SYMBOL, MAGIC + 1)],
                [],
            )
        )

    def test_hedging_finds_our_position_among_multiple_positions(self):
        self.assertFalse(
            position_state_allows_entry(
                "HEDGING",
                True,
                [Exposure(SYMBOL, 0), Exposure(SYMBOL, MAGIC)],
                [],
            )
        )

    def test_hedging_partial_close_remains_blocking_until_position_disappears(self):
        self.assertFalse(
            position_state_allows_entry(
                "HEDGING", True, [Exposure(SYMBOL, MAGIC, volume=0.01)], []
            )
        )

    def test_hedging_blocks_our_active_order(self):
        self.assertFalse(
            position_state_allows_entry(
                "HEDGING", True, [], [Exposure(SYMBOL, MAGIC)]
            )
        )

    def test_hedging_ignores_manual_and_other_ea_orders(self):
        self.assertTrue(
            position_state_allows_entry(
                "HEDGING",
                True,
                [],
                [Exposure(SYMBOL, 0), Exposure(SYMBOL, MAGIC + 1)],
            )
        )

    def test_hedging_allows_multiple_when_option_is_disabled(self):
        self.assertTrue(
            position_state_allows_entry(
                "HEDGING",
                False,
                [Exposure(SYMBOL, MAGIC)],
                [Exposure(SYMBOL, MAGIC)],
            )
        )

    def test_unknown_account_mode_fails_closed(self):
        self.assertFalse(position_state_allows_entry("UNKNOWN", True, [], []))

    def test_source_enumerates_positions_orders_and_rechecks_before_reserve(self):
        source = EA_SOURCE.read_text(encoding="utf-8")
        self.assertNotIn("PositionSelect(_Symbol)", source)
        self.assertIn("PositionGetTicket(i)", source)
        self.assertIn("PositionSelectByTicket(ticket)", source)
        self.assertIn("OrderGetTicket(i)", source)
        self.assertIn("POSITION_MAGIC", source)
        self.assertIn("ORDER_MAGIC", source)
        self.assertIn("ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", source)
        guarded_call = "PositionStateAllowsEntry(positionBlock,DemoMultiplePositionsAllowed())"
        self.assertEqual(source.count(guarded_call), 2)
        self.assertLess(
            source.rfind(guarded_call),
            source.index("ReserveH1EntryPending(entryBar)"),
        )


if __name__ == "__main__":
    unittest.main()
