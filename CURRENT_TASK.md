# GoldScout — Current Task

## Status
Risk Budget V2 Phase 1: MERGED / ACCEPTED via PR #12.
Main merge: 968ad45d7b2b538e7164f14b9120b48bcf5b9e10.
Hali + main integration: MERGED / ACCEPTED via PR #13 on 2026-09-30.
Main merge: e539b05d22605dc1b4cd2cfb57fdc5858c31c000; both security fixes accepted
in the owner's final review of c50a448cf1449bf035e93f6f5669faeceda759ee.
Current task (owner request 2026-10-22): audit recent trades, coordinate evidence
review and implement justified improvements. Status: IN PROGRESS, not accepted.
Available local execution evidence ends 2026-09-23; snapshot ends 2026-09-28.
Hali inspected at https://goldscout.duckdns.org/: snapshot 2026-10-19 10:12:40,
MT5/Observer STALE ~3.7 days, 25 pre-ledger closes through Oct 15, 17 signals.
16 closes associated with signals by exact time/symbol/direction/score/volume.
Six associated SHORT losses had H1/H4 BAJISTA; three SHORT with H1 ALCISTA won.
Owner's rising-price hypothesis requires causal candles, not a blanket H1 filter.
Chart API only exposes 12 recent H1 bars, insufficient for those entries.
Owner additionally requests quantitative trading; implement research/validation
first, score_effect=0, without changing protected live/DEMO strategy behavior.
No VPS deployment has been performed or validated by these tasks.

## Owner / Reviewer
Coordinator / local validator: Codex. Reviewer: Human / independent reviewer pending.
Orca multiagent launch is blocked by automatic approval policy; no workers started.

## Objective
Inspect current Hali trade records and correlate fills/closes with their actual
entry-time H1/H4/M15 context. Separate confirmed losses, metadata gaps,
execution faults and strategy hypotheses. Preserve MT5 risk_budget as the
authoritative read-only risk source; unknown is never healthy. Add an offline
audit with explicit source freshness/account/schema coverage; correct the
reproduced dashboard reader issue with valid MQL blank-line separators.
Do not infer current VPS behavior from the available September-only local files.

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
Offline audit, ledger-reader regression and Hali compatibility tests, plus the
full versioned Python suite and git diff --check. No MQL files are changed in
this task, so MQL recompilation is not required for the current diff. Any later
MQL/execution/risk/SL/TP change requires the appropriate full suite and genuine
MT5 compilation. Local validation does not prove deployed behavior or recovery.

## Workflow / next task
Dedicated branch codex/runtime-trade-audit-20261022, based on e539b05.
Report findings and validate the minimal diff; human review required, NO
automatic merge/deployment. Preserve all existing research files, runtime
records, VPS assets/state/secrets and open positions. Reading existing Hali
records is authorized by the owner; deployment/restarts/activation are separate.
Do not implement Risk Budget Phase 2, pending monetary risk or new strategies.
See docs/RUNTIME_AUDIT_2026-10-22.md for evidence, limitations and the next wave.

## Integration review notes
The inherited public asset-root blocker is ACCEPTED_BY_REVIEW: asset paths
are decoded/validated and resolved strictly within dashboard/assets, including
symlink/junction confinement. Traversal fails with 403; the fixed /login resource
remains separately public. HTTP regression tests use synthetic files only.
The owner's final PR #13 review accepted both security fixes before merge;
VPS deployment remains unvalidated. VPS dashboard/assets is untracked and
must be preserved, not replaced/deleted by deployment or cleanup commands.
Login buckets now use only the TCP peer IP: behind local Caddy this is a
conservative global localhost limit (8 attempts / 5 minutes), not per-client
forwarded identity. No versioned Caddy sanitization contract was found; actual
production proxy behavior remains UNKNOWN. The identity fix was accepted in PR #13.
