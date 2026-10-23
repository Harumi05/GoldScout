"""Stdlib L2 logistic regression for offline meta-labeling (score_effect=0).

The model is fitted only on a training fold. Standardization statistics and
missing-value imputation also come from that fold, so a test fold never leaks
into the fitted parameters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


@dataclass(frozen=True)
class Standardizer:
    means: tuple[float, ...]
    stds: tuple[float, ...]

    @classmethod
    def fit(cls, rows: Sequence[Sequence[float | None]]) -> "Standardizer":
        if not rows:
            raise ValueError("cannot fit a standardizer on zero rows")
        width = len(rows[0])
        means: list[float] = []
        stds: list[float] = []
        for j in range(width):
            col = [r[j] for r in rows if r[j] is not None and math.isfinite(r[j])]
            if len(col) < 2:
                means.append(0.0)
                stds.append(0.0)
                continue
            mean = sum(col) / len(col)
            var = sum((v - mean) ** 2 for v in col) / (len(col) - 1)
            means.append(mean)
            stds.append(math.sqrt(var))
        return cls(tuple(means), tuple(stds))

    def transform(self, row: Sequence[float | None]) -> list[float]:
        """Z-score a row; missing or constant columns map to 0 (the train mean)."""
        out: list[float] = []
        for v, m, s in zip(row, self.means, self.stds):
            if v is None or not math.isfinite(v) or s <= 0:
                out.append(0.0)
            else:
                out.append(max(-6.0, min(6.0, (v - m) / s)))
        return out


@dataclass(frozen=True)
class LogisticModel:
    standardizer: Standardizer
    weights: tuple[float, ...]
    bias: float
    l2: float
    iterations: int

    def predict(self, row: Sequence[float | None]) -> float:
        x = self.standardizer.transform(row)
        return _sigmoid(self.bias + sum(w * v for w, v in zip(self.weights, x)))


def fit_logistic(rows: Sequence[Sequence[float | None]], labels: Sequence[int], *,
                 l2: float = 1.0, iterations: int = 300,
                 learning_rate: float = 0.5) -> LogisticModel:
    """Full-batch gradient descent on mean log-loss + (l2 / 2n) * ||w||^2."""
    if len(rows) != len(labels):
        raise ValueError("rows and labels differ in length")
    if not rows:
        raise ValueError("cannot fit on zero rows")
    if any(y not in (0, 1) for y in labels):
        raise ValueError("labels must be 0/1")
    std = Standardizer.fit(rows)
    xs = [std.transform(r) for r in rows]
    n = len(xs)
    width = len(xs[0])
    w = [0.0] * width
    prior = min(max(sum(labels) / n, 1e-6), 1 - 1e-6)
    b = math.log(prior / (1 - prior))
    for _ in range(iterations):
        grad_w = [0.0] * width
        grad_b = 0.0
        for x, y in zip(xs, labels):
            err = _sigmoid(b + sum(wj * xj for wj, xj in zip(w, x))) - y
            grad_b += err
            for j in range(width):
                grad_w[j] += err * x[j]
        for j in range(width):
            w[j] -= learning_rate * (grad_w[j] / n + l2 * w[j] / n)
        b -= learning_rate * grad_b / n
    return LogisticModel(std, tuple(w), b, l2, iterations)
