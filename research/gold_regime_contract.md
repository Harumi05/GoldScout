# Gold Regime Engine V1 — diagnostic/offline contract

This research code reads the existing historical decision/observation/outcome sidecars and causal M5 bars. It never imports or feeds the EA. `diagnostic_only=true` and `score_effect=0` are mandatory on every regime row. No SCALP, MULTIDAY, concurrency, entry/exit, TP/SL or risk rule is promoted by this analysis.

## Time and provenance

All integer milliseconds encode **MT5 server wall time, not verified UTC**. Broker offset, rollover and DST are UNKNOWN, so there are no session or overnight labels. D1, DXY, yields and any future macro release are excluded. M5 is taken only from the Phase 2 closed-M1 output. Persisted H1/H4 bars have no native availability marker; a later *closed and available* M5 bucket with `start >= H1/H4.end` confirms each. The resulting wrapper has `start`, `end`, `available_at`, timeframe and source/confirmation bar IDs. Every feature checks `available_at <= decision_time`. This deliberately delays H1/H4 rather than treating a bar at its final tick as instantly available. Gaps remain gaps, no forward-fill. An H1/H4 bar without later confirming M5 is excluded.

Confirmed swing highs/lows use two left and two **closed right** bars; a pivot can first appear only after the second right bar is available. Structure is bullish only with HH+HL, bearish only with LH+LL, range only with ATR-tolerant equal highs/lows, else mixed/unknown. Breakout uses close versus prior 10 bars, not wick. Quote tick count is activity, **not traded volume**. ADX/DI are causal 14-bar diagnostics, not claimed byte-identical to MetaTrader smoothing or live scoring.

## Dimensions and precedence

Each M5/H1/H4 stores numerical features and `trend_state`, `volatility_state`, `structure_state`, `transition_state`, plus a composite `gold_regime`. Composite precedence: EVENT_SHOCK (contemporaneous ATR + range/spread extreme), TRANSITION, TREND_UP/DOWN, RANGE, VOL_EXPANSION/CONTRACTION, UNKNOWN. Shock never uses a subsequent return. TRANSITION records a change between the prior and current closed-bar trend state, including UP/DOWN↔FLAT; it is not inferred from subsequent price movement. Alignment keeps higher-TF trend and lower-TF pullback context separately; it is not a trade signal. The H1 composite receives a two-distinct-H1-bar hysteresis (`entry_confidence=.55`, `exit_confidence=.45`); contemporaneous EVENT_SHOCK and TRANSITION events may enter immediately, while return to trend/range requires persistence. Regime age and transitions count distinct closed H1 bars, not repeated M15 decisions.

## Calibration and evaluation

Decision times define chronological TRAIN 60% / VALIDATION 20% / OOS 20%. Volatility p20/p80 and absolute EMA20-slope p60/p35 are fitted from bars available no later than TRAIN end, with no outcomes. TRAIN classifications use a bounded **past-only prefix** (up to 256 prior bars) rather than the final TRAIN profile, avoiding retrospective parameter look-ahead inside TRAIN. The final TRAIN profile is frozen before VALIDATION/OOS. `parameter_version=gold-regime-v1-train60-online-prefix`. Numerical regime profiles are diagnostic and are **not optimized for profitability**.

The analysis joins 1h outcomes only **after** the regime file has been frozen. Direction uses persisted `armed_direction`, then dominant persisted long/short score; ties remain ambiguous. Setup labels are persisted M15 breakout/momentum/pullback/recovery flags and can overlap. No exact CONTINUATION setup is persisted, so that segment remains unavailable rather than invented. Predefined minimum samples: `SUFFICIENT >=100`, `THIN_SAMPLE 30–99`, `INSUFFICIENT <30`.

The dataset has no executed historical trade/fill/TP/SL tape. Thus win, expectancy and profit factor in this report are explicitly **gross directional underlying-price 1h return proxies**, not realized trading performance or R. `ARMED_PROXY` is not an opened trade. The engine cannot establish that a regime should alter a live strategy. No session attribution is allowed until the broker clock is independently verified.

## Commands and ignored outputs

Run `python -m research.gold_regime_engine` then `python -m research.analyze_regime_edge`. The first writes ignored `research/output/regimes/gold_regime_v1.jsonl`; the second writes ignored `research/analysis/gold_regime_v1.md` and `.csv`. The 132,193,488-tick CSV is not reopened. Outputs are derived and can be regenerated; neither large output nor secrets belong in Git.
