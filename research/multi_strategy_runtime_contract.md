# Runtime contract inventory — phase 1

Read-only map of current `main`-derived runtime. These labels describe **concurrent-strategy readiness**, not whether the existing single-strategy EA is unsafe. No component is activated for multi-strategy execution by this phase. `CURRENT_TASK.md` still describes an older TP assignment; current code and tests take precedence, and the file is intentionally unchanged.

| Component | Current behavior and source | Classification |
| --- | --- | --- |
| Armed plan | One global `g_armed`, direction/score/setup; `ArmIntrabarPlan` and `MonitorIntrabar` operate on that one plan (`XAU_GoldScout_H1.mq5` around 272, 2678, 2832). | MUST_REFACTOR |
| Active signal identity | `g_activeSignalEventId` is global; `SignalEventId` hashes symbol, H1 bar, direction, setup and score, without strategy identity (around 256, 488). | MUST_REFACTOR |
| H1 entry reservation | Account-wide Global Variable key intentionally ignores symbol/magic, with PENDING/CONFIRMED and restart recovery (around 1485, 1748–1845). | MUST_REFACTOR |
| `OnePositionAtATime` / own exposure | NETTING blocks any symbol exposure/order; HEDGING checks own symbol+`MagicNumber`, unless explicit verified-DEMO multi-position allowance (around 1290–1392, 1611). | MUST_REFACTOR |
| Pending orders | `FindMatchingActiveOrder` shares the above policy; a pending own order blocks where one-position policy applies (around 1328, 1380). | MUST_REFACTOR |
| DEMO authorization | `EnableDemoExecution` plus positively identified `ACCOUNT_TRADE_MODE_DEMO`; `DemoExecution.mqh` and pre-send checks reject REAL/unknown. This hard block must remain. | SINGLE_STRATEGY_SAFE |
| Daily budget and drawdown | Account-wide persistent terminal Global Variables, start-of-day equity and realized loss/history checks; fail-closed on unavailable safety data (around 1473–1691). | UNSAFE_FOR_CONCURRENCY |
| Open risk and sizing | Account-wide `AccountOpenRisk` inspects all positions and `OrderCalcProfit`; target/planned risk capped by daily budget; broker validation before send (around 1693–1745, 3110–3327). | UNSAFE_FOR_CONCURRENCY |
| Restart recovery | `RecoverDemoPositionState` loops own symbol+magic positions but overwrites one global active signal identity; metadata is partly recovered per position (around 3780–3803). | MUST_REFACTOR |
| Lifecycle | `SIGNAL` → `ORDER_REQUEST` → `ORDER_FILLED` / `ORDER_REJECTED` → `POSITION_OPEN` → `POSITION_CLOSED`; trade comments and position identifiers help reconstruction (around 3258–3423, 4062–4127). | MUST_REFACTOR |
| Execution and TP/SL telemetry | Global `g_execution*`, stop and TP diagnostics are reset/serialized for the current single decision; dashboard JSON emits one current state (around 256–294, 3829–3900). | MUST_REFACTOR |
| Market Observer | One decision, score pair and signal context per snapshot; append-only `FILE_COMMON` JSONL (MarketObserver.mqh around 9–49, 198, 234, 353–400). | MUST_REFACTOR |
| Dashboard | `active_trade` single object plus `open_positions` list; page emphasizes one execution state (`UpdateDashboard`, `server.py`, `index.html`). | MUST_REFACTOR |
| `FILE_COMMON` writers | EA writes dashboard, observer, execution/trade files with MT5 file handles; no cross-sleeve transactional writer coordination contract exists. | UNSAFE_FOR_CONCURRENCY |
| Account mode / magic | NETTING ownership cannot be separated at position level; HEDGING can identify symbol+magic but existing single MagicNumber is not a strategy ID. | MUST_REFACTOR |

Current protected defaults in `XAU_GoldScout_H1.mq5`: `EnableLiveTrading=false`, `RiskPercent=5.0`, `DailyLossLimitPercent=5.0`, `ArmScoreThreshold=58`, `MinScoreToTrade=74`; Adaptive Stop, TP V2 and Armed Invalidation remain independently feature-flagged. Phase 1 changes none of them.
