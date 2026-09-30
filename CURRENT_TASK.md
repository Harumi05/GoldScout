# GoldScout — Current Task

## Status
Risk Budget V2 Phase 1: MERGED / ACCEPTED via PR #12.
Main merge: 968ad45d7b2b538e7164f14b9120b48bcf5b9e10.
Current task: Hali + main integration implemented; PR #13 remains DRAFT, pending human review.
Security asset-root blocker: ACCEPTED_BY_REVIEW.
Login rate-limit proxy trust: FIXED_PENDING_REVIEW; socket peer only, ignoring forwarded headers.
No VPS deployment has been performed or validated by this integration task.

## Owner / Reviewer
Implementer: Codex. Reviewer: Human / ChatGPT.

## Objective
Merge current main into a Hali-derived integration branch without rewriting
either history. Preserve Hali UI/auth/health/one-second read-only chart alongside
DEMO Ledger / Recovery V2 and Risk Budget Phase 1. MT5 risk_budget remains the
authoritative read-only source for the Hali risk panel; unknown is never healthy.

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
Hali auth/health/live chart, dashboard/Observer/ledger/risk compatibility tests,
protected safety characterization and the full versioned Python suite. Compile
the main EA, Observer/ledger harnesses and HaliLiveFeed with real MT5 includes.
Run git diff --check and report actual results in the PR. Local validation is
not evidence of VPS deployment, HTTPS/assets configuration or MT5 recovery there.

## Workflow / next task
Dedicated branch codex/hali-main-integration-v1, based on codex/hali-dashboard-v1;
merge origin/main, preserve provenance, then PR against main. Human review
required, NO automatic merge or deployment. Preserve the two untracked TP V2.1
files and all VPS assets/state/secrets. Do not implement Risk Budget Phase 2,
pending monetary risk or new strategies. Capture actual DEMO runtime evidence
only after a separately approved, controlled VPS deployment.

## Integration review notes
The inherited public asset-root blocker is ACCEPTED_BY_REVIEW: asset paths
are decoded/validated and resolved strictly within dashboard/assets, including
symlink/junction confinement. Traversal fails with 403; the fixed /login resource
remains separately public. HTTP regression tests use synthetic files only.
Human security review is required before marking PR #13 ready; no security
approval or VPS deployment is claimed. VPS dashboard/assets is untracked and
must be preserved, not replaced/deleted by deployment or cleanup commands.
Login buckets now use only the TCP peer IP: behind local Caddy this is a
conservative global localhost limit (8 attempts / 5 minutes), not per-client
forwarded identity. No versioned Caddy sanitization contract was found; actual
production proxy behavior remains UNKNOWN. The identity fix awaits human review.
