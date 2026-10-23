# Quant Research V1 — contract

Offline, diagnostic-only research. Every record/report declares
`observer_only=true` and `score_effect=0`. Nothing here is imported by the EA,
dashboard or execution path, and nothing changes RiskPercent, DailyLossLimit,
SL/TP, scoring, thresholds or order authorization.

## Rules shared by all modules

- Python 3.12 standard library only (no numpy/pandas/scipy/sklearn): the
  repository has no dependency manifest and existing research is stdlib-only.
- Timestamps are epoch ms of MT5 server wall time (`timestamp_unit` in bars).
  No timezone/session is inferred.
- Causality: a value computed "as of" time `t` may only use bars with
  `close_timestamp <= t`. Violations raise `LookAheadError`.
- Unknown is `None`, never a fabricated zero. Insufficient warmup -> `None`.
- Deterministic: any randomness takes an explicit `seed`.
- Tests: `unittest`, synthetic data only, file `tests/test_quant_*.py`,
  run with `python -m unittest tests.test_quant_<name> -v`.

## Modules

| Module | Responsibility |
|---|---|
| `stats.py` | bootstrap CIs, PF/expectancy, PSR/DSR, BH-FDR, purged walk-forward folds, CSCV PBO |
| `features.py` | causal bar store + causal H1/H4/M15 feature vector |
| `barriers.py` | M1 triple-barrier R outcomes with explicit cost scenarios; down-only vol-target multiplier |
| `edge_study.py` | CLI: candidates -> features -> outcomes -> walk-forward meta-label + conditional edge with FDR |

## Bar record (JSONL, from `research/output/historical_bars/XAUUSD_<TF>.jsonl`)

`{"timeframe","timestamp","close_timestamp","open","high","low","close","tick_count","avg_spread","min_spread","max_spread",...}`.
Prices are tick mids (`tick_historical_replay`); spread is in price units.
The export's spread is NOT constant: 0.30 early in the sample, but median M1
spread ~0.50-0.59 in Feb-Jun and Sep 2026 (Feb P90 ~1.26). Costs are modelled
by explicit scenarios (ZERO/BASE/STRESS); BASE understates recent spreads.
With a median stop of ~$32, spread is only ~1-2% of R, so cost scenarios move
R little; STRESS is not a stress test of execution/slippage tails.

## EA source defaults referenced (not asserted as VPS inputs)

ATRPeriod=14, ATRStopMultiplier=1.20, MinStopATR=0.80, MaxStopATR=2.00,
StructureBufferATR=0.25, StructureLookback=20, MinRewardRisk=1.25,
MaxSpreadUSD=0.60, RiskPercent=5.0 (ceiling; research may only scale DOWN).
