# DEMO trade ledger V2 — runtime contract

This phase records broker evidence from DEMO orders. It does not change signal,
score, risk, stop, target, or REAL-account authorization. `market_execution_events.jsonl`
in MT5 `Common/Files` is append-only. `schema_version=2`, `source=MT5`,
`strategy_id=INTRADAY_H1_BASELINE`, and `policy_version=DEMO_LEDGER_V2` identify
new records. Future sleeves, entry-quality, regime and expected edge remain null.

## Pre-change lifecycle audit

| Field / transition | Previous provenance | Limitation |
| --- | --- | --- |
| SIGNAL, ORDER_REQUEST, ORDER_REJECTED | LOCAL_ONLY plus broker retcode on rejection | no durable request identity or sequence |
| ORDER_FILLED | RELIABLE_BROKER_SOURCE for deal/order/retcode when provided | fill could also appear through transaction callback; no deduplication |
| POSITION_OPEN / POSITION_CLOSED | RELIABLE_BROKER_SOURCE for deal/position and History PnL | partial entry emitted repeated opens; close append could repeat on restart |
| `GoldScoutTradeMetadata` | LOCAL_ONLY broker comment | comment can be truncated/changed; not a durable identity |
| `GoldScoutClosedTrade` | DERIVED from `HistorySelectByPosition` | fee folded into commission; initial risk could be derived from later SL |
| request spread / slippage | LOCAL_ONLY request tick; absolute executed difference | no signed slippage; unknown fill spread serialized as zero |
| MFE / MAE | MISSING | live path had no excursion persistence |
| `RecoverDemoPositionState` | RELIABLE_BROKER_SOURCE open positions | no ledger reconciliation or closed backfill |
| dashboard `session_stats` | DERIVED directly from current broker history | not reconciled ledger; no cost/segment breakdown |

Legacy `ORDER_REQUEST` and `POSITION_PARTIAL_CLOSE` are accepted on read, but
new records emit `ORDER_REQUESTED` and `PARTIALLY_CLOSED`. A deterministic
`event_id` is keyed by event, stable trade identity and broker deal/position
where available. A restart reloads known event IDs and position states before
reconciliation. `signal_event_id` groups one logical setup; each broker send
first claims a durable `execution_attempt_id` under the ledger writer lock.
`trade_id` is account/symbol/magic/attempt scoped, so a safely rejected attempt
cannot contaminate a later retry of the same H1 signal. A broker orphan has an
explicit position-scoped identity. The compact `GS2` broker comment carries the
signal hash and persisted attempt ordinal as a recovery hint, not as the source
of truth. The claim must match signal, symbol and magic exactly. A missing or
mismatched claim is a reconciliation error, not a verified fill.

Broker facts are not inferred from price proximity: close reason uses
`DEAL_REASON`; unsupported reasons are `OTHER` or `UNKNOWN`. Unknown prices,
spread, slippage, costs or initial risk are null. Initial risk is captured at
entry and is never recomputed from a modified closing stop. Realized R is
null unless initial account-currency risk and broker net PnL are both known.
`order_planned_risk` is the pre-send budget and is never summed as fill risk.
Each `ORDER_FILLED` contributes only its own `fill_risk_account_currency` based
on executed volume/price. `position_initial_risk` is the sum of all unique
fill risks; if any fill risk is missing, or broker entry-deal count/volume does
not match captured fills, `riskKnown=false` and realized net R stays null.
Broker commission, swap, fee and gross getters are checked
individually. Unknown cost evidence is null (`cost_quality=UNKNOWN`), never
silently zero; those rows retain reliable gross PnL but are excluded from net
cost statistics.
`spread_at_fill`/`spread_at_exit` are only present when the terminal's quote is
within one second of the deal timestamp; they are nearby observed quotes, not
a broker-certified execution spread. Excursions use delivered DEMO ticks and
the executable bid/ask side. `OBSERVED_SAMPLES` does not claim every broker
tick was delivered; after restart/offline time quality becomes `PARTIAL`.
`mfe_r`/`mae_r` are explicitly price-distance R proxies, not realized PnL.
If there was no sampled quote before an offline close, excursion fields remain
null even though quality is `PARTIAL`; zero is never used to imply no movement.

The dashboard reads reconciled terminal close events, never the transient UI
state, and will display ledger corruption. This read-only dashboard path cannot
affect trading. An ambiguous terminal ledger/broker mismatch prevents *new*
DEMO entries but never closes or modifies an existing position.
An order result that is not provably final is recorded as
`RECONCILIATION_ERROR/AMBIGUOUS_ORDER_RESULT`, not `ORDER_REJECTED`; that
trade identity remains blocked across restart until a matching broker fill is
reconciled. Ledger recovery and deal capture run only with a positively
identified DEMO account; REAL and unknown account modes fail closed.
Account-scoped dashboard projections omit legacy events lacking `account_login`;
the EA may still use their position IDs to avoid duplicate lifecycle writes.
The writer validates every critical JSONL record with a strict schema parser
and rescans the entire ledger under its writer lock before appending critical
events. This is intentional for strong idempotency across EA instances and
ledgers larger than 256 KB; an invalid/truncated row marks the ledger unhealthy
and blocks new DEMO entries. Only noncritical excursion samples use a bounded
tail scan. `ledger_conflicts` and `conflicting_trade_ids` are visible in the
dashboard when statistics exclude trades. A NETTING `DEAL_ENTRY_INOUT` is not
treated as a new entry: if old/new exposure cannot be split unambiguously, the
old ledger position becomes a persistent reconciliation error and new DEMO
entries are blocked; no open broker position is modified. HEDGING `INOUT` is
also treated as unexpected rather than inventing a strategy identity.

## DEMO validation checklist (Capital.com)

- Let a normal DEMO TP close and compare deal tickets, gross/net, commission,
  swap, fee, close reason and realized R with MT5 History.
- Repeat for a normal SL and a user-initiated manual close.
- Restart terminal with an open position, then reload the EA; confirm one trade
  identity, one open event, no duplicate close/stat row, and partial excursion
  quality when the EA was offline.
- Disconnect/reconnect broker and confirm no new entry if reconciliation is
  ambiguous. Do not manipulate an open trade to force this test.
- If the broker naturally returns a partial fill or rejection, verify every
  distinct deal is recorded once and rejected orders never create an open row.

No trade should be forced merely to produce a test case. Broker-side tick
history, commission posting and exact fill-time quote availability remain
subject to DEMO verification.
