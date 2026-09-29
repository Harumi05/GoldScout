# GoldScout — Current Task

## Status
IMPLEMENTED_PENDING_HUMAN_REVIEW — Risk Budget V2 Phase 1.
Acceptance/merge is not implied by this status.

## Owner / Reviewer
Implementer: Codex. Reviewer: Human / ChatGPT.

## Objective
Expose the CURRENT risk decomposition and precise block diagnostics in one
read-only snapshot, dashboard and Market Observer. Characterize existing
acceptance behavior before any risk-policy change.

## Implemented contract
- Daily budget uses persisted start-of-day equity, not current profitable equity.
- Remaining = max(0, daily budget - persistent negative net deal losses - account open risk).
- Planned = max(0, min(current-equity target risk, remaining)).
- Open risk covers all account positions; a position without a valid SL fails closed.
- Pending monetary risk is NOT_SUPPORTED; amount is null, not a fabricated zero.
- Account/server H1 reservation remains NONE / PENDING / CONFIRMED, independent of MagicNumber.
- Unknown snapshot amounts are null; risk health is UNKNOWN, not HEALTHY.
- Observer-only telemetry has score_effect=0 and does not grant authorization.
- Exact source defaults are recorded in CONTEXT.md, not asserted as VPS inputs.

## Protected behavior
No change to risk percentages, daily-budget/loss/open-risk semantics, drawdown,
H1 ownership/reservation, position gating, sizing, SL/Adaptive Stop, TP V2/CURRENT
shadow, scoring/thresholds, M15, patterns, sessions/news, ledger or order authorization.
REAL remains hard-blocked; EnableLiveTrading=false. No new strategy or capital reservation.
UseArmedInvalidationV1=false remains the source default; not promoted here.

## Required validation
Risk/safety characterization (before/after allow-block equivalence), dashboard
and Observer contracts, full Python suite, main EA and Market Observer harness
compilation, and git diff --check. Report actual results and limitations in the PR.
Compilation does not prove deployed behavior; request an actual VPS/MT5 snapshot.

## Workflow / next task
Dedicated branch codex/risk-budget-v2-phase1-observability; PR against main,
human review required, NO automatic merge. Preserve the two untracked TP V2.1 files.
After approval: define Risk Budget V2 Phase 2 policy/atomic pending-risk ownership
with explicit approval and tests; do not implement it in Phase 1.
