# GoldScout — Current Context

Last updated: 2026-09-29 (repository evidence; deployment not inspected)

## System
GoldScout is an XAUUSD trading system centered on MT5.

Timeframes:
- H4: context
- H1: primary structure/setup/decision
- M15: timing
- no M30

REPO DEFAULTS (not proof of deployed VPS / MT5 inputs):
- RiskPercent = 5.0
- DailyLossLimitPercent = 5.0
- ArmScoreThreshold = 58
- MinScoreToTrade = 74
- EnableLiveTrading = false
- EnableDemoExecution = false
- UseAdaptiveStopV2 = false
- UseTakeProfitV2 = false
- UseArmedInvalidationV1 = false
- MaxDrawdownPercent = 15.0
- DEMO execution is allowed only after account-mode verification
- REAL-account order sending is hard-blocked

DEPLOYED VPS / MT5 INPUTS = UNKNOWN UNTIL SNAPSHOT IS PROVIDED.
Owner reports Windows VPS / MT5 running 24/7; no autostart/watchdog evidence
is implied. The dashboard already exposes demo_execution.enabled/account_mode,
live_trading and risk inputs; inspect a real runtime snapshot before inferring activation.

## Broker/runtime
Capital.com MT5 demo contract observed:
- account/profit currency: USD
- contract size: 100
- volume min/step: 0.01
- tick size: 0.01
- tick value: 1.0

A real DEMO order was successfully sent and filled:
- XAUUSD BUY 0.01
- entry 4361.12
- SL 4329.02
- TP 4401.62
This validated broker-side DEMO execution and persistence across terminal reconnect/restart.

## Historical data/research
Historical replay processed approximately 132.2M valid XAUUSD ticks from 2025-05-15 through 2026-09-11.

Replay outputs include:
- M1/M15/H1/H4 bars
- Market Observer events
- future outcomes
- TRAIN / VALIDATION / OOS partitions

Historical score replay is a causal closed-bar core, not a perfect reproduction of all runtime inputs.

## Risk/sizing
Risk uses current equity and start-of-day equity:
- targetRisk = current equity * clamped RiskPercent / 100
- dailyLossBudget = persisted startOfDayEquity * DailyLossLimitPercent / 100
- remaining = max(0, dailyLossBudget - dailyLossUsed - accountOpenRisk)
- planned risk cannot exceed remaining daily budget

Loss used = persistent maximum of sum of negative net eligible account deals,
including costs netted per deal. Profitable deals do not reimburse it. Open risk
is account-wide; absent SL fails closed. Start equity is first successful server-day
refresh equity, not a guaranteed midnight sample. There is no monetary pending-risk
reservation and no explicit one-trade-per-day rule; H1 reservation is independently
account/server scoped (not magic/symbol/strategy scoped). Phase 1 adds diagnostic
risk_budget with null/UNKNOWN and exact guard codes without changing these semantics.

Do not tighten SL artificially to make small accounts executable.

## Stop research
Adaptive Stop V2 is optional PAPER/positively verified DEMO + shadow logic;
REAL/unknown account mode cannot select it. Source default remains false.
It reduced premature stops in research but traded some +1R/+2R reach.
It is not a reason to modify live behavior automatically.

## Take-profit research
Key findings:
- CHILL 0.75R was the strongest robust CHILL baseline in OOS.
- GOD 2R was too ambitious.
- Structure-Aware TP V1 was too restrictive.
- Structure-Aware TP V2 was promising for GOD.

Implemented TP V2 behavior WHEN its feature flag is enabled in PAPER/DEMO:
- CHILL: 0.75R
- GOD: Structure-Aware TP V2
- CURRENT retained as shadow comparator

Verify actual runtime selector before assuming V2 is active.

DEMO ledger + position recovery V2 are merged (PR #11); fill/close costs,
partial-close/restart reconciliation and optional excursion data exist in code.
This does not prove the complete lifecycle is operationally validated on the VPS.
Gold Regime Engine / Entry Quality are offline diagnostics, score_effect=0;
DAY/SWING/SCALP ownership and runtime routing are not implemented.

## Armed/stale-bias research
A stale ARMED-direction problem was observed in runtime.

Armed Invalidation V1 was implemented/researched with a conservative counter-breakout + RSI + impulse rule.
Research result:
- 3,511 ARMED episodes
- only 2 cancellations
- 1 saved loser
- 1 killed winner
- 0 OOS cases
Conclusion: KEEP DIAGNOSTIC. Do not enable operationally yet.

## Market Observer / learning
Market Observer records M15/H1/H4 snapshots and decision/execution events.
Outcome labeling supports future 15m/1h/4h evaluation.
Observer features remain diagnostic unless explicitly promoted.

## Immediate project philosophy
Do not maximize trade count.
Do not force high R:R.
Prefer:
- positive OOS expectancy
- PF > 1 OOS
- lower variance
- controlled drawdown
- structural coherence
- reproducible DEMO performance

## Important open work
See ROADMAP.md and CURRENT_TASK.md.
