# GoldScout — Multi-Agent Operating Rules

## Source of truth
GitHub is the persistent source of truth for GoldScout. Do not rely on model memory when code, tests, CURRENT_TASK.md, or repository docs disagree.

Priority:
1. Current repository code
2. Automated tests
3. CURRENT_TASK.md
4. CONTEXT.md
5. ARCHITECTURE.md
6. ROADMAP.md
7. Older notes/handoffs

If docs conflict with code, flag the conflict instead of silently assuming.

## Project goal
Build and validate GoldScout for XAUUSD with profitability and robustness prioritized over trade count or nominal R. Optimize for positive OOS expectancy, stable profit factor, controlled drawdown, and safe execution.

## Agent roles

### Claude — primary implementer
Use Claude mainly for:
- feature implementation
- architecture changes
- focused refactors
- documentation of implemented behavior
- targeted research code

Claude must:
- read AGENTS.md and CURRENT_TASK.md first
- inspect only files needed for the assigned task
- work on a task branch, not directly on main
- keep diffs minimal
- update/add tests for behavior changes
- report cause, files changed, tests, compile result, and limitations
- stop if the task would require changing protected safety constraints without explicit approval

### Codex — primary reviewer/validator
Use Codex mainly for:
- code review
- regression detection
- test expansion
- compile validation
- behavior verification
- small corrective fixes found during review

Codex must:
- review the exact assigned scope
- compare implementation against CURRENT_TASK.md
- run relevant tests and full suite for execution/risk/SL/TP changes
- verify no unrelated behavior changed
- report issues before redesigning architecture
- work on a separate branch when making non-trivial changes

## Shared workflow
1. Read AGENTS.md.
2. Read CURRENT_TASK.md.
3. Pull latest target branch.
4. Confirm working tree is clean or explicitly report pending work.
5. Work only in assigned scope.
6. Run tests/compilation.
7. Commit to a task branch.
8. Reviewer validates the branch or PR.
9. Human approves merge.
10. Update CURRENT_TASK.md / ROADMAP.md when the task is accepted.

Do not let Claude and Codex edit the same files simultaneously unless explicitly coordinated.

## Critical safety constraints
- REAL account execution must remain hard-blocked unless the owner explicitly changes the project policy.
- DEMO execution may be enabled only when the MT5 account is positively identified as DEMO.
- EnableLiveTrading must remain false unless explicitly authorized by the owner.
- RiskPercent must not be increased above the current configured ceiling without explicit approval.
- DailyLossLimitPercent must not be increased without explicit approval.
- Do not remove broker/risk validation to increase trade frequency.
- Do not commit API keys, tokens, passwords, account secrets, or credentials.
- XAUUSD is the primary instrument; H1 is the primary setup/decision timeframe.
- H4 provides context; M15 is timing/supporting context; M30 is not used.

## Current protected strategy assumptions
- Percentage-based risk and daily loss budget.
- Broker-aware sizing and volume-step handling.
- SL based on ATR + structure, with Adaptive Stop research isolated behind feature flags.
- TP CURRENT and TP V2 can be compared in shadow mode; behavior selection must follow CURRENT_TASK.md.
- Market Observer remains diagnostic unless a task explicitly changes its effect.
- Research features start with score_effect=0 unless validated and explicitly promoted.

## Context efficiency
- Do not reread unrelated files.
- Do not restate the entire architecture in every response.
- Prefer exact file references and small diffs.
- If a problem is unrelated to the current task, mention it briefly and do not fix it without authorization.
- Do not re-run expensive historical replay unless the task requires it.
- Reuse existing research outputs when valid.

## Required report after a task
Report only:
- cause / objective
- files changed
- tests
- compile result
- behavior verification
- limitations
- commit/branch/PR status
