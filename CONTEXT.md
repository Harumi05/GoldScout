# GoldScout — Current Context

Last updated: 2026-09-19

## System
GoldScout is an XAUUSD trading system centered on MT5.

Timeframes:
- H4: context
- H1: primary structure/setup/decision
- M15: timing
- no M30

Core inputs/constraints:
- RiskPercent = 5.0
- DailyLossLimitPercent = 5.0
- ArmScoreThreshold = 58
- MinScoreToTrade = 74
- EnableLiveTrading = false
- DEMO execution is allowed only after account-mode verification
- REAL-account order sending is hard-blocked

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
- targetRisk = current equity * RiskPercent
- dailyLossBudget = startOfDayEquity * DailyLossLimitPercent
- remaining budget accounts for realized daily loss and open risk where implemented
- planned risk cannot exceed remaining daily budget

Do not tighten SL artificially to make small accounts executable.

## Stop research
Adaptive Stop V2 was researched and implemented as optional PAPER/shadow logic.
It reduced premature stops in research but traded some +1R/+2R reach.
It is not a reason to modify live behavior automatically.

## Take-profit research
Key findings:
- CHILL 0.75R was the strongest robust CHILL baseline in OOS.
- GOD 2R was too ambitious.
- Structure-Aware TP V1 was too restrictive.
- Structure-Aware TP V2 was promising for GOD.

Current intended DEMO direction:
- CHILL: 0.75R
- GOD: Structure-Aware TP V2
- CURRENT retained as shadow comparator

Verify actual runtime selector before assuming V2 is active.

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
