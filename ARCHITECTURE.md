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
→ ORDER_REQUEST
→ ORDER_FILLED / ORDER_REJECTED
→ POSITION_OPEN
→ POSITION_CLOSED
→ RESULT

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
