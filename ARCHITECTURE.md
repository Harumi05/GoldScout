# GoldScout — Architecture

## High-level components

### 1. MT5 Expert Advisor
Primary runtime trading logic for XAUUSD H1.

Responsibilities:
- H4/H1/M15 market state
- scoring and setup classification
- intrabar trigger monitoring
- SL/TP calculation
- broker-aware sizing
- daily risk budget
- DEMO order execution
- position/restart awareness
- runtime logging

Primary strategy families:
- MOMENTUM
- CONTINUATION
- BREAKOUT
- PULLBACK
- RECOVERY

These are not yet fully separated into independently calibrated models.

### 2. Strategy / policy modules
Current policy concerns include:
- score construction
- structural bucket
- patterns
- M15 timing
- session/news context
- stop selection
- take-profit selection
- ARMED-state handling
- account/risk safety

Feature flags should be used for research/PAPER/DEMO candidates.

### 3. Market Observer
Diagnostic event capture for:
- M15/H1/H4 snapshots
- ARMED states
- rejected/blocked decisions
- execution events
- future research labels

Default principle:
- observer_only=true
- score_effect=0

### 4. Dashboard
Python/web dashboard displays:
- current setup/direction/score
- account/equity/risk budget
- TP/SL shadow comparison
- DEMO execution state
- open/closed trade information where available
- diagnostic observer state

### 5. Historical Replay / Research
Research pipeline:
- reads historical tick CSV
- builds causal bars
- reconstructs closed-bar indicators/structure
- emits observations/outcomes
- runs TRAIN / VALIDATION / OOS analyses
- supports incremental processing

Do not rerun the 132M-tick replay unless necessary.

## Strategy flow
Typical runtime path:

market data
→ H4 context
→ H1 structure/setup
→ M15 timing
→ score
→ ARM
→ intrabar trigger
→ risk/sizing
→ SL/TP policy
→ account-mode safety
→ order request
→ broker fill
→ position lifecycle
→ close/result
→ observer/research feedback

## Safety boundaries
Execution safety should be checked immediately before broker order submission.

A valid signal is not a trade.
Expected lifecycle:
SIGNAL
→ ORDER_REQUESTED
→ ORDER_FILLED / ORDER_REJECTED
→ POSITION_OPEN
→ POSITION_CLOSED
→ RESULT

## Current risk decomposition (Phase 1 observation only)
- dailyBudget = persisted startOfDayEquity * DailyLossLimitPercent / 100.
- targetRisk = currentEquity * clamped RiskPercent / 100 (hard ceiling 5%).
- remaining = max(0, dailyBudget - dailyLossUsed - accountOpenRisk).
- plannedRisk = max(0, min(targetRisk, remaining)); broker sizing may use less.
- dailyLossUsed is the persistent maximum of the sum of negative NET eligible
  account deals (profit + swap + commission + fee), not separately summed gross
  loss plus all costs. Later profitable deals never restore consumed losses.
- Start equity is captured at the first successful refresh of a new server day,
  not guaranteed midnight equity. Peak equity persists; drawdown uses that peak.
- AccountOpenRisk includes ALL account positions, without symbol/magic filtering:
  OrderCalcProfit from entry to losing-side SL plus configured commission buffer.
  Missing/unreadable protection fails closed. It is not floating PnL or a new reservation.
- There is no MaxTradesPerDay=1 rule; one full-risk position can exhaust a 5% daily budget.
- H1 PENDING/CONFIRMED CAS reservation is account/server scoped; symbol, magic,
  engine and strategy are NOT separate owners. Same-H1 pending blocks conservatively.
- Monetary pending-risk reservation and atomic portfolio budget claim are NOT
  implemented. Ledger attempt locks do not reserve monetary risk.
- RiskBudgetSnapshot.mqh serializes supplied evidence only; the EA captures values
  using existing helpers. Dashboard and Observer receive the same nested risk_budget.
  Unknown values are null; pending_risk_supported=false. No snapshot field enters scoring.
- block_code reports the guard that actually rejected the current decision;
  budget_status_code describes the observed budget independently. A positive tiny
  remaining budget is not exhausted: sizing may subsequently reject the minimum lot.
- Legacy top-level fields remain for compatibility; risk_budget is authoritative
  for nullable/unknown evidence. candidate_planned_risk_amount is last sizing evidence
  with candidate_evaluated_at; planned_risk_amount is the current prospective limit.

## Future multi-strategy target — NOT current runtime
Market Observer → Gold Regime Engine → Strategy Router → DAY / SWING / SCALP
→ Expected Edge Filter → Portfolio Risk Manager → Execution Engine
→ Active Trade Manager → Ledger / Learning Loop.
Independent engine ownership, strategy-specific H1 reservations, pending monetary
claims and cross-engine portfolio arbitration remain future work, not enabled here.
Regime/Entry Quality research is offline diagnostic-only; no new runtime score effect.

## Repository areas
- MT5/: EA and MQL5 modules
- dashboard/: dashboard/server/UI
- research/: replay and research analyses
- tests/: automated tests/harnesses
- docs/: supporting project documentation
- AGENTS.md: multi-agent operating rules
- CONTEXT.md: current project state
- ARCHITECTURE.md: system structure
- ROADMAP.md: ordered plan
- CURRENT_TASK.md: active assignment

## Change policy
Risk, execution, SL, TP, and account-safety changes require stronger validation than ordinary diagnostic changes:
- relevant targeted tests
- full test suite
- MQL compilation
- runtime validation when applicable
- human review before merge
