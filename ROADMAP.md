# GoldScout — Roadmap

Legend:
- ✅ completed/validated
- 🟡 active/partially validated
- ⬜ pending
- ❌ researched and rejected for operational use

## Foundation
- ✅ Historical Replay
- ✅ Market Observer
- ✅ Outcome labeling
- ✅ Incremental historical processing
- ✅ percentage risk
- ✅ percentage daily loss budget
- ✅ broker-aware sizing
- ✅ TRAIN / VALIDATION / OOS research framework

## Stops / Take Profit
- ✅ Current SL analysis
- ✅ Adaptive Stop V2 research
- ✅ Adaptive Stop shadow/feature-flag implementation
- ✅ CHILL TP research
- ✅ GOD TP research
- ✅ Structure-Aware TP V1 research
- ✅ Structure-Aware TP V2 research
- ✅ TP V2 shadow/runtime comparison
- 🟡 Select TP V2 for DEMO and validate executed outcomes
- ⬜ early exit on momentum failure research
- ⬜ break-even research
- ⬜ structural trailing research
- ⬜ partial + runner research
- ⬜ freeze Exit Engine V1

## DEMO execution / lifecycle
- ✅ DEMO-account hard gating
- ✅ REAL-account hard block
- ✅ first DEMO order sent and filled
- ✅ position survived reconnect/restart at broker level
- 🟡 complete ENTRY → EXIT → RESULT telemetry
- ⬜ validate TP close
- ⬜ validate SL close
- ⬜ validate manual/other close classification
- ⬜ automated net PnL / realized R statistics

## Armed setup / stale bias
- ✅ runtime stale-bias issue identified
- ✅ Armed Invalidation V1 implemented/researched
- ❌ Armed Invalidation V1 as operational filter
- ⬜ revisit only with a broader evidence-based V2 after higher-priority work

## Models by setup
- ⬜ MOMENTUM model
- ⬜ CONTINUATION model
- ⬜ BREAKOUT model
- ⬜ PULLBACK model
- ⬜ RECOVERY model
- ⬜ calibrate threshold/SL/TP/regime/M15/expectancy per setup

## LONG vs SHORT specialization
- ⬜ edge by direction/setup
- ⬜ separate thresholds if justified
- ⬜ separate TP if justified
- ⬜ separate SL if justified
- ⬜ session/regime asymmetry

## Entry Quality
- ⬜ distance from impulse origin
- ⬜ ATR already traveled
- ⬜ distance to support/resistance
- ⬜ EMA20 extension
- ⬜ acceleration/deceleration
- ⬜ position in recent range
- ⬜ entering into structural obstacle

## M15 timing
- ✅ M15 exists
- ✅ M30 not used
- ⬜ refine M15 for timing, not primary direction
- ⬜ detect pullback before entry
- ⬜ avoid SHORT into support
- ⬜ avoid LONG into resistance
- ⬜ quantify OOS improvement

## Double-count audit
- ✅ structural bucket capped
- ✅ pattern bonus capped
- ⬜ audit structure/breakout/momentum/pattern overlap
- ⬜ measure feature correlation
- ⬜ remove redundant scoring if supported

## Macro context
Initially diagnostic, score_effect=0:
- ⬜ DXY
- ⬜ Treasury yields
- ⬜ real yields
- ⬜ risk-free rate
- ✅ world news
- ✅ high-impact USD macro calendar

## Opportunity / expected edge
- ⬜ historical expectancy by setup/regime
- ⬜ spread cost
- ⬜ slippage estimate
- ⬜ commission
- ⬜ structural quality
- ⬜ TRADE / NO_TRADE by net expected edge

## Position sizing by quality
- ✅ 5% current ceiling
- ✅ dynamic monetary risk
- ✅ daily limits
- ⬜ research lower CHILL risk
- ⬜ research differentiated GOD risk
- ⬜ never increase risk without OOS validation
- ⬜ separate conservative FUNDED profile

## Learning loop
- ✅ observations
- ✅ outcomes
- ✅ replay
- 🟡 execution lifecycle
- ⬜ automatic PAPER/DEMO statistics
- ⬜ daily/weekly summary
- ⬜ agent proposes changes
- ⬜ automatic backtest of proposals
- ⬜ human approval before strategy mutation
- ❌ autonomous live self-modification in V1

## Final validation
- ⬜ freeze V1
- ⬜ full backtest
- ⬜ walk-forward
- ⬜ untouched final OOS
- ⬜ several weeks DEMO
- ⬜ real DEMO profit factor
- ⬜ real DEMO expectancy
- ⬜ drawdown
- ⬜ setup distribution
- ⬜ news/gap/restart behavior

## 24/7 infrastructure
- ⬜ Windows VPS
- ⬜ MT5 autostart
- ⬜ dashboard autostart
- ⬜ watchdog
- ⬜ heartbeat
- ⬜ reconnect monitoring
- ⬜ log rotation
- ⬜ backups

## After V1
- ⬜ own-account profile with compatible capital
- ⬜ FUNDED profile
- ⬜ prop-firm-specific limits
- ⬜ challenge/demo validation
- ⬜ funded real only after stable DEMO evidence
