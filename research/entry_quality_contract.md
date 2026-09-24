# Entry Quality V1 — offline causal contract

This is a research sidecar. It does not enter the EA, dashboard, scoring, risk,
order execution, or PAPER selection path. Every row has `diagnostic_only=true`
and `score_effect=0`. `event_id` joins the frozen historical observation and
Gold Regime V1 row; no outcome file is opened by the feature generator.

## Availability and provenance

`decision_time` is the persisted observation's `observed_at` in MT5
server-wall milliseconds, **not verified UTC**. M5 derives only from closed M1.
H1/H4 are usable only after a later closed M5 proves their close; the frozen
regime row retains bar ID, `end`, `available_at`, and provenance. For every
context, `end <= available_at <= decision_time`. Any open/future context fails.
Confirmed H1 pivots require two closed bars on each side, and become available
only when the second right bar is available. No current open bar confirms a
pivot, level, impulse origin, range, or timing feature.

Freshness is predeclared independently of outcomes: M5 >15 minutes, H1 >120
minutes, H4 >480 minutes is `STALE` (strictly greater). Study tiers are H1
>120/>240/>480 and H4 >480/>720/>1440 minutes. `context_age_bars` means
elapsed wall-time equivalents, not observed bars; gaps are not filled. A stale
feature is **marked**, not blocked or set to zero. Missing context is `UNKNOWN`.

## Feature definitions and missing values

- Direction is persisted `armed_direction`, then strictly dominant persisted
  long/short score; ties are unknown. Setup flags are read from the observation
  in that direction. Multiple direct flags are retained in `setups`; the
  single segmentation label `setup` uses predeclared BREAKOUT > MOMENTUM >
  RECOVERY > PULLBACK priority (otherwise null). This is not EA setup
  replay. There is no exact historical CONTINUATION flag.
- Impulse origin is the latest confirmed H1 low for LONG or high for SHORT
  within the last 30 observed H1 bars. This is a proxy for a discrete impulse,
  not an EA trade setup or live SL. Values can be signed if price has crossed
  the origin. Structural support/resistance are the nearest *confirmed pivot*
  below/above the entry price. Equal-high/low zones are not inferred.
- Extension/distance quantities use H1 ATR when finite and positive. Signed
  EMA extension is in the causal direction. Recent-range position uses 10,
  20, or 30 closed H1 high/low windows with no forward fill. Raw position is
  0 at the low and 1 at the high; the separate directional transform is
  `1-raw` for SHORT so cross-direction comparisons do not invert meaning.
- H1 and M5 returns use only current and earlier available closed bars.
  Acceleration = signed 1-bar return minus one third of signed 3-bar return.
  M5 pullback/reacceleration and breakout-extension are research proxies, not
  the EA's M15 timing or an execution signal. `m15_timing` holds the latest
  persisted **closed M15** observation's direct flags and availability; it
  is null before one exists and never changes the EA's M15 logic. This
  context is marked `STALE` after a predeclared 45 minutes without an M15
  close; it is never a hard filter.
- `spread_percentile_train` uses only earlier TRAIN observations during TRAIN;
  VAL/OOS use the frozen TRAIN reference. Recent-median spread uses the prior
  20 observations. No OOS data fits a parameter.
- Exact historical selected SL, `room_to_obstacle_R`, entry-tick spread,
  slippage, and CHILL/GOD `trade_class` are null: they are not reproducible
  from this persisted observation stream. Null is not a zero or a favorable
  execution assumption.

## Outcomes and sample control

Only `analyze_entry_quality_v1.py` loads 1h/4h outcomes, after the causal
feature file is frozen and SHA-validated. LONG keeps return/MFE/MAE signs;
SHORT inverts return and swaps/inverts MFE/MAE. These are gross directional
price-return proxies, **not realized PnL, expectancy R, or profit factor**.
Fixed buckets are in the analysis module. `SUFFICIENT` is N>=100,
`THIN_SAMPLE` is 30<=N<100, and `INSUFFICIENT` is N<30 per cell. A
`USEFUL` label means only a predeclared descriptive contrast has the same
sign with adequate sample in TRAIN/VALIDATION/OOS; it is not a trading rule.
The temporal 60/20/20 split is inherited unchanged from Gold Regime V1.

Outputs live in ignored `research/output/entry_quality/` and
`research/analysis/`. No filter or candidate is promoted to PAPER/live here.
