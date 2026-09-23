# Multi-strategy activation blockers — phase 1

The table is a **future-work gate**, not a runtime modification. No SCALP or MULTIDAY strategy, concurrent sleeves, changed scoring, changed order path or REAL execution is authorized. Baseline INTRADAY remains the current H1 system.

| Blocker | Current behavior | Failure mode under concurrency | Required future change | Test before activation |
| --- | --- | --- | --- | --- |
| Global `g_armed` | One armed plan/direction/setup. | One sleeve overwrites or cancels another. | Key plans by immutable strategy/signal identity. | Two simultaneous opposing plans remain independent through intrabar, cancellation and restart. |
| `g_activeSignalEventId` | One mutable signal ID, recovered from each own position in a loop. | Events/closures can attach to the wrong signal. | Persist signal ID per order/position/sleeve. | Interleaved fills, partial closes and restart map back to correct IDs. |
| H1 reservation | Account-wide PENDING/CONFIRMED entry per H1, independent of magic. | Legitimate independent sleeves block each other, or bypassing it removes duplicate protection. | Define explicit per-strategy/portfolio reservation policy and atomic keying. | Concurrent CAS, crash recovery, failed orders, same-H1 duplicate rejection. |
| Execution telemetry | One global current execution state/reason/retcode. | A rejected order is displayed as another sleeve's fill. | Event-sourced per-order state and aggregate view. | Two interleaved accepted/rejected requests render correctly. |
| TP/SL telemetry | One current/shadow/selected diagnostic tuple. | Stop/TP comparison leaks across signals. | Key by decision/order ID with explicit NOT_EVALUATED. | Alternating strategies and no-evaluation paths never reuse stale values. |
| Market Observer | One decision and score pair per snapshot. | Ambiguous feature ownership, double counting or overwritten decision. | Strategy-scoped observation records, stable as-of timestamps. | Per-sleeve causal record and `score_effect=0` research isolation. |
| Dashboard `active_trade` | One active object, despite an `open_positions` list. | Multiple trades misrepresented or wrong selected trade. | Explicit multi-position aggregate and per-position cards. | Multiple same-symbol positions, partial close, broker rejection, restart. |
| `FILE_COMMON` writers | Independent MT5 file handles; no multiwriter protocol. | Interleaved/truncated JSONL or overwritten dashboard state. | One owner/queue or atomic append and versioned reader contract. | Concurrent process stress, interruption mid-write, recovery. |
| HEDGING/NETTING | NETTING shares symbol exposure; HEDGING own magic is checked, with verified-DEMO exception. | NETTING sleeves net into each other; magic collision or hidden pending order. | Account-mode-specific portfolio policy and unique sleeve ownership. | Manual/other-EA exposure, same-symbol hedging, pending orders, partial fills. |
| Atomic risk reservation | Account budget and open risk are checked around each send, but no per-sleeve monetary reservation transaction exists. | Two near-simultaneous decisions can each see the same remaining budget. | Atomic portfolio-level reservation/commit/release with fail-closed recovery. | Parallel orders cannot exceed daily/open-risk caps; crash during PENDING. |
| Restart/reconciliation | One global recovered signal and position metadata loop. | Orphan positions or wrong sleeve/lifecycle after restart. | Ledger keyed by strategy+signal+position, reconciled against broker orders/deals. | Restart after request/fill/partial close/reject, H1 reservation recovery. |

## Historical evidence and readiness

The frozen data has 132,193,488 **valid** quote ticks, existing M1/M15/H1/H4 bars and technical M5/D1 estimates. It does not supply actual broker slippage, network/order latency, depth/order book, queue position, guaranteed real traded volume, or commissions/fees and financing/swap where absent. Bar summaries alone do not preserve the exact executable BID/ASK path; targeted raw-tick replay can reconstruct quotes, not the true historical fill process. Unknown values must stay unknown, not zero. Broker-server UTC offset and rollover are unverified, so London/New York/overnight labels must not be inferred from the PC zone.

| Horizon | Research readiness | Gate |
| --- | --- | --- |
| INTRADAY | `BASELINE_AVAILABLE` | Current H1 setup and existing outcomes provide a baseline, subject to documented replay/fill limitations. |
| SCALP | `NOT_READY_FOR_STRATEGY_SELECTION` | Verify quote-to-execution cost model (spread, slippage, latency, commission) and causal M5 data before ranking candidates. |
| MULTIDAY | `NOT_READY_FOR_STRATEGY_SELECTION` | Verify financing/swap, weekend exposure, broker D1 rollover/timezone and horizons longer than the existing 4h outcomes. |

**Recommended phase 2:** independently verify broker-server time offset/rollover, then materialize versioned closed-M1→M5 and technical D1 datasets with provenance, coverage and `available_at`. Validate against MT5 bars across pauses/weekends, build causal scheduler and targeted tick-cost experiments. Do not select SCALP/MULTIDAY or loosen current portfolio safety gates until the corresponding readiness and runtime blockers have tests and explicit owner approval.
