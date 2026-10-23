"""Offline statistics for quant research (observer_only, score_effect=0).

Stdlib-only helpers for trade-sequence evaluation: expectancy / profit factor,
circular-block bootstrap CIs and p-values, probabilistic and deflated Sharpe
ratios, Benjamini-Hochberg FDR control, purged/embargoed walk-forward folds and
CSCV probability of backtest overfitting. Nothing here touches the EA.

Conventions: unknown -> ``None``; randomness always takes an explicit ``seed``.
"""

from __future__ import annotations

from dataclasses import dataclass
import itertools
import math
import random
from typing import Sequence

EULER_GAMMA = 0.5772156649015329
BOOTSTRAP_METHOD = "circular_block_bootstrap_percentile"


# ---------------------------------------------------------------------------
# Basic trade metrics
# ---------------------------------------------------------------------------

def _clean(values: Sequence[float]) -> list[float]:
    return [float(v) for v in values]


def expectancy(values: Sequence[float]) -> float | None:
    """Arithmetic mean of per-trade outcomes; ``None`` when empty."""
    data = _clean(values)
    return sum(data) / len(data) if data else None


def profit_factor(values: Sequence[float]) -> float | None:
    """Gross wins / abs(gross losses).

    ``None`` when there are no wins and no losses (empty or all zeros);
    ``float('inf')`` when there are wins but no losses; ``0.0`` when there are
    losses but no wins. Zero outcomes count as neither.
    """
    data = _clean(values)
    wins = sum(v for v in data if v > 0)
    losses = -sum(v for v in data if v < 0)
    if wins == 0 and losses == 0:
        return None
    if losses == 0:
        return float("inf")
    return wins / losses


def win_rate(values: Sequence[float]) -> float | None:
    """Fraction of strictly positive outcomes; ``None`` when empty."""
    data = _clean(values)
    return sum(1 for v in data if v > 0) / len(data) if data else None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (q in [0, 1]) of pre-sorted data."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def _circular_block_means(data: Sequence[float], n_boot: int, block_size: int,
                          seed: int) -> list[float]:
    """Bootstrap means from circular blocks of ``block_size`` consecutive values."""
    n = len(data)
    if n_boot < 1:
        raise ValueError("n_boot must be >= 1")
    if block_size < 1:
        raise ValueError("block_size must be >= 1")
    block = min(block_size, n)
    n_blocks = math.ceil(n / block)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n_boot):
        total = 0.0
        taken = 0
        for _b in range(n_blocks):
            start = rng.randrange(n)
            for offset in range(block):
                if taken == n:
                    break
                total += data[(start + offset) % n]
                taken += 1
        means.append(total / n)
    return means


def bootstrap_mean_ci(values: Sequence[float], *, n_boot: int = 2000, alpha: float = 0.05,
                      block_size: int = 1, seed: int = 0) -> dict:
    """Percentile CI for the mean via circular block bootstrap.

    ``block_size > 1`` preserves short-range autocorrelation of trade
    sequences. With fewer than two observations the bounds are ``None``.
    """
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")
    data = _clean(values)
    result = {"mean": expectancy(data), "lower": None, "upper": None, "n": len(data),
              "block_size": block_size, "method": BOOTSTRAP_METHOD}
    if len(data) < 2:
        return result
    means = sorted(_circular_block_means(data, n_boot, block_size, seed))
    result["lower"] = _percentile(means, alpha / 2)
    result["upper"] = _percentile(means, 1 - alpha / 2)
    return result


def bootstrap_pvalue_mean_gt_zero(values: Sequence[float], *, n_boot: int = 2000,
                                  block_size: int = 1, seed: int = 0) -> float | None:
    """One-sided bootstrap p-value for H0: mean <= 0 (H1: mean > 0).

    The series is demeaned so the bootstrap distribution reflects H0, then
    ``p = (1 + #{boot_mean >= observed_mean}) / (1 + n_boot)``. ``None`` if n<2.
    """
    data = _clean(values)
    if len(data) < 2:
        return None
    observed = sum(data) / len(data)
    centered = [v - observed for v in data]
    means = _circular_block_means(centered, n_boot, block_size, seed)
    # Tolerance absorbs float noise from demeaning (e.g. constant series).
    tol = 1e-12 * max(1.0, abs(observed))
    exceed = sum(1 for m in means if m >= observed - tol)
    return (1 + exceed) / (1 + n_boot)


# ---------------------------------------------------------------------------
# Normal distribution
# ---------------------------------------------------------------------------

def normal_cdf(x: float) -> float:
    """Standard normal CDF via ``math.erfc`` (accurate in both tails)."""
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


_A = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
      1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
_B = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
      6.680131188771972e+01, -1.328068155288572e+01)
_C = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
      -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
_D = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
      3.754408661907416e+00)
_P_LOW = 0.02425


def normal_ppf(p: float) -> float:
    """Inverse standard normal CDF (Acklam rational approx + one Halley step)."""
    if not 0.0 < p < 1.0:
        if p == 0.0:
            return float("-inf")
        if p == 1.0:
            return float("inf")
        raise ValueError("p must be in [0, 1]")
    if p < _P_LOW:
        q = math.sqrt(-2 * math.log(p))
        x = (((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    elif p <= 1 - _P_LOW:
        q = p - 0.5
        r = q * q
        x = (((((_A[0] * r + _A[1]) * r + _A[2]) * r + _A[3]) * r + _A[4]) * r + _A[5]) * q / \
            (((((_B[0] * r + _B[1]) * r + _B[2]) * r + _B[3]) * r + _B[4]) * r + 1)
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        x = -(((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]) / \
            ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    # Halley refinement brings the ~1e-9 approximation to near machine precision.
    e = normal_cdf(x) - p
    u = e * math.sqrt(2 * math.pi) * math.exp(x * x / 2)
    return x - u / (1 + x * u / 2)


# ---------------------------------------------------------------------------
# Sharpe family
# ---------------------------------------------------------------------------

def _moments(data: Sequence[float]) -> tuple[float, float, float, float]:
    """Return (mean, sample std ddof=1, skewness, non-excess kurtosis).

    Skewness/kurtosis use population central moments (m3/m2^1.5, m4/m2^2) as
    in Bailey & Lopez de Prado. Caller guarantees n >= 2 and non-zero variance.
    """
    n = len(data)
    mean = sum(data) / n
    dev = [v - mean for v in data]
    m2 = sum(d * d for d in dev) / n
    m3 = sum(d ** 3 for d in dev) / n
    m4 = sum(d ** 4 for d in dev) / n
    std = math.sqrt(m2 * n / (n - 1))
    return mean, std, m3 / m2 ** 1.5, m4 / m2 ** 2


def sharpe_ratio(values: Sequence[float]) -> float | None:
    """Per-observation Sharpe (mean / sample std, ddof=1), not annualized.

    ``None`` when n < 2 or the standard deviation is zero.
    """
    data = _clean(values)
    if len(data) < 2:
        return None
    mean = sum(data) / len(data)
    var = sum((v - mean) ** 2 for v in data) / (len(data) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var)


def probabilistic_sharpe_ratio(values: Sequence[float],
                               sr_benchmark: float = 0.0) -> float | None:
    """PSR: probability that the true per-observation SR exceeds ``sr_benchmark``.

    ``PSR = Phi((SR - SR*) * sqrt(n-1) / sqrt(1 - g3*SR + (g4-1)/4 * SR^2))``
    with g3 skewness and g4 non-excess kurtosis. ``None`` when n < 3, zero
    variance, or a non-positive variance term (pathological moments).
    """
    data = _clean(values)
    if len(data) < 3:
        return None
    sr = sharpe_ratio(data)
    if sr is None:
        return None
    _mean, _std, skew, kurt = _moments(data)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0:
        return None
    return normal_cdf((sr - sr_benchmark) * math.sqrt(len(data) - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum SR of ``n_trials`` zero-skill trials (False Strategy Theorem).

    ``sqrt(V) * ((1-g)*Phi^-1(1-1/N) + g*Phi^-1(1-1/(N*e)))``; 0.0 for N <= 1.
    """
    if sr_variance < 0:
        raise ValueError("sr_variance must be >= 0")
    if n_trials <= 1:
        return 0.0
    n = float(n_trials)
    return math.sqrt(sr_variance) * ((1 - EULER_GAMMA) * normal_ppf(1 - 1 / n)
                                     + EULER_GAMMA * normal_ppf(1 - 1 / (n * math.e)))


def deflated_sharpe_ratio(values: Sequence[float], *, n_trials: int,
                          sr_variance: float) -> float | None:
    """DSR: PSR against the expected max SR of ``n_trials`` trials.

    ``sr_variance`` is the cross-trial variance of per-observation SR
    estimates. With ``n_trials <= 1`` this equals PSR at benchmark 0.
    """
    return probabilistic_sharpe_ratio(values, expected_max_sharpe(n_trials, sr_variance))


# ---------------------------------------------------------------------------
# Multiple testing
# ---------------------------------------------------------------------------

def benjamini_hochberg(pvalues: Sequence[float | None], q: float = 0.10) -> list[bool]:
    """Benjamini-Hochberg step-up FDR control; returns rejections in input order.

    ``None`` p-values are never rejected but still count toward ``m``
    (conservative: an untestable hypothesis is a test that failed).
    """
    if not 0 < q < 1:
        raise ValueError("q must be in (0, 1)")
    m = len(pvalues)
    indexed = sorted((p, i) for i, p in enumerate(pvalues) if p is not None)
    cutoff_rank = 0
    for rank, (p, _i) in enumerate(indexed, start=1):
        if p <= rank / m * q:
            cutoff_rank = rank
    rejected = [False] * m
    for p, i in indexed[:cutoff_rank]:
        rejected[i] = True
    return rejected


# ---------------------------------------------------------------------------
# Purged walk-forward
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Fold:
    """One anchored walk-forward fold; indices refer to the caller's input order."""

    index: int
    train: tuple[int, ...]
    test: tuple[int, ...]
    test_start_ms: int
    test_end_ms: int


def purged_walk_forward_folds(event_times_ms: Sequence[int], label_end_times_ms: Sequence[int],
                              *, n_folds: int = 5, embargo_ms: int = 0,
                              min_train: int = 50) -> list[Fold]:
    """Expanding-window walk-forward folds with purging and embargo.

    Events are ordered by event time (stable; original indices returned) and
    cut into ``n_folds + 1`` contiguous blocks. Fold k trains on blocks 0..k
    and tests on block k+1. ``test_start_ms`` is the first test event time and
    ``test_end_ms`` the latest test label end. Train events with
    ``label_end >= test_start_ms - embargo_ms`` are dropped (purge + embargo),
    so no train label reaches into (or within ``embargo_ms`` of) the test
    window. Folds whose purged train has fewer than ``min_train`` events are
    skipped; ``Fold.index`` keeps the block number so skips leave gaps.
    """
    if len(event_times_ms) != len(label_end_times_ms):
        raise ValueError("event_times_ms and label_end_times_ms differ in length")
    if n_folds < 1:
        raise ValueError("n_folds must be >= 1")
    if embargo_ms < 0:
        raise ValueError("embargo_ms must be >= 0")
    for start, end in zip(event_times_ms, label_end_times_ms):
        if end < start:
            raise ValueError("label_end_time precedes event_time")
    n = len(event_times_ms)
    n_blocks = n_folds + 1
    if n < n_blocks:
        return []
    order = sorted(range(n), key=lambda i: event_times_ms[i])
    bounds = [b * n // n_blocks for b in range(n_blocks + 1)]
    folds: list[Fold] = []
    for k in range(n_folds):
        test = order[bounds[k + 1]:bounds[k + 2]]
        test_start = min(event_times_ms[i] for i in test)
        test_end = max(label_end_times_ms[i] for i in test)
        cutoff = test_start - embargo_ms
        train = [i for i in order[:bounds[k + 1]] if label_end_times_ms[i] < cutoff]
        if len(train) < min_train:
            continue
        folds.append(Fold(k, tuple(train), tuple(test), int(test_start), int(test_end)))
    return folds


# ---------------------------------------------------------------------------
# CSCV probability of backtest overfitting
# ---------------------------------------------------------------------------

def _sharpe_from_sums(total: float, total_sq: float, count: int) -> float:
    """Sample Sharpe from aggregates; zero variance -> 0 or +/-inf by sign of mean."""
    mean = total / count
    var = (total_sq - count * mean * mean) / (count - 1) if count > 1 else 0.0
    if var <= 1e-18 * max(1.0, total_sq / count):
        return 0.0 if mean == 0 else math.copysign(float("inf"), mean)
    return mean / math.sqrt(var)


def _average_rank(values: Sequence[float], target: int) -> float:
    """1-based ascending rank of ``values[target]`` with ties averaged."""
    v = values[target]
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return below + (equal + 1) / 2


def cscv_pbo(perf_matrix: Sequence[Sequence[float]], *, n_splits: int = 16,
             max_combinations: int = 500, seed: int = 0) -> dict:
    """Probability of backtest overfitting via CSCV (Bailey et al. 2015).

    ``perf_matrix`` rows are time-ordered periods, columns are configs. Rows
    are split into ``n_splits`` contiguous groups; each combination of half the
    groups is in-sample (IS), the complement out-of-sample (OOS). The config
    with the best IS Sharpe (first index on ties) gets its OOS relative rank
    ``w = rank / (N + 1)`` (ties averaged) and logit ``ln(w / (1 - w))``.
    PBO is the fraction of logits <= 0. If ``C(S, S/2)`` exceeds
    ``max_combinations``, that many distinct combinations are sampled with
    ``seed``.
    """
    rows = [list(map(float, r)) for r in perf_matrix]
    if n_splits < 2 or n_splits % 2:
        raise ValueError("n_splits must be an even integer >= 2")
    if len(rows) < n_splits:
        raise ValueError("need at least n_splits rows")
    n_configs = len(rows[0])
    if n_configs < 2:
        raise ValueError("need at least 2 configs")
    if any(len(r) != n_configs for r in rows):
        raise ValueError("ragged perf_matrix")
    t = len(rows)
    bounds = [s * t // n_splits for s in range(n_splits + 1)]
    sums = [[0.0] * n_configs for _ in range(n_splits)]
    sq = [[0.0] * n_configs for _ in range(n_splits)]
    counts = [bounds[s + 1] - bounds[s] for s in range(n_splits)]
    for s in range(n_splits):
        for r in rows[bounds[s]:bounds[s + 1]]:
            for c, v in enumerate(r):
                sums[s][c] += v
                sq[s][c] += v * v

    half = n_splits // 2
    total_combos = math.comb(n_splits, half)
    if total_combos <= max_combinations:
        combos = list(itertools.combinations(range(n_splits), half))
    else:
        rng = random.Random(seed)
        chosen: set[tuple[int, ...]] = set()
        combos = []
        while len(combos) < max_combinations:
            combo = tuple(sorted(rng.sample(range(n_splits), half)))
            if combo not in chosen:
                chosen.add(combo)
                combos.append(combo)

    def sharpes(groups: Sequence[int]) -> list[float]:
        cnt = sum(counts[g] for g in groups)
        return [_sharpe_from_sums(sum(sums[g][c] for g in groups),
                                  sum(sq[g][c] for g in groups), cnt)
                for c in range(n_configs)]

    logits: list[float] = []
    for combo in combos:
        in_set = set(combo)
        is_sr = sharpes(combo)
        oos_sr = sharpes([g for g in range(n_splits) if g not in in_set])
        best = max(range(n_configs), key=lambda c: (is_sr[c], -c))
        omega = _average_rank(oos_sr, best) / (n_configs + 1)
        logits.append(math.log(omega / (1 - omega)))
    logits_sorted = sorted(logits)
    mid = len(logits_sorted) // 2
    median = (logits_sorted[mid] if len(logits_sorted) % 2
              else (logits_sorted[mid - 1] + logits_sorted[mid]) / 2)
    return {"pbo": sum(1 for x in logits if x <= 0) / len(logits),
            "n_combinations": len(combos), "n_configs": n_configs,
            "n_splits": n_splits, "logits_median": median, "metric": "sharpe"}
