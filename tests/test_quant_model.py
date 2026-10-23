"""Synthetic tests for the stdlib meta-label logistic model."""

from __future__ import annotations

import random
import unittest

from research.quant.model import Standardizer, fit_logistic


class StandardizerTests(unittest.TestCase):
    def test_missing_and_constant_columns_map_to_train_mean(self) -> None:
        std = Standardizer.fit([[1.0, 5.0, None], [3.0, 5.0, None]])
        self.assertEqual(std.transform([None, 5.0, 2.0]), [0.0, 0.0, 0.0])
        self.assertAlmostEqual(std.transform([3.0, 5.0, None])[0], 0.7071067811865475)

    def test_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            Standardizer.fit([])


class LogisticTests(unittest.TestCase):
    def test_learns_informative_feature_and_ignores_noise(self) -> None:
        rng = random.Random(7)
        rows, labels = [], []
        for _ in range(600):
            signal = rng.gauss(0, 1)
            rows.append([signal, rng.gauss(0, 1)])
            labels.append(1 if signal + rng.gauss(0, 0.5) > 0 else 0)
        model = fit_logistic(rows, labels, l2=1.0)
        self.assertGreater(model.weights[0], 1.0)
        self.assertLess(abs(model.weights[1]), 0.3)
        self.assertGreater(model.predict([2.0, 0.0]), 0.9)
        self.assertLess(model.predict([-2.0, 0.0]), 0.1)

    def test_deterministic(self) -> None:
        rows = [[float(i % 7), float(i % 3)] for i in range(50)]
        labels = [i % 2 for i in range(50)]
        self.assertEqual(fit_logistic(rows, labels), fit_logistic(rows, labels))

    def test_validation(self) -> None:
        with self.assertRaises(ValueError):
            fit_logistic([[1.0]], [2])
        with self.assertRaises(ValueError):
            fit_logistic([[1.0]], [1, 0])


if __name__ == "__main__":
    unittest.main()
