"""Deterministic safety-invariant tests for the GoldScout MQL5 EA.

The state model covers the agreed fail-closed transitions independently of MT5.
Source contracts ensure the EA retains the corresponding MQL guards. MetaEditor/
MT5 is still required to validate the terminal APIs, broker execution and fills.
"""

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
import unittest


EA = (
    Path(__file__).resolve().parents[1]
    / "MT5"
    / "Experts"
    / "XAU_GoldScout_H1.mq5"
)
HARD_MAX_RISK_PERCENT = 5.0


class SafetyStateUnavailable(RuntimeError):
    """The EA must reject new entries when required safety state is unavailable."""


@dataclass
class AccountSafetyState:
    day: int | None = None
    daily_loss_used: float = 0.0
    peak_equity: float = 0.0


def refresh_account_state(
    state: AccountSafetyState,
    *,
    server_day: int,
    equity: float,
    history_available: bool,
    account_profit: float,
) -> AccountSafetyState:
    """Reference model for the account-level fail-closed persistence rules."""
    if server_day <= 0 or equity <= 0 or not history_available:
        raise SafetyStateUnavailable
    if state.day != server_day:
        state.day = server_day
        state.daily_loss_used = 0.0
    state.daily_loss_used = max(
        state.daily_loss_used,
        max(0.0, -account_profit),
    )
    state.peak_equity = max(state.peak_equity, equity)
    return state


def remaining_daily_budget(limit: float, used: float) -> float:
    return max(0.0, limit - used)


def inputs_are_safe(risk_percent: float) -> bool:
    return 0.0 <= risk_percent <= HARD_MAX_RISK_PERCENT


class ReservationPhase(IntEnum):
    NONE = 0
    PENDING = 1
    CONFIRMED = 2


@dataclass
class H1Reservation:
    bar: int = 0
    phase: ReservationPhase = ReservationPhase.NONE

    def reserve_pending(self, bar: int) -> bool:
        if bar <= 0 or (self.bar == bar and self.phase != ReservationPhase.NONE):
            return False
        self.bar = bar
        self.phase = ReservationPhase.PENDING
        return True

    def confirm(self, bar: int) -> bool:
        if self.bar != bar or self.phase != ReservationPhase.PENDING:
            return False
        self.phase = ReservationPhase.CONFIRMED
        return True

    def release_after_safe_failure(self, bar: int) -> bool:
        if self.bar != bar or self.phase != ReservationPhase.PENDING:
            return False
        self.bar = 0
        self.phase = ReservationPhase.NONE
        return True

    def blocks(self, bar: int) -> bool:
        return self.bar == bar and self.phase != ReservationPhase.NONE


class SafetyInvariantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = EA.read_text(encoding="utf-8")

    def test_history_unavailable_is_fail_closed(self):
        with self.assertRaises(SafetyStateUnavailable):
            refresh_account_state(
                AccountSafetyState(),
                server_day=20260908,
                equity=1_000.0,
                history_available=False,
                account_profit=0.0,
            )
        self.assertIn("if(!TodayAccountProfit(accountProfit)) return false;", self.source)
        self.assertIn("if(!HistorySelect(start,now)) return false;", self.source)

    def test_history_without_losses_keeps_daily_budget_available(self):
        state = refresh_account_state(
            AccountSafetyState(),
            server_day=20260908,
            equity=1_000.0,
            history_available=True,
            account_profit=0.0,
        )
        self.assertEqual(state.daily_loss_used, 0.0)
        self.assertEqual(remaining_daily_budget(20.0, state.daily_loss_used), 20.0)

    def test_account_drawdown_uses_shared_account_peak(self):
        state = refresh_account_state(
            AccountSafetyState(),
            server_day=20260908,
            equity=1_000.0,
            history_available=True,
            account_profit=0.0,
        )
        refresh_account_state(
            state,
            server_day=20260908,
            equity=840.0,
            history_available=True,
            account_profit=0.0,
        )
        drawdown = 100.0 * (state.peak_equity - 840.0) / state.peak_equity
        self.assertGreaterEqual(drawdown, 15.0)
        self.assertIn(
            'return StringFormat("GSH2.A.%s.%s",AccountStateIdentity(),suffix);',
            self.source,
        )
        self.assertIn("AccountInfoString(ACCOUNT_SERVER)", self.source)

    def test_daily_budget_is_exhausted_by_account_loss(self):
        state = refresh_account_state(
            AccountSafetyState(),
            server_day=20260908,
            equity=980.0,
            history_available=True,
            account_profit=-20.0,
        )
        self.assertEqual(state.daily_loss_used, 20.0)
        self.assertEqual(remaining_daily_budget(20.0, state.daily_loss_used), 0.0)
        self.assertIn(
            "return MathMax(0.0,DailyLossLimitUSD-g_dailyLossUsed);", self.source
        )

    def test_h1_reservation_transitions_pending_to_confirmed(self):
        reservation = H1Reservation()
        self.assertTrue(reservation.reserve_pending(1_725_792_000))
        self.assertEqual(reservation.phase, ReservationPhase.PENDING)
        self.assertTrue(reservation.confirm(1_725_792_000))
        self.assertEqual(reservation.phase, ReservationPhase.CONFIRMED)
        self.assertTrue(reservation.blocks(1_725_792_000))
        self.assertIn("ENTRY_RESERVATION_PENDING=1", self.source)
        self.assertIn("ENTRY_RESERVATION_CONFIRMED=2", self.source)
        self.assertIn("ReserveH1EntryPending(entryBar)", self.source)
        self.assertIn("ConfirmH1Entry(entryBar)", self.source)

    def test_safely_rejected_order_releases_pending_reservation(self):
        reservation = H1Reservation()
        self.assertTrue(reservation.reserve_pending(1_725_792_000))
        self.assertTrue(reservation.release_after_safe_failure(1_725_792_000))
        self.assertFalse(reservation.blocks(1_725_792_000))
        self.assertIn("ExecutionFailureIsSafelyFinal", self.source)
        self.assertIn("ReleasePendingH1Entry(entryBar)", self.source)

    def test_execution_confirmation_requires_retcode_and_deal(self):
        self.assertIn("retcode=trade.ResultRetcode();", self.source)
        self.assertIn("deal=trade.ResultDeal();", self.source)
        self.assertIn("ExecutionResultConfirmed(retcode,deal)", self.source)
        self.assertIn("TRADE_RETCODE_DONE_PARTIAL", self.source)
        self.assertIn("&& deal>0", self.source)

    def test_restart_during_pending_reservation_blocks_the_same_h1(self):
        reservation = H1Reservation()
        self.assertTrue(reservation.reserve_pending(1_725_792_000))
        restarted = H1Reservation(reservation.bar, reservation.phase)
        self.assertTrue(restarted.blocks(1_725_792_000))
        self.assertIn("reserva H1 PENDING; posible orden previa sin confirmar", self.source)
        self.assertIn("g_entryUsedThisBar=!EntryReservationForBar", self.source)

    def test_requested_risk_over_five_percent_is_rejected(self):
        self.assertFalse(inputs_are_safe(5.01))
        self.assertTrue(inputs_are_safe(5.0))
        self.assertIn("const double HARD_MAX_RISK_PERCENT = 5.0;", self.source)
        self.assertIn(
            "RiskPercent<0.0 || RiskPercent>HARD_MAX_RISK_PERCENT", self.source
        )
        self.assertIn(
            "MathMax(0.0,MathMin(RiskPercent,HARD_MAX_RISK_PERCENT))", self.source
        )
        self.assertNotIn("MathAbs(RiskPercent)", self.source)
        self.assertIn("input bool   EnableLiveTrading      = false;", self.source)

    def test_worst_permitted_fill_is_used_before_submission(self):
        self.assertIn("MAX_EXECUTION_DEVIATION_POINTS = 30", self.source)
        self.assertIn("double worstCasePrice=WorstCaseFillPrice(type,price,contract);", self.source)
        self.assertIn("PositionSizeForRisk(type,worstCasePrice,sl,plannedRisk,contract,lots)", self.source)
        self.assertIn("RiskAtSL(type,worstCasePrice,sl,lots,actualRisk)", self.source)
        self.assertIn("trade.SetDeviationInPoints(MAX_EXECUTION_DEVIATION_POINTS);", self.source)

    def test_global_keys_are_short_and_state_order_is_safe(self):
        self.assertIn("MAX_TERMINAL_GLOBAL_NAME_LENGTH = 63", self.source)
        self.assertIn("StringLen(key)<=MAX_TERMINAL_GLOBAL_NAME_LENGTH", self.source)
        self.assertIn("InitializeEntryReservationState()", self.source)
        entry_key = self.source[
            self.source.index("string EntrySafetyStateKey") : self.source.index(
                "bool SetSafetyStateValue"
            )
        ]
        self.assertNotIn("MagicNumber", entry_key)
        self.assertNotIn("_Symbol", entry_key)
        self.assertLess(
            self.source.index("ReserveH1EntryPending(entryBar)"),
            self.source.index("trade.PositionOpen(_Symbol,type,lots,price,sl,tp,comment)"),
        )


if __name__ == "__main__":
    unittest.main()
