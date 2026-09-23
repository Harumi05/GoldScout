# Causal research contract — phase 1

This contract applies to future research and does **not** alter the EA. Timestamp integers in [`multi_strategy_causal_bars.py`](multi_strategy_causal_bars.py) are milliseconds encoded in MT5 broker-server wall time. Until the broker UTC offset and rollover are independently verified, they are not UTC timestamps, and session/overnight labels are disallowed.

For every feature used in a historical decision, persist `source_bar_ids`, `bar_start`, `bar_end`, `closed`, `available_at`, `decision_time` and provenance. The admissibility rule is `closed == true AND bar_end <= decision_time AND available_at <= decision_time`. It applies separately to every input in a multi-bar feature; the feature's `available_at` is at least the maximum of its inputs and any confirmation delay. If any value is missing or ambiguous, the feature is unavailable rather than backfilled. Outcome horizons are labels, never admissible signal features. The prototype's `require_causal_feature` enforces this for `pivot`, `structure`, `regime`, `setup` and `entry_quality`.

| Bar | Close/availability contract | Phase-1 capability |
| --- | --- | --- |
| M1 | Source record must be closed; earliest mathematical availability is its end, though actual replay notification can be later. | Persisted input; no open M1 confirmation. |
| M5 | Five aligned, closed M1 minutes; any missing minute leaves `complete=false`. Available no earlier than the next closed M1 that establishes rollover. | Tested streaming prototype; not materialized. |
| M15 | Persisted replay bar may be read only after its close and actual replay availability. | Existing output; not rebuilt here. |
| H1 | Same closed-bar rule; no current open H1 for pivot/structure/regime/setup/entry quality. | Existing output; primary setup timeframe remains unchanged. |
| H4 | Same closed-bar rule; context cannot see the current open H4. | Existing output; no change to live H4 scoring. |
| D1 | Explicit server-wall `broker_day_boundary`; only closed M1 inputs, later closed M1 rollover evidence and no inferred missing minutes. | Technical bucket prototype only; session status `UNVERIFIED`. |

An open bar may eventually be used for an explicitly separate, named *intrabar* measurement, but it cannot confirm a pivot, structure, regime, setup or entry-quality feature. This phase implements no intrabar research feature. A pivot with right-side bars is available only after **all** right-side bars close; its pivot timestamp is not its decision availability. Similar delay applies to higher-timeframe structure and patterns. When linking observations to outcomes, join only after the decision record is frozen; never use future return, MFE or MAE to select direction or a setup.

`ClosedM1.from_replay_record` falls back to `close_timestamp` when the existing replay output lacks `available_at`; this is a *lower bound*, not proof of physical receipt at that instant. The M5/D1 prototype conservatively delays output until a later closed M1 is ingested. Before any strategy-selection backtest, a replay scheduler must define and test when each persisted M15/H1/H4 and derived M5/D1 value actually becomes available at the decision event, including gaps and restart boundaries. No forward fill is allowed.
