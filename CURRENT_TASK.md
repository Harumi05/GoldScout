# GoldScout — Current Task

## Status
READY

## Owner
Claude

## Reviewer
Codex

## Task
Promote Take Profit V2 from shadow comparison to the TP selected for DEMO/PAPER execution, while keeping CURRENT as the shadow comparator.

## Required behavior
CHILL:
- selected TP = 0.75R

GOD:
- selected TP = Structure-Aware TP V2
- baseline = 1.25R
- HIGH/MEDIUM structural obstacle may cut TP
- buffer = 0.20 ATR
- LOW confidence does not cut
- LOW_REWARD remains allowed according to validated V2 behavior

CURRENT:
- continue calculating/logging as shadow
- do not use CURRENT as selected TP in DEMO when V2 is enabled by this task

## Scope
May modify only files required for:
- TP selection
- DEMO/PAPER execution plumbing
- TP shadow telemetry
- dashboard display
- tests/harnesses directly related to this behavior

## Do not change
- RiskPercent
- DailyLossLimitPercent
- scoring
- ArmScoreThreshold
- MinScoreToTrade
- SL logic
- Adaptive Stop behavior
- M15 scoring
- patterns
- news
- account-mode REAL hard block
- EnableLiveTrading policy

## Validation
Required:
- targeted TP tests
- full test suite
- main EA compile: 0 errors / 0 warnings
- Market Observer harness compile: 0 errors / 0 warnings
- CURRENT shadow values still emitted
- selected TP demonstrably V2 in DEMO/PAPER
- REAL-account execution remains blocked

## Runtime examples to verify
CHILL:
- selected RR approximately 0.75R

GOD:
- selected TP follows Structure-Aware V2 and can be below 1.25R only for validated structural reasons

## Handoff
Claude implements on a dedicated branch and reports:
- files changed
- tests
- compile results
- example CURRENT vs V2 vs selected output
- limitations
- commit hash

Codex then reviews the branch/PR against this file.

## Note
Armed Invalidation V1 remains diagnostic and should not be enabled or mixed into this task.
